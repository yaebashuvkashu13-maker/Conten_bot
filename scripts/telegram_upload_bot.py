#!/usr/bin/env python3
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from source_freshness import filter_new_sources, mark_used
except ImportError:
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from source_freshness import filter_new_sources, mark_used

ENV_FILE = Path('/root/.video_bot.env')
LOG_FILE = Path('/root/telegram_upload_bot.log')
STATE_FILE = Path('/root/.telegram_upload_bot_state.json')
UPLOAD_ROOT = Path('/root/telegram_uploads')
PENDING_ROOT = UPLOAD_ROOT / 'pending'
ARCHIVE_ROOT = UPLOAD_ROOT / 'archive'
PROCESSOR = '/usr/local/bin/smart_video_editor.py'
AD_INGEST = '/usr/local/bin/ad_screenshot_ingest.py'
PUBG_LEARN = '/usr/local/bin/pubg_stream_learn_worker.py'
AD_EXAMPLES_DIR = Path('/root/data/mlbb/ad_examples')
REJECT_EXAMPLES_DIR = Path('/root/data/mlbb/reject_examples')
WATERMARK_EXAMPLES_DIR = Path('/root/data/mlbb/watermark_examples')
WATERMARK_REMOVE_SCRIPT = Path(__file__).resolve().parent / 'image_watermark_remove.py'
RESEARCH_INBOX_DIR = Path('/root/research/inbox')
POLL_TIMEOUT = 25
AD_MODE_TIMEOUT_SEC = 3600
REJECT_MODE_TIMEOUT_SEC = 3600
WM_MODE_TIMEOUT_SEC = 3600
BOT_VERSION = '2026-06-02-wm-ocr'
RESEARCH_ANALYSIS = Path('/usr/local/bin/research_delivery_analysis.py')
INSTAGRAM_COOKIES_PATH = Path('/root/instagram_cookies.txt')
INSTAGRAM_DIGEST_RUN = Path('/usr/local/bin/instagram_digest_run.sh')
PROFILE_LABELS = {
    'pubg': 'PUBG Mobile',
    'mobile_legends': 'Mobile Legends',
    'mlbb': 'Mobile Legends',
}

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
PENDING_ROOT.mkdir(parents=True, exist_ok=True)
ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)


def load_env(path: Path):
    env = {}
    if not path.exists():
        raise RuntimeError(f'env file not found: {path}')
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip()
    return env


def command_token(text: str) -> str:
    """Telegram sends /ad@BotName — normalize to /ad."""
    if not text:
        return ''
    return text.split()[0].split('@')[0].lower()


def tail_smart_edit_log(max_lines: int = 8) -> str:
    log_path = Path('/root/smart_video_editor.log')
    if not log_path.exists():
        return ''
    try:
        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    interesting = [line for line in lines[-80:] if 'ERROR' in line or 'error' in line.lower() or 'no ' in line.lower()]
    pick = interesting[-max_lines:] if interesting else lines[-max_lines:]
    snippet = '\n'.join(pick).strip()
    return snippet[:600]


def parse_chat_game_profiles() -> dict[str, str]:
    """CHAT_GAME_PROFILES=6366727522:pubg or PUBG_CHAT_IDS=6366727522"""
    mapping: dict[str, str] = {}
    raw = env.get('CHAT_GAME_PROFILES', '')
    for part in raw.split(','):
        part = part.strip()
        if ':' not in part:
            continue
        chat_id, profile = part.split(':', 1)
        mapping[chat_id.strip()] = profile.strip().lower()
    for chat_id in env.get('PUBG_CHAT_IDS', '').split(','):
        chat_id = chat_id.strip()
        if chat_id:
            mapping.setdefault(chat_id, 'pubg')
    return mapping


def game_profile_for_chat(chat_id: str) -> str | None:
    return parse_chat_game_profiles().get(str(chat_id))


def game_label_for_chat(chat_id: str, caption: str | None = None) -> str:
    profile = game_profile_for_chat(chat_id)
    if not profile:
        return safe_label(caption)
    base = PROFILE_LABELS.get(profile, profile.replace('_', ' ').title())
    cap = safe_label(caption)
    if cap and cap != 'Telegram upload':
        return f'{base} | {cap}'
    return base


def is_owner(chat_id: str) -> bool:
    cid = str(chat_id)
    owners = {str(DEFAULT_CHAT_ID)} if DEFAULT_CHAT_ID else set()
    for item in env.get('AD_OWNER_CHAT_IDS', env.get('OWNER_CHAT_IDS', '')).split(','):
        item = item.strip()
        if item:
            owners.add(item)
    return cid in owners


def chat_is_allowed(chat_id: str) -> bool:
    """Owner (TG_CHAT_ID) is always allowed even if missing from TG_ALLOWED_CHAT_IDS."""
    cid = str(chat_id)
    if is_owner(cid):
        return True
    if not ALLOWED_CHAT_IDS:
        return True
    return cid in ALLOWED_CHAT_IDS


def is_pubg_chat(chat_id: str) -> bool:
    return game_profile_for_chat(chat_id) == 'pubg'


