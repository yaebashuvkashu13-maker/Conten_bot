#!/usr/bin/env python3
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

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
AD_EXAMPLES_DIR = Path('/root/data/mlbb/ad_examples')
POLL_TIMEOUT = 25
AD_MODE_TIMEOUT_SEC = 3600

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
        return {'last_update_id': 0, 'ad_mode_until': {}}
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {'last_update_id': 0, 'ad_mode_until': {}}
    state.setdefault('ad_mode_until', {})
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
    return [f"{path}|{labels.get(path, 'Telegram upload')}|{chat_id}" for path in paths]


def _smart_edit_failure_hint(code: int, log_tail: str) -> str:
    if 'no usable sources' in log_tail:
        return 'Файл не прочитался (битый или не видео).'
    if 'no candidates' in log_tail or 'produced no candidates' in log_tail:
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
            lines = [line.strip() for line in queue_path.read_text().splitlines() if line.strip()]
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
            send_message(
                chat_id,
                'Принял задачу. Запускаю Smart Edit v1.1: выберу 3-4 хайлайта и соберу ролик примерно на 33-57 секунд.',
            )
        with tempfile.NamedTemporaryFile('w', delete=False, prefix=f'tg-batch-{chat_id}-', suffix='.txt') as tmp_queue:
            tmp_queue.write('\n'.join(lines) + '\n')
            tmp_queue_path = tmp_queue.name

        run_env = os.environ.copy()
        run_env['QUEUE_FILE'] = tmp_queue_path
        run_env['MAX_SOURCES'] = str(len(lines))
        run_env.setdefault('TARGET_DURATION', env.get('SMART_TARGET_DURATION', '40'))
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
            err_hint = _smart_edit_failure_hint(completed.returncode, log_tail)
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


def handle_message(message: dict):
    chat = message.get('chat', {})
    chat_id = str(chat.get('id', ''))
    if not chat_id:
        return
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logging.warning('ignoring unauthorized chat %s', chat_id)
        return

    text = (message.get('text') or '').strip()
    cmd = command_token(text)
    caption = safe_label(message.get('caption'))
    limited = is_limited_notify(chat_id)

    if cmd == '/start' or text.startswith('/start'):
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
                'Примеры рекламы (скрины): команда /ad — затем фото, завершить /ad_done.',
            )
        return
    if cmd in ('/ad', '/реклама', '/ads'):
        set_ad_mode(chat_id, True)
        send_message(
            chat_id,
            'Режим примеров рекламы включён на 1 час.\n'
            'Пришли скрины (фото) — сохраню для обучения фильтра «не слать такое».\n'
            'Когда закончишь: /ad_done\n'
            f'Сейчас в базе: {count_ad_examples()} шт.',
        )
        return
    if cmd in ('/ad_done', '/ad_stop', '/реклама_готово'):
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
        send_message(
            chat_id,
            f'Режим /ad: {"включён" if is_ad_mode(chat_id) else "выключен"}. '
            f'Примеров в базе: {count_ad_examples()}.',
        )
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
        if not limited:
            send_message(
                chat_id,
                'Чтобы отправить скрины рекламы для обучения бота, сначала напиши /ad (или /реклама), '
                'потом пришли фото. Завершить — /ad_done.',
            )
        return

    media = extract_media(message)
    if not media:
        return

    pending_dir = chat_pending_dir(chat_id)
    file_url = get_file_url(media['file_id'])
    destination = pending_dir / f"{int(time.time())}_{media['file_unique_id']}{media['ext']}"
    download_file(file_url, destination)
    append_pending(chat_id, destination, caption)
    pending_count = count_pending(chat_id)
    if chat_id in AUTO_MAKE_CHAT_IDS:
        send_upload_status(chat_id, pending_count)
        start_processing(chat_id, only_paths=[destination])
        return
    send_upload_status(chat_id, pending_count)


def main():
    api_call('deleteWebhook', {'drop_pending_updates': False}, timeout=30)
    state = _bot_state()
    logging.info('telegram upload bot started')
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