def spawn_pubg_learning(video_path: Path, chat_id: str) -> None:
    if not is_pubg_chat(chat_id) or not Path(PUBG_LEARN).exists():
        return

    def _run():
        try:
            subprocess.run(
                ['python3', PUBG_LEARN, '--video', str(video_path), '--chat-id', chat_id],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except Exception as exc:
            logging.warning('pubg learn failed for %s: %s', video_path, exc)

    threading.Thread(target=_run, daemon=True).start()


def safe_label(text: str | None) -> str:
    if not text:
        return 'Telegram upload'
    normalized = ' '.join(text.replace('|', ' ').split())
    return normalized[:60] or 'Telegram upload'


env = load_env(ENV_FILE)
BOT_TOKEN = env['TG_BOT_TOKEN']
DEFAULT_CHAT_ID = env.get('TG_CHAT_ID', '')
ALLOWED_CHAT_IDS = {item.strip() for item in env.get('TG_ALLOWED_CHAT_IDS', '').split(',') if item.strip()}
AUTO_MAKE_CHAT_IDS = {item.strip() for item in env.get('AUTO_MAKE_CHAT_IDS', '').split(',') if item.strip()}
LIMITED_NOTIFY_CHAT_IDS = {
    item.strip()
    for item in env.get('LIMITED_NOTIFY_CHAT_IDS', env.get('AUTO_MAKE_CHAT_IDS', '')).split(',')
    if item.strip()
}
API_BASE = f'https://api.telegram.org/bot{BOT_TOKEN}'
FILE_BASE = f'https://api.telegram.org/file/bot{BOT_TOKEN}'
PROCESSING_CHATS: set[str] = set()
PROCESSING_LOCK = threading.Lock()


def api_call(method: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload or {}).encode('utf-8')
    request = urllib.request.Request(
        f'{API_BASE}/{method}',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not result.get('ok'):
        raise RuntimeError(f'Telegram API error for {method}: {result}')
    return result['result']


def is_limited_notify(chat_id: str | int) -> bool:
    return str(chat_id) in LIMITED_NOTIFY_CHAT_IDS


def send_message(chat_id: str | int, text: str):
    try:
        api_call('sendMessage', {'chat_id': str(chat_id), 'text': text}, timeout=30)
    except Exception as exc:
        logging.error('failed to send message to %s: %s', chat_id, exc)


def send_photo_file(chat_id: str | int, image_path: Path, caption: str = '') -> None:
    cmd = [
        'curl',
        '-sS',
        '-m',
        '120',
        '-F',
        f'chat_id={chat_id}',
        '-F',
        f'caption={caption}',
        '-F',
        f'photo=@{image_path}',
        f'{API_BASE}/sendPhoto',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or 'sendPhoto failed')
    result = json.loads(proc.stdout or '{}')
    if not result.get('ok'):
        raise RuntimeError(result)


def run_watermark_clean(source: Path) -> tuple[Path, bool, str]:
    import sys

    scripts_dir = WATERMARK_REMOVE_SCRIPT.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import cv2
    from image_watermark_remove import clean_image_file, detect_watermark_source, remove_watermarks

    img = cv2.imread(str(source))
    if img is None:
        return source, False, 'none'
    source_kind, boxes = detect_watermark_source(img)
    if not boxes:
        return source, False, source_kind
    cleaned, changed = remove_watermarks(img)
    out = source.parent / f'{source.stem}_clean{source.suffix}'
    cv2.imwrite(str(out), cleaned, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return out, changed, source_kind


def save_wm_photo(chat_id: str, message: dict) -> Path | None:
    photo = extract_photo(message)
    if not photo:
        return None
    WATERMARK_EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    caption = safe_label(message.get('caption'))[:80]
    stamp = time.strftime('%Y%m%d_%H%M%S')
    name = f'wm_{chat_id}_{stamp}_{photo["file_unique_id"]}{photo["ext"]}'
    destination = WATERMARK_EXAMPLES_DIR / name
    file_url = get_file_url(photo['file_id'])
    download_file(file_url, destination)
    meta = WATERMARK_EXAMPLES_DIR / f'{destination.stem}.meta.json'
    meta.write_text(
        json.dumps(
            {
                'chat_id': chat_id,
                'caption': caption,
                'message_id': message.get('message_id'),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return destination


def process_wm_photo(chat_id: str, message: dict) -> None:
    saved = save_wm_photo(chat_id, message)
    if not saved:
        send_message(chat_id, 'Не удалось сохранить фото.')
        return
    try:
        cleaned_path, changed, source_kind = run_watermark_clean(saved)
        if changed:
            how = 'по красной обводке' if source_kind == 'red_markup' else 'по тексту OCR'
            send_photo_file(
                chat_id,
                cleaned_path,
                f'Готово ({how}). Примеров: {count_wm_examples()}.',
            )
        else:
            send_message(
                chat_id,
                'Не нашёл зону знака.\n'
                '• Обведите **только** надпись «god of mlbb» внизу кадра (ярко-красным).\n'
                '• Не обводите весь экран — в игре много красного UI.\n'
                '• Или пришлите без обводки — попробую OCR.',
            )
            send_photo_file(chat_id, saved, 'Оригинал (зона не найдена).')
    except Exception as exc:
        logging.exception('watermark clean failed')
        send_message(chat_id, f'Ошибка обработки: {exc}')


def notify_owner(text: str):
    if DEFAULT_CHAT_ID:
        send_message(DEFAULT_CHAT_ID, text)


def send_upload_status(chat_id: str, pending_count: int):
    if is_limited_notify(chat_id):
        send_message(chat_id, 'Видео получено. Идёт нарезка…')
        return
    send_message(
        chat_id,
        f'Видео сохранено. Сейчас в очереди {pending_count} шт. Можешь прислать еще или дать /make.',
    )


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {'last_update_id': 0, 'ad_mode_until': {}, 'reject_mode_until': {}, 'wm_mode_until': {}}
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {'last_update_id': 0, 'ad_mode_until': {}, 'reject_mode_until': {}, 'wm_mode_until': {}}
    state.setdefault('ad_mode_until', {})
    state.setdefault('reject_mode_until', {})
    state.setdefault('wm_mode_until', {})
    return state


def _bot_state() -> dict:
    """Mutable bot state persisted across polls (ad mode, offset)."""
    if not hasattr(_bot_state, 'data'):
        _bot_state.data = load_state()
    return _bot_state.data


def is_ad_mode(chat_id: str) -> bool:
    until = _bot_state().get('ad_mode_until', {}).get(chat_id)
    if not until:
        return False
    if time.time() > float(until):
        _bot_state()['ad_mode_until'].pop(chat_id, None)
        save_state(_bot_state())
        return False
    return True


def set_ad_mode(chat_id: str, enabled: bool):
    state = _bot_state()
    state.setdefault('ad_mode_until', {})
    if enabled:
        state['ad_mode_until'][chat_id] = time.time() + AD_MODE_TIMEOUT_SEC
    else:
        state['ad_mode_until'].pop(chat_id, None)
    save_state(state)


def is_reject_mode(chat_id: str) -> bool:
    until = _bot_state().get('reject_mode_until', {}).get(chat_id)
    if not until:
        return False
    if time.time() > float(until):
        _bot_state()['reject_mode_until'].pop(chat_id, None)
        save_state(_bot_state())
        return False
    return True


def set_reject_mode(chat_id: str, enabled: bool):
    state = _bot_state()
    state.setdefault('reject_mode_until', {})
    if enabled:
        state['reject_mode_until'][chat_id] = time.time() + REJECT_MODE_TIMEOUT_SEC
    else:
        state['reject_mode_until'].pop(chat_id, None)
    save_state(state)


def is_wm_mode(chat_id: str) -> bool:
    until = _bot_state().get('wm_mode_until', {}).get(chat_id)
    if not until:
        return False
    if time.time() > float(until):
        _bot_state()['wm_mode_until'].pop(chat_id, None)
        save_state(_bot_state())
        return False
    return True


def set_wm_mode(chat_id: str, enabled: bool):
    state = _bot_state()
    state.setdefault('wm_mode_until', {})
    if enabled:
        state['wm_mode_until'][chat_id] = time.time() + WM_MODE_TIMEOUT_SEC
    else:
        state['wm_mode_until'].pop(chat_id, None)
    save_state(state)


def count_wm_examples() -> int:
    if not WATERMARK_EXAMPLES_DIR.exists():
        return 0
    exts = {'.jpg', '.jpeg', '.png', '.webp'}
    return sum(1 for p in WATERMARK_EXAMPLES_DIR.iterdir() if p.suffix.lower() in exts)


def count_reject_examples() -> int:
    if not REJECT_EXAMPLES_DIR.exists():
        return 0
    return len(list(REJECT_EXAMPLES_DIR.glob('reject_*.*')))


def save_reject_photo(chat_id: str, message: dict) -> Path | None:
    photo = extract_photo(message)
    if not photo:
        return None
    REJECT_EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    caption = safe_label(message.get('caption'))[:80]
    stamp = time.strftime('%Y%m%d_%H%M%S')
    name = f"reject_{chat_id}_{stamp}_{photo['file_unique_id']}{photo['ext']}"
    destination = REJECT_EXAMPLES_DIR / name
    file_url = get_file_url(photo['file_id'])
    download_file(file_url, destination)
    meta = REJECT_EXAMPLES_DIR / f'{destination.stem}.meta.json'
    meta.write_text(
        json.dumps(
            {
                'chat_id': chat_id,
                'caption': caption,
                'message_id': message.get('message_id'),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return destination


def count_ad_examples() -> int:
    if not AD_EXAMPLES_DIR.exists():
        return 0
    exts = {'.jpg', '.jpeg', '.png', '.webp'}
    return sum(1 for p in AD_EXAMPLES_DIR.iterdir() if p.suffix.lower() in exts)


def run_ad_index():
    if not Path(AD_INGEST).exists():
        return
    try:
        subprocess.run(['python3', AD_INGEST], check=False, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        logging.warning('ad index failed: %s', exc)


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def chat_pending_dir(chat_id: str) -> Path:
    path = PENDING_ROOT / chat_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def queue_file_for(chat_id: str) -> Path:
    return chat_pending_dir(chat_id) / 'queue.txt'


def count_pending(chat_id: str) -> int:
    queue_file = queue_file_for(chat_id)
    if not queue_file.exists():
        return 0
    return sum(1 for line in queue_file.read_text().splitlines() if line.strip())


def append_pending(chat_id: str, local_path: Path, label: str):
    line = f'{local_path}|{label}|{chat_id}\n'
    with queue_file_for(chat_id).open('a', encoding='utf-8') as handle:
        handle.write(line)


def clear_pending(chat_id: str):
    pending_dir = chat_pending_dir(chat_id)
    if pending_dir.exists():
        for entry in pending_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


def remove_processed_lines(chat_id: str, processed_lines: list[str]):
    queue_file = queue_file_for(chat_id)
    if not queue_file.exists():
        return
    remaining = queue_file.read_text().splitlines()
    for line in processed_lines:
        try:
            remaining.remove(line)
        except ValueError:
            continue
    queue_file.write_text('\n'.join(item for item in remaining if item.strip()) + ('\n' if any(item.strip() for item in remaining) else ''))


def archive_processed(chat_id: str, processed_lines: list[str]):
    if not processed_lines:
        return
    archive_dir = ARCHIVE_ROOT / chat_id / time.strftime('%Y%m%d_%H%M%S')
    archive_dir.mkdir(parents=True, exist_ok=True)
    queue_dump = archive_dir / 'queue.txt'
    queue_dump.write_text('\n'.join(processed_lines) + '\n')
    for line in processed_lines:
        parts = line.split('|')
        if not parts:
            continue
        source_path = Path(parts[0])
        if source_path.exists():
            destination = archive_dir / source_path.name
            counter = 1
            while destination.exists():
                destination = archive_dir / f'{source_path.stem}_{counter}{source_path.suffix}'
                counter += 1
            shutil.move(str(source_path), str(destination))
    remove_processed_lines(chat_id, processed_lines)


def get_file_url(file_id: str) -> str:
    result = api_call('getFile', {'file_id': file_id}, timeout=30)
    return f"{FILE_BASE}/{result['file_path']}"


def download_file(file_url: str, destination: Path):
    with urllib.request.urlopen(file_url, timeout=120) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)


def extract_photo(message: dict):
    photos = message.get('photo')
    if photos:
        best = max(photos, key=lambda item: item.get('file_size', 0))
        return {
            'file_id': best['file_id'],
            'file_unique_id': best.get('file_unique_id', best['file_id']),
            'ext': '.jpg',
        }
    document = message.get('document')
    if document:
        mime = (document.get('mime_type') or '').lower()
        name = (document.get('file_name') or '').lower()
        if mime.startswith('image/') or name.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            ext = Path(name).suffix or '.jpg'
            return {
                'file_id': document['file_id'],
                'file_unique_id': document.get('file_unique_id', document['file_id']),
                'ext': ext if ext else '.jpg',
            }
    return None


def save_ad_photo(chat_id: str, message: dict) -> Path | None:
    photo = extract_photo(message)
    if not photo:
        return None
    AD_EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    caption = safe_label(message.get('caption'))[:40]
    stamp = time.strftime('%Y%m%d_%H%M%S')
    name = f'ad_{chat_id}_{stamp}_{photo["file_unique_id"]}{photo["ext"]}'
    destination = AD_EXAMPLES_DIR / name
    file_url = get_file_url(photo['file_id'])
    download_file(file_url, destination)
    meta = AD_EXAMPLES_DIR / f'{destination.stem}.meta.json'
    meta.write_text(
        json.dumps(
            {
                'chat_id': chat_id,
                'caption': caption,
                'message_id': message.get('message_id'),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return destination


def extract_document_file(message: dict):
    document = message.get('document')
    if not document:
        return None
    file_name = (document.get('file_name') or '').strip()
    if not file_name:
        return None
    ext = Path(file_name).suffix.lower()
    return {
        'file_id': document['file_id'],
        'file_unique_id': document.get('file_unique_id', document['file_id']),
        'file_name': file_name,
        'ext': ext or '',
        'mime_type': (document.get('mime_type') or '').lower(),
    }


def research_help_text() -> str:
    return (
        'Excel больше ~20 МБ бот из Telegram не получит — это лимит Bot API, не архиватора.\n'
        'Файл .xlsx уже внутри ZIP: WinRAR/7-Zip почти не ужимают.\n\n'
        'Вариант 1 — ссылка (любой размер):\n'
        'На ПК в PowerShell:\n'
        'curl.exe -T "E:\\путь\\исследование клика.xlsx" https://transfer.sh/\n'
        'Скопируйте выданную https://… ссылку и пришлите боту одной строкой или:\n'
        '/research https://…\n\n'
        'Вариант 2 — уменьшить файл в Excel:\n'
        'Сохранить копию только с order_id, courier_id и колонками G+ за нужный период.\n\n'
        'После загрузки на сервер запускается анализ доставки — отчёт придёт в этот чат.'
    )


def safe_research_filename(name: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in {'.', '-', '_'} else '_' for ch in name)[:120]
    return safe or 'upload.xlsx'


def parse_research_url(text: str) -> str | None:
    if not text or 'http' not in text:
        return None
    match = re.search(r'https?://[^\s<>"\']+', text)
    if not match:
        return None
    return match.group(0).rstrip('.,);')


def looks_like_research_url(url: str) -> bool:
    u = url.lower()
    return any(
        marker in u
        for marker in (
            'transfer.sh',
            'file.io',
            'tmpfiles.org',
            '0x0.st',
            'catbox.moe',
            '.xlsx',
            'research',
        )
    )


def save_research_from_url(url: str, chat_id: str) -> Path | None:
    RESEARCH_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name) or 'research.xlsx'
    if not name.lower().endswith(('.xlsx', '.xlsm', '.xls')):
        if '.' not in name or name.endswith('/'):
            name = 'research.xlsx'
    stamp = time.strftime('%Y%m%d_%H%M%S')
    destination = RESEARCH_INBOX_DIR / f'{stamp}_{safe_research_filename(name)}'
    req = urllib.request.Request(url, headers={'User-Agent': 'ContenBot-research/1.0'})
    with urllib.request.urlopen(req, timeout=600) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)
    meta = destination.with_suffix(destination.suffix + '.meta.json')
    meta.write_text(
        json.dumps(
            {
                'chat_id': chat_id,
                'source_url': url,
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return destination


def extract_xlsx_from_zip(zip_path: Path, chat_id: str) -> Path | None:
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith('.xlsx') or Path(name).name.startswith('~'):
                continue
            stamp = time.strftime('%Y%m%d_%H%M%S')
            destination = RESEARCH_INBOX_DIR / f'{stamp}_{safe_research_filename(Path(name).name)}'
            destination.write_bytes(zf.read(name))
            meta = destination.with_suffix(destination.suffix + '.meta.json')
            meta.write_text(
                json.dumps(
                    {'chat_id': chat_id, 'from_zip': str(zip_path), 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding='utf-8',
            )
            return destination
    return None


def start_research_analysis() -> None:
    script = RESEARCH_ANALYSIS
    if not script.exists():
        script = Path(__file__).resolve().parent / 'research_delivery_analysis.py'
    if not script.exists():
        logging.warning('research_delivery_analysis.py not found')
        return

    def _run():
        try:
            subprocess.run(['python3', str(script)], check=False, timeout=3600)
        except Exception as exc:
            logging.exception('research analysis failed: %s', exc)

    threading.Thread(target=_run, daemon=True).start()


def start_instagram_digest(notify_chat_id: str | None = None) -> None:
    script = INSTAGRAM_DIGEST_RUN
    if not script.exists():
        script = Path(__file__).resolve().parent / 'instagram_digest_run.sh'
    if not INSTAGRAM_COOKIES_PATH.exists():
        if notify_chat_id:
            send_message(
                notify_chat_id,
                'Instagram cookies нет. Пришлите файл cookies.txt (Netscape) как документ — '
                'или положите на сервер /root/instagram_cookies.txt',
            )
        return

    def _run():
        try:
            subprocess.run(['bash', str(script)], check=False, timeout=1800)
            if notify_chat_id:
                log_tail = ''
                log_path = Path('/root/data/mlbb/instagram_digest.log')
                if log_path.exists():
                    log_tail = '\n'.join(log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-4:])
                send_message(
                    notify_chat_id,
                    'Instagram-дайджест завершён. Смотрите посты выше.\n'
                    + (f'Лог:\n{log_tail}' if log_tail else ''),
                )
        except Exception as exc:
            logging.exception('instagram digest failed: %s', exc)
            if notify_chat_id:
                send_message(notify_chat_id, f'Ошибка дайджеста Instagram: {exc}')

    threading.Thread(target=_run, daemon=True).start()


def save_instagram_cookies(message: dict) -> Path:
    doc = extract_document_file(message)
    if not doc:
        raise ValueError('нет файла')
    download_file(get_file_url(doc['file_id']), INSTAGRAM_COOKIES_PATH)
    text = INSTAGRAM_COOKIES_PATH.read_text(encoding='utf-8', errors='replace')
    if 'instagram.com' not in text.lower():
        raise ValueError('файл не похож на cookies Instagram')
    return INSTAGRAM_COOKIES_PATH


def save_research_file(chat_id: str, message: dict) -> Path | None:
    doc = extract_document_file(message)
    if not doc:
        return None
    file_name = doc.get('file_name') or f"upload_{doc.get('file_unique_id')}"
    RESEARCH_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    safe = ''.join(ch if ch.isalnum() or ch in {'.', '-', '_'} else '_' for ch in file_name)[:120]
    stamp = time.strftime('%Y%m%d_%H%M%S')
    destination = RESEARCH_INBOX_DIR / f"{stamp}_{safe}"
    try:
        file_url = get_file_url(doc['file_id'])
        download_file(file_url, destination)
    except Exception as exc:
        # Telegram Bot API cannot download very large files (>~20MB) via getFile.
        logging.warning('failed to download research file: %s', exc)
        return None
    meta = destination.with_suffix(destination.suffix + '.meta.json')
    meta.write_text(
        json.dumps(
            {
                'chat_id': chat_id,
                'message_id': message.get('message_id'),
                'original_name': file_name,
                'mime_type': doc.get('mime_type'),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return destination


def extract_media(message: dict):
    if 'video' in message:
        video = message['video']
        return {
            'file_id': video['file_id'],
            'file_unique_id': video.get('file_unique_id', video['file_id']),
            'ext': '.mp4',
        }
    document = message.get('document')
    if document:
        mime_type = document.get('mime_type', '')
        file_name = document.get('file_name', '')
        if mime_type.startswith('video/') or file_name.lower().endswith(('.mp4', '.mov', '.mkv', '.webm')):
            ext = Path(file_name).suffix or '.mp4'
            return {
                'file_id': document['file_id'],
                'file_unique_id': document.get('file_unique_id', document['file_id']),
                'ext': ext,
            }
    return None


def _lines_from_paths(chat_id: str, paths: list[Path], labels: dict[Path, str] | None = None) -> list[str]:
    labels = labels or {}
    default_label = game_label_for_chat(chat_id)
    return [f"{path}|{labels.get(path, default_label)}|{chat_id}" for path in paths]


def _fix_queue_line(line: str, chat_id: str) -> str:
    """Ensure queue label reflects PUBG/MLBB chat profile (not generic Telegram upload)."""
    line = line.strip()
    if not line:
        return line
    parts = line.split('|')
    if len(parts) < 2:
        return line
    path = parts[0]
    cid = parts[-1] if len(parts) >= 3 else chat_id
    label = '|'.join(parts[1:-1]) if len(parts) >= 3 else parts[1]
    if label.strip().lower() in ('telegram upload', 'telegram', ''):
        label = game_label_for_chat(chat_id)
    return f'{path}|{label}|{cid}'


def _smart_edit_failure_hint(code: int, log_tail: str, chat_id: str = '') -> str:
    pubg = is_pubg_chat(chat_id) if chat_id else False
    if 'no usable sources' in log_tail:
        return 'Файл не прочитался (битый или не видео).'
    if 'no candidates' in log_tail or 'produced no candidates' in log_tail:
        if pubg:
            return 'В стриме не нашлось 3–4 боевых моментов PUBG (меню, лобби, кат-сцены отфильтрованы).'
        return 'В ролике не нашлось 3–4 игровых сцен (меню, мем или нет HUD MLBB).'
    if code == 1:
        return 'Smart Edit не собрал монтаж — см. лог на VPS.'
    return ''


def process_chat_batch(chat_id: str, only_paths: list[Path] | None = None):
    limited = is_limited_notify(chat_id)
    try:
        if only_paths is not None:
            fresh_paths = filter_new_sources(only_paths)
            lines = _lines_from_paths(chat_id, fresh_paths)
        else:
            queue_path = queue_file_for(chat_id)
            lines = [
                _fix_queue_line(line, chat_id)
                for line in queue_path.read_text().splitlines()
                if line.strip()
            ]
            paths = [Path(line.split('|', 1)[0]) for line in lines]
            fresh_paths = filter_new_sources(paths)
            lines = [line for line in lines if Path(line.split('|', 1)[0]) in fresh_paths]
        if not lines:
            if limited:
                send_message(
                    chat_id,
                    'Не могу сделать нарезку: это видео уже использовали или файл слишком старый. '
                    'Пришлите другой ролик.',
                )
                notify_owner(
                    f'Нарезка пропущена для chat {chat_id}: видео уже в used_source или старше 36 ч.',
                )
                return
            send_message(chat_id, 'У тебя пока нет загруженных видео. Сначала пришли файлы, потом команду /make.')
            return

        if not limited:
            game_hint = 'PUBG' if is_pubg_chat(chat_id) else 'MLBB'
            send_message(
                chat_id,
                f'Принял задачу ({game_hint}). Запускаю Smart Edit: 3-4 хайлайта, ~33-57 сек.',
            )
        with tempfile.NamedTemporaryFile('w', delete=False, prefix=f'tg-batch-{chat_id}-', suffix='.txt') as tmp_queue:
            tmp_queue.write('\n'.join(lines) + '\n')
            tmp_queue_path = tmp_queue.name

        run_env = os.environ.copy()
        run_env['QUEUE_FILE'] = tmp_queue_path
        run_env['MAX_SOURCES'] = str(len(lines))
        run_env.setdefault('TARGET_DURATION', env.get('SMART_TARGET_DURATION', '40'))
        profile = game_profile_for_chat(chat_id)
        if profile:
            run_env['DEFAULT_GAME_PROFILE'] = profile
            run_env['QUEUE_GAME_PROFILE'] = profile
        if only_paths is not None and len(only_paths) == 1:
            run_env['SINGLE_SOURCE_MODE'] = '1'
        completed = subprocess.run(
            [PROCESSOR],
            env=run_env,
            timeout=2400,
            capture_output=True,
            text=True,
        )
        Path(tmp_queue_path).unlink(missing_ok=True)

        if completed.returncode == 0:
            mark_used([Path(line.split('|', 1)[0]) for line in lines])
            archive_processed(chat_id, lines)
            if limited:
                # Готовый ролик приходит отдельным sendVideo из smart_video_editor.py
                pass
            else:
                send_message(chat_id, 'Smart Edit v1.1 завершен. Готовый ролик уже отправлен в этот чат.')
        else:
            log_tail = tail_smart_edit_log()
            err_hint = _smart_edit_failure_hint(completed.returncode, log_tail, chat_id)
            if limited:
                send_message(chat_id, f'Не удалось сделать нарезку. {err_hint}')
                notify_owner(
                    f'Ошибка нарезки для chat {chat_id}. Код {completed.returncode}.\n'
                    f'{err_hint}\n{log_tail}',
                )
            else:
                send_message(chat_id, 'Не удалось обработать видео. Исходники сохранены, можно повторить /make позже.')
    except subprocess.TimeoutExpired:
        if limited:
            send_message(chat_id, 'Не удалось сделать нарезку.')
            notify_owner(f'Таймаут нарезки для chat {chat_id}.')
        else:
            send_message(
                chat_id,
                'Обработка заняла слишком много времени. Попробуй отправить более короткие видео или меньше файлов за раз.',
            )
    except Exception as exc:
        logging.exception('failed to process chat batch %s', chat_id)
        if limited:
            send_message(chat_id, 'Не удалось сделать нарезку.')
            notify_owner(f'Ошибка нарезки для chat {chat_id}: {exc}')
        else:
            send_message(chat_id, f'Ошибка обработки: {exc}')
    finally:
        with PROCESSING_LOCK:
            PROCESSING_CHATS.discard(chat_id)


def start_processing(chat_id: str, only_paths: list[Path] | None = None):
    with PROCESSING_LOCK:
        if chat_id in PROCESSING_CHATS:
            if not is_limited_notify(chat_id):
                send_message(chat_id, 'Обработка уже запущена. Дождись результата или сообщения об ошибке.')
            return
        PROCESSING_CHATS.add(chat_id)
    thread = threading.Thread(target=process_chat_batch, args=(chat_id, only_paths), daemon=True)
    thread.start()


def register_bot_commands() -> None:
    try:
        api_call(
            'setMyCommands',
            {
                'commands': [
                    {'command': 'start', 'description': 'Начало работы'},
                    {'command': 'ping', 'description': 'Проверка, что бот жив'},
                    {'command': 'ad', 'description': 'Скрины рекламы (владелец)'},
                    {'command': 'ad_done', 'description': 'Закончить приём скринов'},
                    {'command': 'bad', 'description': 'Плохие кадры/примеры (владелец)'},
                    {'command': 'bad_done', 'description': 'Закончить приём плохих кадров'},
                    {'command': 'make', 'description': 'Собрать нарезку из видео'},
                    {'command': 'research', 'description': 'Большой Excel: ссылка transfer.sh'},
                    {'command': 'ig_digest', 'description': 'Дайджест Instagram блогеров (владелец)'},
                    {'command': 'ig_cookies', 'description': 'Как загрузить cookies Instagram'},
                    {'command': 'wm', 'description': 'Убрать «god of mlbb» со скрина (владелец)'},
                    {'command': 'wm_done', 'description': 'Выйти из режима водяного знака'},
                ],
            },
            timeout=30,
        )
    except Exception as exc:
        logging.warning('setMyCommands failed: %s', exc)


def handle_message(message: dict):
    chat = message.get('chat', {})
    chat_id = str(chat.get('id', ''))
    if not chat_id:
        return
    if not chat_is_allowed(chat_id):
        logging.warning('ignoring unauthorized chat %s (not in ALLOWED, not owner)', chat_id)
        return

    text = (message.get('text') or '').strip()
    cmd = command_token(text)
    logging.info('message chat=%s cmd=%r text=%r', chat_id, cmd, text[:80] if text else '')
    caption = safe_label(message.get('caption'))
    limited = is_limited_notify(chat_id)

    if cmd == '/start' or text.startswith('/start'):
        if is_pubg_chat(chat_id):
            send_message(
                chat_id,
                'Режим PUBG: отправляйте видео со стрима — получите нарезку Smart Edit. '
                'Параллельно ролики сохраняются для обучения по PUBG.',
            )
            return
        if limited:
            send_message(chat_id, 'Отправьте видео — в ответ придёт нарезка.')
        elif chat_id in AUTO_MAKE_CHAT_IDS:
            send_message(
                chat_id,
                'Отправь видео (можно по одному) — нарезка Smart Edit v1.1 запустится автоматически (3-4 сцены, 33-57 сек).',
            )
        else:
            send_message(
                chat_id,
                'Отправь сюда 3-10 видео. Когда все загрузишь, дай /make — я соберу Smart Edit v1.1 из 3-4 хайлайтов на 33-57 секунд.\n\n'
                'Примеры рекламы (скрины): /ad → фото → /ad_done.\n'
                'Водяной знак «god of mlbb»: /wm → фото → /wm_done.',
            )
        return
    if cmd in ('/ad', '/реклама', '/ads'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /ad только для владельца (скрины рекламы MLBB).')
            return
        set_ad_mode(chat_id, True)
        send_message(
            chat_id,
            'Режим примеров рекламы включён на 1 час.\n'
            'Пришли скрины (фото) — сохраню для обучения фильтра «не слать такое».\n'
            'Когда закончишь: /ad_done\n'
            f'Сейчас в базе: {count_ad_examples()} шт.',
        )
        return
    if cmd in ('/wm', '/watermark', '/водяной'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /wm только для владельца (примеры «god of mlbb»).')
            return
        set_wm_mode(chat_id, True)
        send_message(
            chat_id,
            'Режим водяного знака включён на 1 час.\n'
            'Пришли скрин с надписью — уберу её и пришлю результат.\n'
            'Можно обвести знак **красным** (рамка/кружок) — тогда зона берётся с обводки, OCR не нужен.\n'
            'Без обводки — ищем текст «god of mlbb» автоматически.\n'
            'Завершить: /wm_done\n'
            f'Сейчас примеров: {count_wm_examples()} шт.\n'
            'В дайджесте Instagram очистка включена автоматически (IG_REMOVE_WATERMARK=1).',
        )
        return
    if cmd in ('/wm_done', '/wm_stop'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /wm_done только для владельца.')
            return
        was = is_wm_mode(chat_id)
        set_wm_mode(chat_id, False)
        send_message(
            chat_id,
            f'Режим /wm выключен. Примеров: {count_wm_examples()}.\n'
            + ('Последние скрины обработаны.' if was else ''),
        )
        return
    if cmd in ('/bad', '/плохо', '/badframe', '/reject'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /bad только для владельца (примеры неуместных кадров/вставок).')
            return
        set_reject_mode(chat_id, True)
        send_message(
            chat_id,
            'Режим примеров «не слать такое» включён на 1 час.\n'
            'Пришли кадры/скрины (фото) и в подписи коротко почему плохо (мем/меню/донат/реклама/чат и т.д.).\n'
            'Завершить: /bad_done\n'
            f'Сейчас в базе: {count_reject_examples()} шт.',
        )
        return
    if cmd in ('/bad_done', '/bad_stop', '/плохо_готово'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /bad_done только для владельца.')
            return
        was = is_reject_mode(chat_id)
        set_reject_mode(chat_id, False)
        total = count_reject_examples()
        send_message(
            chat_id,
            f'Режим «плохие кадры» выключен. Всего примеров: {total}.\n'
            + ('Последние фото сохранены.' if was else 'Новых фото в этом режиме не было.'),
        )
        notify_owner(f'Reject examples updated: {total} files (chat {chat_id}).')
        return
    if cmd in ('/ad_done', '/ad_stop', '/реклама_готово'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /ad_done только для владельца.')
            return
        was = is_ad_mode(chat_id)
        set_ad_mode(chat_id, False)
        run_ad_index()
        total = count_ad_examples()
        send_message(
            chat_id,
            f'Режим рекламы выключен. Всего примеров: {total}.\n'
            + ('Последние фото проиндексированы.' if was else 'Новых фото в этом режиме не было.'),
        )
        notify_owner(f'Ad examples updated: {total} files (chat {chat_id}).')
        return
    if cmd in ('/ad_status',):
        if not is_owner(chat_id):
            send_message(chat_id, 'Статус /ad — только для владельца.')
            return
        send_message(
            chat_id,
            f'Режим /ad: {"включён" if is_ad_mode(chat_id) else "выключен"}. '
            f'Примеров в базе: {count_ad_examples()}.',
        )
        return
    if cmd == '/ping':
        send_message(
            chat_id,
            f'Бот на связи ({BOT_VERSION}).\n'
            f'chat_id={chat_id}\n'
            f'владелец={"да" if is_owner(chat_id) else "нет"} '
            f'PUBG={"да" if is_pubg_chat(chat_id) else "нет"}',
        )
        return
    if cmd in ('/ig_cookies', '/ig_cookie', '/instagram_cookies'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Только для владельца.')
            return
        send_message(
            chat_id,
            'Чтобы дайджест 12 MLBB-блогеров работал:\n'
            '1) В браузере зайдите в instagram.com\n'
            '2) Экспорт cookies (расширение Get cookies.txt LOCALLY / аналог) — формат Netscape\n'
            '3) Пришлите файл сюда как **документ** (cookies.txt)\n'
            '4) Напишите /ig_digest — запущу рассылку сразу\n\n'
            f'Сейчас на сервере: {"есть" if INSTAGRAM_COOKIES_PATH.exists() else "нет"} cookies.',
        )
        return
    if cmd in ('/ig_digest', '/instagram', '/digest'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Только для владельца.')
            return
        if not INSTAGRAM_COOKIES_PATH.exists():
            send_message(chat_id, 'Сначала пришлите cookies.txt (см. /ig_cookies).')
            return
        send_message(chat_id, 'Запускаю Instagram-дайджест (~7 постов, 2–5 мин)…')
        start_instagram_digest(chat_id)
        return
    if cmd in ('/research', '/исследование', '/delivery'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /research только для владельца.')
            return
        url = parse_research_url(text)
        if not url:
            send_message(chat_id, research_help_text())
            return
        send_message(chat_id, 'Скачиваю файл по ссылке на сервер (может занять несколько минут)…')
        try:
            saved = save_research_from_url(url, chat_id)
            send_message(
                chat_id,
                f'Файл на сервере: {saved.name}. Запускаю анализ доставки — отчёт пришлю сюда.',
            )
            notify_owner(f'Research URL saved: {saved}')
            start_research_analysis()
        except Exception as exc:
            logging.exception('research url download failed')
            send_message(chat_id, f'Не удалось скачать по ссылке: {exc}\n\n{research_help_text()}')
        return
    if text.startswith('/status'):
        if limited:
            return
        send_message(chat_id, f'В очереди сейчас {count_pending(chat_id)} видео. Можно отправлять еще файлы или запускать /make.')
        return
    if text.startswith('/clear'):
        clear_pending(chat_id)
        if not limited:
            send_message(chat_id, 'Очередь очищена. Можешь присылать новые видео.')
        return
    if text.startswith('/make'):
        if limited:
            return
        if count_pending(chat_id) == 0:
            send_message(chat_id, 'Сначала пришли хотя бы одно видео.')
            return
        start_processing(chat_id)
        return

    photo = extract_photo(message)
    if photo:
        cap_cmd = command_token(safe_label(message.get('caption')))
        if is_wm_mode(chat_id) or (
            is_owner(chat_id) and cap_cmd in ('/wm_test', '/test_wm', '/wm')
        ):
            process_wm_photo(chat_id, message)
            return
        if is_ad_mode(chat_id):
            saved = save_ad_photo(chat_id, message)
            if saved:
                run_ad_index()
                send_message(
                    chat_id,
                    f'Сохранил пример рекламы ({saved.name}). Всего: {count_ad_examples()}. '
                    'Ещё фото или /ad_done',
                )
            else:
                send_message(chat_id, 'Не удалось сохранить фото.')
            return
        if is_reject_mode(chat_id):
            saved = save_reject_photo(chat_id, message)
            if saved:
                send_message(
                    chat_id,
                    f'Сохранил плохой пример ({saved.name}). Всего: {count_reject_examples()}. '
                    'Ещё фото или /bad_done',
                )
            else:
                send_message(chat_id, 'Не удалось сохранить фото.')
            return
        if not limited:
            send_message(
                chat_id,
                'Чтобы отправить скрины рекламы для обучения бота, сначала напиши /ad (или /реклама), '
                'потом пришли фото. Завершить — /ad_done.',
            )
        return

    # Owner: research xlsx / zip, or plain https link (transfer.sh etc.)
    if is_owner(chat_id) and text and 'http' in text and not text.startswith('/'):
        url = parse_research_url(text)
        if url and looks_like_research_url(url):
            send_message(chat_id, 'Вижу ссылку — качаю на сервер…')
            try:
                saved = save_research_from_url(url, chat_id)
                send_message(chat_id, f'Сохранено: {saved.name}. Запускаю анализ…')
                start_research_analysis()
            except Exception as exc:
                send_message(chat_id, f'Ошибка загрузки: {exc}\n\n{research_help_text()}')
            return

    doc = extract_document_file(message)
    if doc and is_owner(chat_id) and (
        doc.get('ext') == '.txt' and 'cookie' in (doc.get('file_name') or '').lower()
        or doc.get('file_name', '').lower() in ('cookies.txt', 'instagram_cookies.txt')
    ):
        try:
            save_instagram_cookies(message)
            send_message(chat_id, 'Cookies сохранены. Запускаю дайджест…')
            start_instagram_digest(chat_id)
        except Exception as exc:
            send_message(chat_id, f'Не удалось сохранить cookies: {exc}')
        return

    if doc and is_owner(chat_id) and doc.get('ext') == '.zip':
        RESEARCH_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        zip_dest = RESEARCH_INBOX_DIR / f'{stamp}_upload.zip'
        try:
            download_file(get_file_url(doc['file_id']), zip_dest)
            saved = extract_xlsx_from_zip(zip_dest, chat_id)
            if saved:
                send_message(chat_id, f'Из ZIP извлечён: {saved.name}. Запускаю анализ…')
                start_research_analysis()
            else:
                send_message(chat_id, 'В ZIP нет .xlsx. Пришлите ссылку transfer.sh — см. /research')
        except Exception as exc:
            send_message(chat_id, f'ZIP не скачался (часто >20 МБ): {exc}\n\n{research_help_text()}')
        return

    if doc and is_owner(chat_id) and (doc.get('ext') == '.xlsx' or 'spreadsheet' in (doc.get('mime_type') or '')):
        saved = save_research_file(chat_id, message)
        if saved and saved.exists():
            send_message(chat_id, f'Файл сохранён: {saved.name}. Запускаю анализ доставки…')
            notify_owner(f'Research file saved: {saved}')
            start_research_analysis()
        else:
            send_message(chat_id, research_help_text())
        return

    media = extract_media(message)
    if not media:
        if text and cmd not in ('/start',):
            send_message(
                chat_id,
                'Команды: /ping, /ad, /wm (убрать god of mlbb), /ig_digest, /start. '
                'Видео — файлом. Если /wm не находится — на VPS нужен деплой бота (см. scripts/deploy_telegram_bot.sh).',
            )
        return

    pending_dir = chat_pending_dir(chat_id)
    file_url = get_file_url(media['file_id'])
    destination = pending_dir / f"{int(time.time())}_{media['file_unique_id']}{media['ext']}"
    download_file(file_url, destination)
    label = game_label_for_chat(chat_id, caption)
    append_pending(chat_id, destination, label)
    spawn_pubg_learning(destination, chat_id)
    pending_count = count_pending(chat_id)
    if chat_id in AUTO_MAKE_CHAT_IDS:
        send_upload_status(chat_id, pending_count)
        start_processing(chat_id, only_paths=[destination])
        return
    send_upload_status(chat_id, pending_count)


def main():
    api_call('deleteWebhook', {'drop_pending_updates': False}, timeout=30)
    register_bot_commands()
    state = _bot_state()
    me = api_call('getMe', timeout=30)
    logging.info(
        'telegram upload bot started %s as @%s owner_chat=%s allowed=%s',
        BOT_VERSION,
        me.get('username'),
        DEFAULT_CHAT_ID or '(empty)',
        sorted(ALLOWED_CHAT_IDS),
    )
    while True:
        try:
            updates = api_call(
                'getUpdates',
                {
                    'offset': state.get('last_update_id', 0) + 1,
                    'timeout': POLL_TIMEOUT,
                    'allowed_updates': ['message'],
                },
                timeout=POLL_TIMEOUT + 10,
            )
            for update in updates:
                state['last_update_id'] = update['update_id']
                save_state(state)
                message = update.get('message')
                if message:
                    handle_message(message)
        except Exception:
            logging.exception('poll loop error')
            time.sleep(5)


if __name__ == '__main__':
    main()
