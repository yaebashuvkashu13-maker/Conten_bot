#!/usr/bin/env python3
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from source_freshness import filter_new_sources, mark_used, prune_used_from_queue_file
except ImportError:
    sys.path.insert(0, '/usr/local/bin')
    from source_freshness import filter_new_sources, mark_used, prune_used_from_queue_file

ENV_FILE = Path('/root/.video_bot.env')
LOG_FILE = Path('/root/telegram_upload_bot.log')


def _ensure_scripts_on_path() -> None:
    repo = Path(os.environ.get('CONTENT_BOT_REPO', '/root/content_bot_ml'))
    for candidate in (Path(__file__).resolve().parent, repo / 'scripts'):
        path = str(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)


_ensure_scripts_on_path()
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
STANDOFF_EXEMPLAR_MODE_TIMEOUT_SEC = 7200
VK_MLBB_UPLOAD_MODE_TIMEOUT_SEC = 7 * 86400
BOT_VERSION = '2026-08-27-tg-process-reset-v1'
TELEGRAM_BOT_MAX_BYTES = 20 * 1024 * 1024  # Bot API getFile limit
RESEARCH_ANALYSIS = Path('/usr/local/bin/research_delivery_analysis.py')
INSTAGRAM_COOKIES_PATH = Path('/root/instagram_cookies.txt')
INSTAGRAM_DIGEST_RUN = Path('/usr/local/bin/instagram_digest_run.sh')
PROFILE_LABELS = {
    'pubg': 'PUBG Mobile',
    'mobile_legends': 'Mobile Legends',
    'mlbb': 'Mobile Legends',
    'genshin': 'Genshin Impact',
    'standoff': 'Standoff 2',
    'wot': 'WoT PC',
    'world_of_tanks': 'World of Tanks',
}

STRICT_MONTAGE_PROFILES = frozenset({
    'pubg',
    'standoff',
    'mobile_legends',
    'genshin',
    'wot',
    'world_of_tanks',
    'mlbb',
})

PIPELINE_INBOX = Path('/root/data/mlbb/youtube_nightly/inbox')
MAKE_PROFILE_OVERRIDES: dict[str, str] = {}
PROFILE_ALIASES = {
    'pubg': 'pubg',
    'standoff': 'standoff',
    'standoff2': 'standoff',
    'стендоф': 'standoff',
    'стендофф': 'standoff',
    'mlbb': 'mobile_legends',
    'mobile_legends': 'mobile_legends',
    'genshin': 'genshin',
    'wot': 'wot',
    'world_of_tanks': 'wot',
}
OWNER_LABEL_FILES = {
    'pubg': 'pubg_owner_labels.json',
    'standoff': 'standoff_owner_labels.json',
    'mobile_legends': 'mobile_legends_owner_labels.json',
    'genshin': 'genshin_owner_labels.json',
    'wot': 'wot_owner_labels.json',
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


from telegram_access import chat_is_allowed as _chat_is_allowed_core
from telegram_access import is_owner as _is_owner_core


def is_owner(chat_id: str) -> bool:
    return _is_owner_core(
        chat_id,
        DEFAULT_CHAT_ID,
        env.get("AD_OWNER_CHAT_IDS", env.get("OWNER_CHAT_IDS", "")),
    )


def chat_is_allowed(chat_id: str) -> bool:
    """Owner (TG_CHAT_ID) always allowed; others only if listed in TG_ALLOWED_CHAT_IDS."""
    return _chat_is_allowed_core(
        chat_id,
        owner_chat_id=DEFAULT_CHAT_ID,
        allowed_chat_ids=ALLOWED_CHAT_IDS,
        extra_owner_ids=env.get("AD_OWNER_CHAT_IDS", env.get("OWNER_CHAT_IDS", "")),
    )


def is_pubg_chat(chat_id: str) -> bool:
    return game_profile_for_chat(chat_id) == 'pubg'


def normalize_montage_profile(profile: str) -> str:
    p = profile.strip().lower()
    if p == 'mlbb':
        return 'mobile_legends'
    if p == 'world_of_tanks':
        return 'wot'
    return p


def _repo_data_dir() -> Path:
    repo = Path(os.environ.get('CONTENT_BOT_REPO', '/root/content_bot_ml'))
    data = repo / 'data'
    if data.exists():
        return data
    local = Path(__file__).resolve().parent.parent / 'data'
    return local if local.exists() else data


def youtube_id_from_upload(path: Path) -> str | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from pipeline_inbox import youtube_id_from_name

        return youtube_id_from_name(path.name)
    except ImportError:
        stem = path.stem
        if '_youtube_' in stem:
            return stem.rsplit('_youtube_', 1)[-1]
        if stem.startswith('yt_'):
            return stem[3:]
        return None


def profile_from_owner_labels(path: Path) -> str | None:
    vid = youtube_id_from_upload(path)
    if not vid:
        return None
    data_dir = _repo_data_dir()
    for profile, fname in OWNER_LABEL_FILES.items():
        labels_path = data_dir / fname
        if not labels_path.exists():
            continue
        try:
            payload = json.loads(labels_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        if vid in payload.get('videos', {}):
            return profile
    return None


def caption_implies_profile(label: str | None) -> str | None:
    if not label:
        return None
    low = label.lower()
    if 'standoff' in low or 'стендоф' in low:
        return 'standoff'
    if 'pubg' in low or 'пабг' in low or 'metro' in low:
        return 'pubg'
    if 'genshin' in low or 'геншин' in low:
        return 'genshin'
    if 'mlbb' in low or 'mobile legends' in low or 'mobile_legends' in low:
        return 'mobile_legends'
    if 'wot' in low or 'world of tanks' in low or 'танк' in low:
        return 'wot'
    return None


def mirror_upload_to_pipeline_inbox(source: Path) -> Path | None:
    """Copy owner uploads into nightly inbox so the queue can pick them up."""
    if not source.exists():
        return None
    PIPELINE_INBOX.mkdir(parents=True, exist_ok=True)
    vid = youtube_id_from_upload(source)
    dest = PIPELINE_INBOX / (f'yt_{vid}.mp4' if vid else source.name)
    try:
        if dest.exists() and dest.stat().st_size >= source.stat().st_size * 0.98:
            return dest
        shutil.copy2(source, dest)
        logging.info('mirrored upload to inbox %s -> %s', source.name, dest.name)
        return dest
    except OSError as exc:
        logging.warning('inbox mirror failed %s: %s', source, exc)
        return None


def resolve_montage_profile(
    chat_id: str,
    lines: list[str],
    *,
    forced: str | None = None,
) -> str:
    if forced:
        alias = PROFILE_ALIASES.get(forced.strip().lower(), forced.strip().lower())
        return normalize_montage_profile(alias)

    mapped = game_profile_for_chat(chat_id)
    if mapped:
        return normalize_montage_profile(mapped)

    default_profile = env.get('DEFAULT_GAME_PROFILE', '').strip().lower()
    if default_profile:
        return normalize_montage_profile(default_profile)

    for line in lines:
        parts = line.split('|', 2)
        label = parts[1] if len(parts) > 1 else ''
        path = Path(parts[0])
        from_label = caption_implies_profile(label)
        if from_label:
            return from_label
        from_labels = profile_from_owner_labels(path)
        if from_labels:
            return from_labels

    return 'mobile_legends'


def build_strict_montage_env(profile: str) -> dict[str, str]:
    """highlight_scorer + strict_peak + owner preview — no smart_video_editor."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import strict_peak_env

    prof = normalize_montage_profile(profile)
    run_env = dict(env)
    run_env.update(strict_peak_env(prof))
    run_env.update({
        'HIGHLIGHT_SCORER': '1',
        'HIGHLIGHT_USE_OWNER_ANCHORS': '0',
        'SEND_TELEGRAM': '0',
        'OWNER_PREVIEW_REQUIRED': '1',
        'HIGHLIGHT_CLIP_DISABLED': '0',
        'INTELLICLIP_FUSION': '0',
        'DEFAULT_GAME_PROFILE': prof,
        'QUEUE_GAME_PROFILE': prof,
        'TG_BOT_TOKEN': env.get('TG_BOT_TOKEN', ''),
        'TG_CHAT_ID': env.get('TG_CHAT_ID', ''),
    })
    return run_env


def run_strict_montage_for_source(
    chat_id: str,
    source_path: Path,
    profile: str,
    caption: str,
) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from strict_montage_direct import make_strict_montage

    prof = normalize_montage_profile(profile)
    run_env = build_strict_montage_env(prof)
    for key, val in run_env.items():
        os.environ[key] = str(val)
    basename = f'tg_{prof}_{chat_id}_{int(time.time())}'
    return make_strict_montage(
        profile=prof,
        vod=source_path,
        output_basename=basename,
        caption=caption,
        env=run_env,
    )


def _extract_preview_id(detail: str) -> str:
    match = re.search(r'preview_id=(\S+?)(?:,|$)', detail)
    return match.group(1) if match else ''


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
YOUTUBE_DOWNLOAD_CHATS: set[str] = set()
YOUTUBE_DOWNLOAD_LOCK = threading.Lock()
YOUTUBE_PENDING_QUEUES: dict[str, list[dict[str, str]]] = {}
_TELEGRAM_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def telegram_urlopen(request: urllib.request.Request, timeout: int = 60):
    """Telegram API must not use HTTP_PROXY from .video_bot.env (dead CyberYozh IP)."""
    return _TELEGRAM_OPENER.open(request, timeout=timeout)


def api_call(method: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload or {}).encode('utf-8')
    request = urllib.request.Request(
        f'{API_BASE}/{method}',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    with telegram_urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not result.get('ok'):
        raise RuntimeError(f'Telegram API error for {method}: {result}')
    return result['result']


def is_limited_notify(chat_id: str | int) -> bool:
    return str(chat_id) in LIMITED_NOTIFY_CHAT_IDS


def send_message(chat_id: str | int, text: str, reply_markup: dict | None = None):
    payload: dict = {'chat_id': str(chat_id), 'text': text}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        api_call('sendMessage', payload, timeout=30)
    except Exception as exc:
        logging.error('failed to send message to %s: %s', chat_id, exc)


def send_owner_controls(chat_id: str | int, text: str, *, inline: bool = False):
    from telegram_owner_controls import owner_reply_keyboard, process_inline_keyboard

    markup = {**owner_reply_keyboard()}
    if inline:
        markup = process_inline_keyboard()
    send_message(chat_id, text, reply_markup=markup)


def _schedule_owner_sync() -> None:
    """Refresh pending owner_score after 👍/👎 (non-blocking)."""
    code = (
        "import sys; sys.path.insert(0,'/usr/local/bin');"
        "sys.path.insert(0,'/root/content_bot_ml/scripts');"
        "from mlbb_calibration_store import sync_owner_learning; "
        "print(sync_owner_learning(rescore_limit=int(__import__('os').environ.get('MLBB_RESCORE_ON_LABEL','30'))))"
    )
    try:
        subprocess.Popen(
            [sys.executable, '-c', code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        logging.exception('mlbb owner sync schedule failed')


def _schedule_mlbb_retrain() -> None:
    """Retrain MLBB scorer from owner 👍/👎 without blocking Telegram."""
    _schedule_owner_sync()
    script = Path('/usr/local/bin/mlbb_learn_apply.sh')
    if not script.exists():
        script = Path(__file__).resolve().parent / 'mlbb_learn_apply.sh'
    if not script.exists():
        return
    try:
        subprocess.Popen(
            ['bash', str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        logging.exception('mlbb retrain schedule failed')


def _mlbb_apply_vseg_label(
    chat_id: str | int,
    segment_id: str,
    *,
    is_good: bool,
    reason: str = '',
) -> tuple[bool, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mlbb_vod_segment_store import apply_owner_label, stats

    ok, _label = apply_owner_label(
        segment_id.strip(),
        is_good=is_good,
        reason=reason,
        by_chat=str(chat_id),
    )
    s = stats()
    if not ok:
        return False, f'Не нашёл кусок {segment_id}. Запусти /mlbb_vod'
    _schedule_mlbb_retrain()
    if is_good:
        return True, (
            f'✅ Ок — кусок {segment_id.strip()}\n'
            f'Всего VOD: 👍{s["feedback_yes"]} 👎{s["feedback_no"]}'
        )
    from mlbb_learning_first import dislike_feedback_report
    from mlbb_vod_segment_store import find_segment

    row = find_segment(segment_id.strip()) or {}
    peak = float(row.get('peak_start') or row.get('start') or 0)
    vid = str(row.get('vod_id') or segment_id.strip().rsplit('_', 1)[0])
    owner_report = dislike_feedback_report(
        segment_id.strip(),
        vod_id=vid,
        peak_sec=peak,
        reason=reason,
    )
    notify_owner(owner_report)
    return True, (
        f'❌ Не ок — кусок {segment_id.strip()}\n'
        f'Причина: {reason or "—"}\n'
        f'Всего VOD: 👍{s["feedback_yes"]} 👎{s["feedback_no"]}\n\n'
        f'{owner_report}'
    )


def _shooter_apply_vseg_label(
    game: str,
    chat_id: str | int,
    segment_id: str,
    *,
    is_good: bool,
    reason: str = '',
) -> tuple[bool, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from shooter_vod_segment_store import apply_owner_label, stats

    game = game.strip().lower()
    sid = segment_id.strip()
    ok, _label = apply_owner_label(
        game,
        sid,
        is_good=is_good,
        reason=reason,
        by_chat=str(chat_id),
    )
    s = stats(game)
    if not ok:
        return False, f'Не нашёл {game.upper()} кусок {sid}.'
    from daily_game_cycle import profile_for_game
    from vod_owner_learning import exemplar_counts

    profile = profile_for_game(game)
    good_n, bad_n = exemplar_counts(profile)
    if is_good:
        return True, (
            f'✅ Ок — {game.upper()} #{sid}\n'
            f'Всего: 👍{s["feedback_yes"]} 👎{s["feedback_no"]}\n'
            f'exemplars: 👍{good_n} 👎{bad_n}'
        )
    return True, (
        f'❌ Не ок — {game.upper()} #{sid}\n'
        f'Причина: {reason or "—"}\n'
        f'Всего: 👍{s["feedback_yes"]} 👎{s["feedback_no"]}\n'
        f'exemplars: 👍{good_n} 👎{bad_n}'
    )


def _shooter_vseg_hq_path(game: str, segment_id: str) -> tuple[Path | None, dict]:
    from shooter_vod_segment_store import _paths, find_segment

    game = game.strip().lower()
    sid = segment_id.strip()
    row = find_segment(game, sid) or {}
    if row:
        path = Path(str(row.get('path', '')))
        if path.exists():
            return path, row
    direct = _paths(game)['segments'] / f'seg_{sid}.mp4'
    if direct.exists():
        merged = {**row, 'segment_id': sid, 'path': str(direct)}
        return direct, merged
    return None, row


def _shooter_send_vseg_hq_file(game: str, chat_id: str | int, segment_id: str) -> bool:
    from mlbb_telegram_video import send_hq_files

    game = game.strip().lower()
    sid = segment_id.strip()
    path, row = _shooter_vseg_hq_path(game, sid)
    if path is None:
        logging.warning('shooter HQ missing seg=%s game=%s', sid, game)
        return False
    caption = (
        f'{game.upper()} HQ файл #{sid}\n'
        f"VOD {row.get('vod_id') or sid.rsplit('_', 1)[0]}\n"
        f"peak={row.get('peak_start') or row.get('start', '?')}s\n"
        f'📁 скачай файл — без пережатия Telegram'
    )
    ok = send_hq_files(
        BOT_TOKEN,
        str(chat_id),
        path,
        caption,
        filename=f'{game.upper()}_{sid}.mp4',
    )
    if not ok:
        logging.warning('shooter HQ send failed seg=%s game=%s path=%s', sid, game, path)
    return ok


def _handle_shooter_vseg_callback(
    game: str,
    data: str,
    *,
    chat_id: str | int,
    message_id: int,
    query_id: str,
) -> bool:
    """Handle pubg_vseg_* / standoff_vseg_* callbacks. Returns True if handled."""
    prefix = f'{game.strip().lower()}_vseg'
    if not data.startswith(f'{prefix}_'):
        return False

    if data.startswith(f'{prefix}_hq:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            ok = _shooter_send_vseg_hq_file(game, chat_id, item_id)
            api_call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query_id,
                    'text': 'HQ файл отправлен' if ok else 'Не удалось отправить HQ',
                    'show_alert': not ok,
                },
                timeout=15,
            )
            if not ok:
                send_message(
                    chat_id,
                    f'HQ файл для {game.upper()} #{item_id} не отправился (нет файла на диске).',
                )
        except Exception as exc:
            logging.exception('%s_vseg_hq callback failed data=%s', game, data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return True

    if data.startswith(f'{prefix}_bad:'):
        try:
            _, item_id, reason = data.split(':', 2)
        except ValueError:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
            return True
        from calibration_dislike_reasons import dislike_reason_codes
        from shooter_vod_segment_store import labeled_keyboard_markup as shooter_markup

        if reason not in dislike_reason_codes(game):
            reason = 'other'
        try:
            ok, reply = _shooter_apply_vseg_label(
                game, chat_id, item_id, is_good=False, reason=reason
            )
            if not ok:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                    timeout=15,
                )
                return True
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': '❌ Записано'},
                timeout=15,
            )
            api_call(
                'editMessageReplyMarkup',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'reply_markup': shooter_markup(game, 'bad', reason=reason),
                },
                timeout=15,
            )
        except Exception as exc:
            logging.exception('%s_vseg_bad callback failed data=%s', game, data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return True

    is_good: bool | None = None
    item_id = ''
    reason = ''
    if data.startswith(f'{prefix}_yes:'):
        is_good = True
        item_id = data.split(':', 1)[1].strip()
    elif data.startswith(f'{prefix}_no:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            _show_dislike_reason_picker(
                chat_id=chat_id,
                message_id=message_id,
                query_id=query_id,
                item_id=item_id,
                callback_prefix=f'{prefix}_bad',
                game=game,
            )
        except Exception as exc:
            logging.exception('%s_vseg_no picker failed seg=%s', game, item_id)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return True
    else:
        return False

    try:
        ok, reply = _shooter_apply_vseg_label(
            game, chat_id, item_id, is_good=is_good, reason=reason
        )
        from shooter_vod_segment_store import labeled_keyboard_markup as shooter_markup

        markup = shooter_markup(
            game,
            'good' if is_good else 'bad',
            reason=reason,
            segment_id=item_id if is_good else '',
        )
        alert = '✅ Ок' if is_good else '❌ Не ок'
        if not ok:
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                timeout=15,
            )
            return True
        api_call(
            'answerCallbackQuery',
            {'callback_query_id': query_id, 'text': alert},
            timeout=15,
        )
        api_call(
            'editMessageReplyMarkup',
            {
                'chat_id': chat_id,
                'message_id': message_id,
                'reply_markup': markup,
            },
            timeout=15,
        )
        if is_good:
            hq_ok = _shooter_send_vseg_hq_file(game, chat_id, item_id)
            if not hq_ok:
                send_message(
                    chat_id,
                    f'⚠️ {game.upper()} HQ файл #{item_id} не отправился — нажми 📁 HQ файл ещё раз.',
                )
    except Exception as exc:
        logging.exception('%s_vseg_yes callback failed data=%s', game, data)
        api_call(
            'answerCallbackQuery',
            {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
            timeout=15,
        )
    return True


def _mlbb_apply_owner_label(
    chat_id: str | int,
    video_id: str,
    *,
    is_good: bool,
    reason: str = '',
) -> tuple[bool, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mlbb_calibration_store import apply_owner_label, stats

    ok, _label = apply_owner_label(
        video_id.strip(),
        is_good=is_good,
        reason=reason,
        by_chat=str(chat_id),
    )
    s = stats()
    if not ok:
        return False, f'Не нашёл id={video_id} в индексе Shorts. Сначала /mlbb_samples'
    _schedule_mlbb_retrain()
    if is_good:
        return True, (
            f'✅ Записал good exemplar #{video_id.strip()}\n'
            f'Всего: 👍{s["feedback_yes"]} 👎{s["feedback_no"]} | accuracy {s["accuracy"]:.0%}'
        )
    return True, (
        f'❌ Записал bad exemplar #{video_id.strip()}\n'
        f'Причина: {reason or "—"}\n'
        f'Всего: 👍{s["feedback_yes"]} 👎{s["feedback_no"]} | accuracy {s["accuracy"]:.0%}'
    )


def _mlbb_send_hq_file(chat_id: str | int, video_id: str) -> bool:
    from mlbb_calibration_store import find_candidate
    from mlbb_telegram_video import send_hq_files

    row = find_candidate(video_id)
    if not row:
        return False
    path = Path(str(row.get('path', '')))
    if not path.exists():
        return False
    caption = (
        f"MLBB HQ файл #{video_id}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {video_id}"
    )
    return send_hq_files(BOT_TOKEN, str(chat_id), path, caption)


def _mlbb_send_vseg_hq_file(chat_id: str | int, segment_id: str) -> bool:
    from mlbb_telegram_video import send_hq_files
    from mlbb_vod_segment_store import find_segment

    sid = segment_id.strip()
    row = find_segment(sid)
    if not row:
        return False
    path = Path(str(row.get('path', '')))
    if not path.exists():
        direct = Path(f"/root/datasets/mlbb/vod_segments/seg_{sid}.mp4")
        if direct.exists():
            path = direct
        else:
            return False
    caption = (
        f"MLBB HQ файл #{sid}\n"
        f"VOD {row.get('vod_id') or sid.rsplit('_', 1)[0]}\n"
        f"peak={row.get('peak_start') or row.get('start', '?')}s"
    )
    return send_hq_files(BOT_TOKEN, str(chat_id), path, caption)


def _show_dislike_reason_picker(
    *,
    chat_id: str | int,
    message_id: int,
    query_id: str,
    item_id: str,
    callback_prefix: str = 'mlbb_bad',
    game: str = '',
) -> None:
    """Second step after 👎 — edit markup or send a new picker message if edit fails."""
    from calibration_dislike_reasons import dislike_reason_keyboard_markup, normalize_game

    g = normalize_game(game or callback_prefix.split('_', 1)[0])
    markup = dislike_reason_keyboard_markup(item_id, game=g, callback_prefix=callback_prefix)
    try:
        api_call(
            'editMessageReplyMarkup',
            {
                'chat_id': chat_id,
                'message_id': message_id,
                'reply_markup': markup,
            },
            timeout=15,
        )
        api_call(
            'answerCallbackQuery',
            {'callback_query_id': query_id, 'text': 'Выбери причину 👇'},
            timeout=15,
        )
        return
    except Exception as exc:
        logging.warning('dislike picker edit failed id=%s: %s', item_id, exc)
    try:
        api_call(
            'sendMessage',
            {
                'chat_id': str(chat_id),
                'text': f'Причина дизлайка #{item_id}:',
                'reply_markup': markup,
            },
            timeout=15,
        )
        api_call(
            'answerCallbackQuery',
            {
                'callback_query_id': query_id,
                'text': 'Выбери причину в сообщении ниже 👇',
            },
            timeout=15,
        )
    except Exception as exc:
        logging.exception('dislike picker fallback failed id=%s', item_id)
        try:
            api_call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query_id,
                    'text': f'Ошибка: {exc}'[:180],
                    'show_alert': True,
                },
                timeout=15,
            )
        except Exception:
            pass


def _handle_game_shorts_callback(
    game: str,
    data: str,
    *,
    chat_id: str | int,
    message_id: int,
    query_id: str,
) -> bool:
    """Handle {game}_yes / {game}_no / {game}_bad / {game}_hq for Shorts calibration."""
    from calibration_dislike_reasons import (
        dislike_reason_codes,
        labeled_keyboard_markup,
        normalize_game,
    )

    g = normalize_game(game)
    prefix = g
    if not data.startswith(f'{prefix}_'):
        return False

    if data == f'{prefix}_noop':
        api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
        return True

    if data.startswith(f'{prefix}_hq:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            if g == 'mlbb':
                ok = _mlbb_send_hq_file(chat_id, item_id)
            else:
                from game_shorts_calibration import _paths

                path = _paths(g)['shorts'] / f'yt_{item_id}.mp4'
                ok = path.exists() and send_hq_files(
                    BOT_TOKEN, str(chat_id), path, f'{g.upper()} HQ #{item_id}'
                )
            api_call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query_id,
                    'text': 'HQ файл отправлен' if ok else 'Не удалось отправить HQ',
                    'show_alert': not ok,
                },
                timeout=15,
            )
        except Exception as exc:
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': str(exc)[:180], 'show_alert': True},
                timeout=15,
            )
        return True

    if data.startswith(f'{prefix}_bad:'):
        try:
            _, item_id, reason = data.split(':', 2)
        except ValueError:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
            return True
        if reason not in dislike_reason_codes(g):
            reason = 'other'
        try:
            if g == 'mlbb':
                ok, reply = _mlbb_apply_owner_label(
                    chat_id, item_id, is_good=False, reason=reason
                )
            else:
                from game_shorts_calibration import apply_shorts_label

                ok, reply = apply_shorts_label(
                    g, item_id, is_good=False, reason=reason, by_chat=str(chat_id)
                )
            if not ok:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                    timeout=15,
                )
                return True
            api_call('answerCallbackQuery', {'callback_query_id': query_id, 'text': '❌ Записано'}, timeout=15)
            api_call(
                'editMessageReplyMarkup',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'reply_markup': labeled_keyboard_markup(g, 'bad', reason=reason),
                },
                timeout=15,
            )
        except Exception as exc:
            logging.exception('%s_bad callback failed', g)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': str(exc)[:180], 'show_alert': True},
                timeout=15,
            )
        return True

    if data.startswith(f'{prefix}_yes:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            if g == 'mlbb':
                ok, reply = _mlbb_apply_owner_label(chat_id, item_id, is_good=True)
            else:
                from game_shorts_calibration import apply_shorts_label

                ok, reply = apply_shorts_label(
                    g, item_id, is_good=True, by_chat=str(chat_id)
                )
            if not ok:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                    timeout=15,
                )
                return True
            api_call('answerCallbackQuery', {'callback_query_id': query_id, 'text': '✅ Записано'}, timeout=15)
            api_call(
                'editMessageReplyMarkup',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'reply_markup': labeled_keyboard_markup(g, 'good', video_id=item_id),
                },
                timeout=15,
            )
        except Exception as exc:
            logging.exception('%s_yes callback failed', g)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': str(exc)[:180], 'show_alert': True},
                timeout=15,
            )
        return True

    if data.startswith(f'{prefix}_no:'):
        item_id = data.split(':', 1)[1].strip()
        _show_dislike_reason_picker(
            chat_id=chat_id,
            message_id=message_id,
            query_id=query_id,
            item_id=item_id,
            callback_prefix=f'{prefix}_bad',
            game=g,
        )
        return True

    return False


def handle_callback_query(query: dict) -> None:
    query_id = query.get('id')
    data = str(query.get('data') or '')
    message = query.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    message_id = message.get('message_id')
    if not query_id or chat_id is None or message_id is None:
        return
    if not is_owner(chat_id):
        try:
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': 'Нет доступа', 'show_alert': True},
                timeout=15,
            )
        except Exception:
            pass
        return

    if data == 'mlbb_noop':
        try:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
        except Exception:
            pass
        return

    if data in ('ops_process', 'ops_reset'):
        try:
            from telegram_owner_controls import format_process_report, run_reset

            if data == 'ops_process':
                api_call('answerCallbackQuery', {'callback_query_id': query_id, 'text': 'Процесс'}, timeout=15)
                send_owner_controls(chat_id, format_process_report())
            else:
                api_call('answerCallbackQuery', {'callback_query_id': query_id, 'text': 'Сброс'}, timeout=15)
                send_owner_controls(chat_id, run_reset('all'))
        except Exception as exc:
            logging.exception('ops callback failed')
            try:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                    timeout=15,
                )
            except Exception:
                pass
        return

    for shorts_game in ('mlbb', 'pubg', 'standoff', 'genshin', 'wot'):
        if _handle_game_shorts_callback(
            shorts_game,
            data,
            chat_id=chat_id,
            message_id=message_id,
            query_id=query_id,
        ):
            return

    for shooter_game in ('pubg', 'standoff'):
        if _handle_shooter_vseg_callback(
            shooter_game,
            data,
            chat_id=chat_id,
            message_id=message_id,
            query_id=query_id,
        ):
            return

    if data.startswith('mlbb_hq:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            ok = _mlbb_send_hq_file(chat_id, item_id)
            api_call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query_id,
                    'text': 'HQ файл отправлен' if ok else 'Не удалось отправить HQ',
                    'show_alert': not ok,
                },
                timeout=15,
            )
            if not ok:
                send_message(chat_id, f'HQ файл для #{item_id} не отправился (нет файла или >50MB).')
        except Exception as exc:
            logging.exception('mlbb_hq callback failed data=%s', data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return

    if data.startswith('mlbb_vseg_hq:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            ok = _mlbb_send_vseg_hq_file(chat_id, item_id)
            api_call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query_id,
                    'text': 'HQ файл отправлен' if ok else 'Не удалось отправить HQ',
                    'show_alert': not ok,
                },
                timeout=15,
            )
            if not ok:
                send_message(
                    chat_id,
                    f'HQ файл для #{item_id} не отправился (нет файла на диске).',
                )
        except Exception as exc:
            logging.exception('mlbb_vseg_hq callback failed data=%s', data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return

    if data.startswith('mlbb_bad:'):
        try:
            _, item_id, reason = data.split(':', 2)
        except ValueError:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
            return
        from mlbb_calibration_store import DISLIKE_REASON_CODES, labeled_keyboard_markup as shorts_markup

        if reason not in DISLIKE_REASON_CODES:
            reason = 'other'
        try:
            ok, reply = _mlbb_apply_owner_label(
                chat_id, item_id, is_good=False, reason=reason
            )
            if not ok:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                    timeout=15,
                )
                return
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': '❌ Записано'},
                timeout=15,
            )
            api_call(
                'editMessageReplyMarkup',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'reply_markup': shorts_markup('bad', reason=reason),
                },
                timeout=15,
            )
        except Exception as exc:
            logging.exception('mlbb_bad callback failed data=%s', data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return

    if data.startswith('mlbb_vseg_bad:'):
        try:
            _, item_id, reason = data.split(':', 2)
        except ValueError:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
            return
        from mlbb_calibration_store import DISLIKE_REASON_CODES
        from mlbb_vod_segment_store import labeled_keyboard_markup as vseg_markup

        if reason not in DISLIKE_REASON_CODES:
            reason = 'other'
        try:
            ok, reply = _mlbb_apply_vseg_label(
                chat_id, item_id, is_good=False, reason=reason
            )
            if not ok:
                api_call(
                    'answerCallbackQuery',
                    {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                    timeout=15,
                )
                return
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': '❌ Записано'},
                timeout=15,
            )
            api_call(
                'editMessageReplyMarkup',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'reply_markup': vseg_markup('bad', reason=reason),
                },
                timeout=15,
            )
        except Exception as exc:
            logging.exception('mlbb_vseg_bad callback failed data=%s', data)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return

    mode = ''
    is_good: bool | None = None
    item_id = ''
    reason = ''
    if data.startswith('mlbb_yes:'):
        mode = 'shorts'
        is_good = True
        item_id = data.split(':', 1)[1].strip()
    elif data.startswith('mlbb_no:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            _show_dislike_reason_picker(
                chat_id=chat_id,
                message_id=message_id,
                query_id=query_id,
                item_id=item_id,
            )
        except Exception as exc:
            logging.exception('mlbb_no picker failed video_id=%s', item_id)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return
    elif data.startswith('mlbb_vseg_yes:'):
        mode = 'vseg'
        is_good = True
        item_id = data.split(':', 1)[1].strip()
    elif data.startswith('mlbb_vseg_no:'):
        item_id = data.split(':', 1)[1].strip()
        try:
            _show_dislike_reason_picker(
                chat_id=chat_id,
                message_id=message_id,
                query_id=query_id,
                item_id=item_id,
                callback_prefix='mlbb_vseg_bad',
            )
        except Exception as exc:
            logging.exception('mlbb_vseg_no picker failed seg=%s', item_id)
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        return
    else:
        try:
            api_call('answerCallbackQuery', {'callback_query_id': query_id}, timeout=15)
        except Exception:
            pass
        return

    try:
        if mode == 'vseg':
            ok, reply = _mlbb_apply_vseg_label(chat_id, item_id, is_good=is_good, reason=reason)
            from mlbb_vod_segment_store import labeled_keyboard_markup as vseg_markup

            markup = vseg_markup(
                'good' if is_good else 'bad',
                reason=reason,
                segment_id=item_id if is_good else '',
            )
        else:
            ok, reply = _mlbb_apply_owner_label(chat_id, item_id, is_good=is_good, reason=reason)
            from mlbb_calibration_store import labeled_keyboard_markup as shorts_markup

            markup = shorts_markup('good' if is_good else 'bad', reason=reason, video_id=item_id)
        alert = '✅ Ок' if is_good else '❌ Не ок'
        if not ok:
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': reply[:180], 'show_alert': True},
                timeout=15,
            )
            return
        api_call(
            'answerCallbackQuery',
            {'callback_query_id': query_id, 'text': alert},
            timeout=15,
        )
        api_call(
            'editMessageReplyMarkup',
            {
                'chat_id': chat_id,
                'message_id': message_id,
                'reply_markup': markup,
            },
            timeout=15,
        )
        if mode == 'vseg' and is_good:
            hq_ok = _mlbb_send_vseg_hq_file(chat_id, item_id)
            if not hq_ok:
                send_message(
                    chat_id,
                    f'⚠️ MLBB HQ файл #{item_id} не отправился — нажми 📁 HQ файл ещё раз.',
                )
    except Exception as exc:
        try:
            api_call(
                'answerCallbackQuery',
                {'callback_query_id': query_id, 'text': f'Ошибка: {exc}'[:180], 'show_alert': True},
                timeout=15,
            )
        except Exception:
            pass


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
        return {
            'last_update_id': 0,
            'ad_mode_until': {},
            'reject_mode_until': {},
            'wm_mode_until': {},
            'standoff_exemplar_mode_until': {},
            'vk_mlbb_upload_mode_until': {},
        }
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {
            'last_update_id': 0,
            'ad_mode_until': {},
            'reject_mode_until': {},
            'wm_mode_until': {},
            'standoff_exemplar_mode_until': {},
            'vk_mlbb_upload_mode_until': {},
        }
    state.setdefault('ad_mode_until', {})
    state.setdefault('reject_mode_until', {})
    state.setdefault('wm_mode_until', {})
    state.setdefault('standoff_exemplar_mode_until', {})
    state.setdefault('vk_mlbb_upload_mode_until', {})
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


def is_standoff_exemplar_mode(chat_id: str) -> bool:
    until = _bot_state().get('standoff_exemplar_mode_until', {}).get(chat_id)
    if not until:
        return False
    if time.time() > float(until):
        _bot_state()['standoff_exemplar_mode_until'].pop(chat_id, None)
        save_state(_bot_state())
        return False
    return True


def set_standoff_exemplar_mode(chat_id: str, enabled: bool):
    state = _bot_state()
    state.setdefault('standoff_exemplar_mode_until', {})
    if enabled:
        state['standoff_exemplar_mode_until'][chat_id] = time.time() + STANDOFF_EXEMPLAR_MODE_TIMEOUT_SEC
    else:
        state['standoff_exemplar_mode_until'].pop(chat_id, None)
    save_state(state)


def is_vk_mlbb_upload_mode(chat_id: str) -> bool:
    until = _bot_state().get('vk_mlbb_upload_mode_until', {}).get(chat_id)
    if not until:
        return False
    if time.time() > float(until):
        _bot_state()['vk_mlbb_upload_mode_until'].pop(chat_id, None)
        save_state(_bot_state())
        return False
    return True


def set_vk_mlbb_upload_mode(chat_id: str, enabled: bool):
    state = _bot_state()
    state.setdefault('vk_mlbb_upload_mode_until', {})
    if enabled:
        state['vk_mlbb_upload_mode_until'][chat_id] = time.time() + VK_MLBB_UPLOAD_MODE_TIMEOUT_SEC
    else:
        state['vk_mlbb_upload_mode_until'].pop(chat_id, None)
    save_state(state)


def _vk_mlbb_queue_helpers():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vk_mlbb_queue import enqueue_video, pending_count  # noqa: WPS433

    return enqueue_video, pending_count


def enqueue_vk_mlbb_video(chat_id: str, source: Path, label: str) -> Path:
    enqueue_video, _ = _vk_mlbb_queue_helpers()
    return enqueue_video(source, chat_id=chat_id, label=label or 'MLBB')


def count_vk_mlbb_pending() -> int:
    _, pending_count_fn = _vk_mlbb_queue_helpers()
    return pending_count_fn()


def _standoff_exemplar_helpers():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from standoff_exemplar_ingest import (  # noqa: WPS433
        count_standoff_exemplars,
        import_recent_standoff_exemplars,
        save_standoff_exemplar_video,
        standoff_exemplar_dir,
    )

    return (
        count_standoff_exemplars,
        import_recent_standoff_exemplars,
        save_standoff_exemplar_video,
        standoff_exemplar_dir,
    )


def count_standoff_exemplar_clips() -> int:
    try:
        return _standoff_exemplar_helpers()[0]()
    except Exception as exc:
        logging.warning('count standoff exemplars failed: %s', exc)
        return 0


def import_owner_standoff_exemplars(chat_id: str, *, limit: int = 9) -> tuple[list[Path], list[Path]]:
    return _standoff_exemplar_helpers()[1](chat_id, limit=limit)


def store_standoff_exemplar_video(chat_id: str, source: Path, label: str) -> Path:
    save_fn = _standoff_exemplar_helpers()[2]
    return save_fn(source, chat_id=chat_id, label=label)


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
    if is_owner(chat_id):
        mirror_upload_to_pipeline_inbox(local_path)
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
    request = urllib.request.Request(file_url)
    if 'api.telegram.org' in file_url:
        response = telegram_urlopen(request, timeout=300)
    else:
        response = urllib.request.urlopen(request, timeout=300)
    with response, destination.open('wb') as handle:
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


def video_upload_help_text(for_owner: bool = False) -> str:
    lines = [
        'Видео не попало в очередь — /make не видит файлов.',
        'Лимит Telegram Bot API: бот скачивает только файлы до ~20 МБ.',
        'Ролик на 15 минут почти всегда больше — Telegram показывает превью, но бот файл не получает.',
        '',
        'Что сделать:',
        '1) Сжать или экспортировать короче (3–5 мин, до ~20 МБ) и отправить снова.',
        '2) Разбить на 2–4 части, потом /make.',
        '3) Отправить как «файл» (документ), не «видео» — лимит тот же.',
    ]
    if for_owner:
        lines.extend(
            [
                '',
                '4) Владельцу: YouTube / Shorts / youtu.be — одной строкой (скачаем на сервер).',
                '5) Или прямая ссылка на .mp4 (transfer.sh, catbox).',
                '6) Команда /yt <ссылка> — то же, что просто ссылка.',
            ]
        )
    return '\n'.join(lines)


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
    urls = extract_http_urls(text)
    return urls[0] if urls else None


def normalize_http_url(url: str) -> str:
    url = url.strip().rstrip('.,);')
    if not url:
        return url
    if url.startswith('//'):
        return 'https:' + url
    if re.match(r'^(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)/', url, re.I):
        return 'https://' + url
    return url


def extract_http_urls(text: str) -> list[str]:
    if not text:
        return []
    found = list(re.findall(r'https?://[^\s<>"\']+', text))
    found += re.findall(
        r'(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)/[^\s<>"\']+',
        text,
        flags=re.I,
    )
    out: list[str] = []
    seen: set[str] = set()
    for raw in found:
        url = normalize_http_url(raw)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_urls_from_message(message: dict) -> list[str]:
    """URLs from message text, caption, and Telegram entities (text_link)."""
    chunks: list[str] = []
    for key in ('text', 'caption'):
        val = (message.get(key) or '').strip()
        if val:
            chunks.append(val)
    urls: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        for url in extract_http_urls(chunk):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    text = (message.get('text') or message.get('caption') or '')
    for ent in message.get('entities') or []:
        if ent.get('type') == 'text_link' and ent.get('url'):
            url = str(ent['url']).rstrip('.,);')
            if url not in seen:
                seen.add(url)
                urls.append(url)
        elif ent.get('type') == 'url' and text:
            try:
                snippet = text[ent['offset'] : ent['offset'] + ent['length']]
            except (KeyError, TypeError):
                continue
            for url in extract_http_urls(snippet):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


def _youtube_host(host: str) -> str:
    host = host.lower().split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


def looks_like_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip().rstrip('.,);'))
    except ValueError:
        return False
    host = _youtube_host(parsed.netloc or '')
    if host not in {'youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com'}:
        return False
    path = (parsed.path or '/').lower()
    if host == 'youtu.be':
        return len(path) > 1 and path != '/'
    if path.startswith('/shorts/') or path.startswith('/live/'):
        return True
    if path.startswith('/watch') or path == '/watch':
        return bool(parsed.query and 'v=' in parsed.query)
    if path.startswith(('/embed/', '/v/')):
        return True
    if path.startswith(('/@', '/channel/', '/c/', '/user/', '/playlist')):
        return True
    return 'list=' in (parsed.query or '').lower()


def finalize_yt_work_file(work: Path) -> Path:
    """Return merged mp4 from yt-dlp work dir (handles video-only .f399 etc.)."""
    mp4s = sorted(work.glob('yt_*.mp4'), key=lambda p: p.stat().st_mtime, reverse=True)
    if mp4s:
        return mp4s[0]
    candidates = [
        p
        for p in work.iterdir()
        if p.name.startswith('yt_') and not p.name.endswith('.part') and p.is_file()
    ]
    if not candidates:
        raise RuntimeError('yt-dlp did not produce output file')
    src = max(candidates, key=lambda p: p.stat().st_mtime)
    if src.suffix.lower() == '.mp4':
        return src
    dest = work / f'{src.stem}.mp4'
    proc = subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-c', 'copy', str(dest)],
        capture_output=True,
        text=True,
        timeout=7200,
    )
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError((proc.stderr or 'ffmpeg remux failed')[:500])
    return dest


def _disk_free_gb(path: Path) -> float:
    try:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize / (1024**3)
    except OSError:
        return 0.0


def save_youtube_from_url(
    url: str,
    chat_id: str,
    label: str = '',
    *,
    source_url: str | None = None,
) -> Path:
    """Download YouTube via yt-dlp into pending queue (no 20MB Telegram limit)."""
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from youtube_download import (  # noqa: WPS433
        is_youtube_live_url,
        is_youtube_shorts_url,
        normalize_youtube_url,
        subprocess_env_no_proxy,
        ytdlp_extra_args,
        youtube_format_for_url,
    )

    raw_url = source_url or url
    url = normalize_youtube_url(url)
    pending_dir = chat_pending_dir(chat_id)
    if _disk_free_gb(pending_dir) < 0.4:
        raise RuntimeError('На сервере мало места на диске (<400 МБ). Освободите место и повторите.')

    stamp = time.strftime('%Y%m%d_%H%M%S')
    work = pending_dir / f'_yt_tmp_{stamp}'
    work.mkdir(parents=True, exist_ok=True)
    env = load_env(ENV_FILE)
    impersonate = env.get('YTDLP_IMPERSONATE', 'chrome-131')
    template = work / 'yt_%(id)s.%(ext)s'
    timeout = int(float(
        env.get(
            'YOUTUBE_SHORTS_TIMEOUT' if is_youtube_shorts_url(raw_url) else 'YOUTUBE_DOWNLOAD_TIMEOUT',
            '300' if is_youtube_shorts_url(raw_url) else '14400',
        )
    ))
    cmd = [
        'yt-dlp',
        '--impersonate', impersonate,
        '--no-warnings',
        '--no-progress',
        '--restrict-filenames',
        '--merge-output-format', 'mp4',
        '-f', youtube_format_for_url(raw_url, env),
        *ytdlp_extra_args(env),
        '-o', str(template),
    ]
    if looks_like_youtube_url(url) and any(
        x in url.lower() for x in ('/playlist', 'list=', '/@', '/channel/', '/c/')
    ):
        cmd += ['--playlist-end', env.get('YOUTUBE_PLAYLIST_END', '3')]
    else:
        cmd += ['--no-playlist']
    cmd.append(url)

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=subprocess_env_no_proxy()
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or 'yt-dlp failed')[:800]
        if is_youtube_shorts_url(raw_url):
            err += ' (Shorts: попробуйте ещё раз через минуту или /yt <ссылка>)'
        elif is_youtube_live_url(raw_url):
            err += ' (Live/VOD: полный стрим качается долго — для Shorts шлите /shorts/…)'
        raise RuntimeError(err)
    outfile = finalize_yt_work_file(work)
    destination = pending_dir / f'{stamp}_youtube_{outfile.stem.replace("yt_", "")}.mp4'
    shutil.move(str(outfile), str(destination))
    shutil.rmtree(work, ignore_errors=True)
    game_label = label.strip() or game_label_for_chat(chat_id)
    append_pending(chat_id, destination, game_label)
    hero = (env.get('YOUTUBE_DEFAULT_HERO') or '').strip().lower()
    if hero:
        hero_dir = Path(f'/root/hero_datasets/{hero}')
        hero_dir.mkdir(parents=True, exist_ok=True)
        copy_dest = hero_dir / f'yt_{destination.name}'
        shutil.copy2(destination, copy_dest)
    return destination


def ffprobe_duration_sec(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    return 0.0


def smart_make_timeout_sec(source_paths: list[Path], env_map: dict[str, str]) -> int:
    base = int(float(env_map.get('SMART_MAKE_TIMEOUT_SEC', '10800')))
    cap = int(float(env_map.get('SMART_MAKE_TIMEOUT_MAX_SEC', '14400')))
    max_dur = max((ffprobe_duration_sec(p) for p in source_paths), default=0.0)
    if max_dur >= 3600:
        scaled = int(max_dur * 0.35 + 1800)
    elif max_dur >= 1200:
        scaled = int(max_dur * 0.45 + 900)
    else:
        scaled = base
    return min(cap, max(base, scaled))


def parse_youtube_allowed_chat_ids() -> set[str]:
    ids: set[str] = set()
    for key in ('YOUTUBE_ALLOWED_CHAT_IDS', 'TG_ALLOWED_CHAT_IDS'):
        for part in env.get(key, '').split(','):
            part = part.strip()
            if part:
                ids.add(part)
    return ids


def youtube_ingest_allowed(chat_id: str) -> bool:
    """Owner + YOUTUBE_ALLOWED_CHAT_IDS / TG_ALLOWED_CHAT_IDS may ingest YouTube."""
    if is_owner(chat_id):
        return True
    allowed = parse_youtube_allowed_chat_ids()
    if allowed:
        return str(chat_id) in allowed
    if env.get('YOUTUBE_OWNER_ONLY', '').strip().lower() in ('1', 'true', 'yes'):
        return False
    return chat_is_allowed(chat_id)


def _youtube_download_worker(chat_id: str) -> None:
    """Background worker — does not block Telegram poll loop."""
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from youtube_download import is_youtube_live_url, is_youtube_shorts_url  # noqa: WPS433

    while True:
        with YOUTUBE_DOWNLOAD_LOCK:
            queue = YOUTUBE_PENDING_QUEUES.get(chat_id) or []
            if not queue:
                YOUTUBE_DOWNLOAD_CHATS.discard(chat_id)
                return
            item = queue.pop(0)
            remaining = len(queue)
        url = item['url']
        raw = item.get('raw', url)
        logging.info('youtube worker chat=%s url=%s remaining=%s', chat_id, url[:120], remaining)
        try:
            saved = save_youtube_from_url(url, chat_id, source_url=raw)
            pending_count = count_pending(chat_id)
            dur = ffprobe_duration_sec(saved)
            dur_hint = ''
            if dur >= 3600:
                dur_hint = f' ~{int(dur // 3600)}ч {int((dur % 3600) // 60)}м'
            elif dur >= 120:
                dur_hint = f' ~{int(dur // 60)}м'
            kind = 'Short' if is_youtube_shorts_url(raw) else ('Live/VOD' if is_youtube_live_url(raw) else 'YouTube')
            tail = f' Осталось в очереди: {remaining}.' if remaining else ''
            send_message(
                chat_id,
                f'✅ {kind} скачан: {saved.name} ({pending_count} в pending).{dur_hint}{tail} Дальше /make.',
            )
        except Exception as exc:
            logging.exception('youtube download failed')
            tail = f' Осталось в очереди: {remaining}.' if remaining else ''
            send_message(chat_id, f'❌ YouTube не скачался: {exc}{tail}')
        time.sleep(1)


def enqueue_youtube_downloads(chat_id: str, items: list[dict[str, str]]) -> int:
    with YOUTUBE_DOWNLOAD_LOCK:
        queue = YOUTUBE_PENDING_QUEUES.setdefault(chat_id, [])
        seen = {x['url'] for x in queue}
        added = 0
        for item in items:
            if item['url'] not in seen:
                queue.append(item)
                seen.add(item['url'])
                added += 1
        start_worker = chat_id not in YOUTUBE_DOWNLOAD_CHATS
        if start_worker and queue:
            YOUTUBE_DOWNLOAD_CHATS.add(chat_id)
    if start_worker and queue:
        threading.Thread(target=_youtube_download_worker, args=(chat_id,), daemon=True).start()
    return added


def try_youtube_ingest(chat_id: str, message: dict) -> bool:
    """Handle YouTube URLs in text/caption/entities. Returns True if handled."""
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from youtube_download import is_youtube_live_url, is_youtube_shorts_url, normalize_youtube_url  # noqa: WPS433

    yt_urls = [u for u in extract_urls_from_message(message) if looks_like_youtube_url(u)]
    if not yt_urls:
        return False
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in yt_urls:
        url = normalize_youtube_url(raw)
        if url and url not in seen:
            seen.add(url)
            items.append({'url': url, 'raw': raw})
    logging.info('youtube ingest chat=%s urls=%s', chat_id, len(items))
    if not youtube_ingest_allowed(chat_id):
        send_message(
            chat_id,
            f'YouTube принимает владелец бота (ваш chat_id={chat_id}). '
            f'Напишите /ping — там «владелец=да/нет».',
        )
        return True

    shorts_n = sum(1 for u in yt_urls if is_youtube_shorts_url(u))
    live_n = sum(1 for u in yt_urls if is_youtube_live_url(u))
    if shorts_n:
        eta = 'Shorts: ~30–90 сек каждый (в фоне, бот не зависает).'
    elif live_n:
        eta = 'Live/VOD: полный стрим — может занять 30–60+ мин.'
    else:
        eta = 'YouTube: ~1–5 мин.'
    if len(items) > 1:
        eta += f' В очереди: {len(items)} ссылок.'
    send_message(chat_id, f'Качаю на сервер. {eta}')
    enqueue_youtube_downloads(chat_id, items)
    return True


def looks_like_video_url(url: str) -> bool:
    u = url.lower().rstrip('.,);')
    if looks_like_research_url(url) or looks_like_youtube_url(url):
        return False
    return any(
        marker in u
        for marker in (
            '.mp4',
            '.mov',
            '.mkv',
            '.webm',
            'transfer.sh',
            '0x0.st',
            'catbox.moe',
            'tmpfiles.org',
            'file.io',
        )
    )


def save_video_from_url(url: str, chat_id: str, label: str = '') -> Path:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name) or 'upload.mp4'
    if not name.lower().endswith(('.mp4', '.mov', '.mkv', '.webm')):
        name = f'{name}.mp4' if '.' not in name else name
    pending_dir = chat_pending_dir(chat_id)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    safe = ''.join(ch if ch.isalnum() or ch in {'.', '-', '_'} else '_' for ch in name)[:80]
    destination = pending_dir / f'{stamp}_url_{safe}'
    req = urllib.request.Request(url, headers={'User-Agent': 'ContenBot-video/1.0'})
    with urllib.request.urlopen(req, timeout=900) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)
    game_label = label.strip() or game_label_for_chat(chat_id)
    append_pending(chat_id, destination, game_label)
    return destination


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


def summarize_instagram_digest_log(log_path: Path) -> dict:
    """Read the latest digest run from the shared log file."""
    if not log_path.exists():
        return {}
    lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
    sent = 0
    errors = 0
    auth_expired = False
    for line in reversed(lines[-200:]):
        if 'done sent=' in line:
            m = re.search(
                r'sent=(\d+).*errors=(\d+)(?:.*auth_expired=(True|False))?',
                line,
            )
            if m:
                sent = int(m.group(1))
                errors = int(m.group(2))
                if m.lastindex and m.lastindex >= 3 and m.group(3):
                    auth_expired = m.group(3) == 'True'
            break
        if '401' in line or 'instagram_auth_expired' in line or 'auth_expired=True' in line:
            auth_expired = True
    return {'sent': sent, 'errors': errors, 'auth_expired': auth_expired}


def instagram_digest_completion_text(log_path: Path) -> str:
    summary = summarize_instagram_digest_log(log_path)
    sent = int(summary.get('sent', 0))
    auth = bool(summary.get('auth_expired'))
    errors = int(summary.get('errors', 0))
    log_tail = ''
    if log_path.exists():
        log_tail = log_path.read_text(encoding='utf-8', errors='replace')[-8000:]
    if auth:
        return (
            'Instagram-дайджест завершён без постов: сессия Instagram истекла (401).\n\n'
            '1) Зайдите в instagram.com в браузере\n'
            '2) Экспорт cookies (Netscape) — Get cookies.txt LOCALLY\n'
            '3) Пришлите cookies.txt боту как документ\n'
            '4) /ig_digest — повторить рассылку'
        )
    if sent > 0:
        return f'Instagram-дайджест завершён: отправлено {sent} пост(ов).'
    if errors and ('ProxyError' in log_tail or 'Connection refused' in log_tail):
        return (
            'Instagram-дайджест: постов не отправлено — сработал мёртвый HTTP_PROXY '
            '(прокси для TikTok, не для Instagram).\n'
            'Прокси для дайджеста отключён на сервере. Подождите 10–15 мин и снова /ig_digest.'
        )
    if errors:
        return (
            f'Instagram-дайджест: постов не отправлено, ошибок загрузки: {errors}.\n'
            'Проверьте лог ниже. Если 401 — обновите cookies (/ig_cookies). '
            'Иначе повторите /ig_digest через 15–30 мин.'
        )
    return (
        'Instagram-дайджест завершён: новых постов для отправки не было '
        '(все уже в базе или отфильтрована реклама).\n'
        'Если ожидали картинки — /ig_cookies → файл → /ig_digest.'
    )


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
            env = os.environ.copy()
            if notify_chat_id:
                # Completion text is sent by the bot; avoid duplicate Telegram pings.
                env['IG_NOTIFY_AUTH'] = '0'
                env['IG_NOTIFY_EMPTY'] = '0'
            subprocess.run(['bash', str(script)], check=False, timeout=1800, env=env)
            if notify_chat_id:
                log_path = Path('/root/data/mlbb/instagram_digest.log')
                body = instagram_digest_completion_text(log_path)
                summary = summarize_instagram_digest_log(log_path)
                if int(summary.get('sent', 0)) == 0 and log_path.exists():
                    log_tail = '\n'.join(
                        log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-6:]
                    )
                    body += f'\n\nЛог:\n{log_tail}'
                send_message(notify_chat_id, body[:3900])
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
    try:
        from instagram_cookies_util import normalize_instagram_cookies_file

        normalize_instagram_cookies_file(INSTAGRAM_COOKIES_PATH)
    except ImportError:
        sys_path = Path(__file__).resolve().parent
        import sys

        sys.path.insert(0, str(sys_path))
        from instagram_cookies_util import normalize_instagram_cookies_file

        normalize_instagram_cookies_file(INSTAGRAM_COOKIES_PATH)
    except ValueError as exc:
        INSTAGRAM_COOKIES_PATH.unlink(missing_ok=True)
        raise ValueError(str(exc)) from exc
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
            'file_size': int(video.get('file_size') or 0),
            'duration': int(video.get('duration') or 0),
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
                'file_size': int(document.get('file_size') or 0),
                'duration': 0,
            }
    return None


def media_too_large_for_bot(media: dict) -> bool:
    size = int(media.get('file_size') or 0)
    return size > TELEGRAM_BOT_MAX_BYTES


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
        prune_used_from_queue_file(queue_file_for(chat_id), chat_id=chat_id)
        if only_paths is not None:
            fresh_paths = filter_new_sources(only_paths, chat_id=chat_id)
            lines = _lines_from_paths(chat_id, fresh_paths)
        else:
            queue_path = queue_file_for(chat_id)
            lines = [
                _fix_queue_line(line, chat_id)
                for line in queue_path.read_text().splitlines()
                if line.strip()
            ]
            paths = [Path(line.split('|', 1)[0]) for line in lines]
            fresh_paths = filter_new_sources(paths, chat_id=chat_id)
            lines = [line for line in lines if Path(line.split('|', 1)[0]) in fresh_paths]
        if not lines:
            if limited:
                send_message(
                    chat_id,
                    'Этот ролик уже нарезали (тот же файл). Пришлите **новое** видео или другой фрагмент стрима.',
                )
                if os.environ.get('NOTIFY_OWNER_ON_SKIP', '0') == '1':
                    notify_owner(
                        f'Нарезка пропущена для chat {chat_id}: дубликат или старше {env.get("SOURCE_MAX_AGE_HOURS", "36")} ч.',
                    )
                return
            send_message(chat_id, 'У тебя пока нет загруженных видео. Сначала пришли файлы, потом команду /make.')
            return

        source_paths = [Path(line.split('|', 1)[0]) for line in lines]
        make_timeout = smart_make_timeout_sec(source_paths, env)
        max_dur = max((ffprobe_duration_sec(p) for p in source_paths), default=0.0)
        forced_profile = MAKE_PROFILE_OVERRIDES.pop(chat_id, None)
        prof = resolve_montage_profile(chat_id, lines, forced=forced_profile)
        game_hint = PROFILE_LABELS.get(prof, prof.replace('_', ' ').title())
        long_note = ''
        if max_dur >= 1200:
            long_note = (
                f' Длинный исходник (~{int(max_dur // 60)} мин): '
                f'ищу моменты со стрельбой по всему ролику, ждите до ~{make_timeout // 60} мин.'
            )
        if not limited:
            send_message(
                chat_id,
                f'Принял задачу ({game_hint}). Строгий монтаж: 3–4 сегмента, ~33–57 сек.{long_note}',
            )

        if prof in STRICT_MONTAGE_PROFILES:
            source_path = source_paths[0]
            caption = game_label_for_chat(
                chat_id,
                lines[0].split('|', 1)[1] if '|' in lines[0] and len(lines[0].split('|', 1)) > 1 else None,
            )
            code, detail = run_strict_montage_for_source(chat_id, source_path, prof, caption)
            logging.info('strict_montage chat=%s code=%s detail=%s', chat_id, code, detail)
            if code == 3:
                mark_used([source_path], chat_id=chat_id)
                archive_processed(chat_id, lines)
                preview_id = _extract_preview_id(detail)
                owner_note = (
                    f'Превью отправлено владельцу. Подтверди: /approve_preview {preview_id}'
                    if preview_id
                    else 'Превью отправлено владельцу — ждём /approve_preview.'
                )
                if limited:
                    send_message(chat_id, f'Монтаж готов к проверке. {owner_note}')
                else:
                    send_message(chat_id, owner_note)
            elif code == 2:
                send_message(chat_id, f'REFUSED: исходник не найден — {detail}')
            else:
                refuse = detail if detail.startswith('REFUSED') else f'REFUSED: {detail}'
                if limited:
                    send_message(chat_id, refuse[:900])
                    notify_owner(f'REFUSED montage chat={chat_id}\n{detail}')
                else:
                    send_message(chat_id, refuse[:900])
        else:
            with tempfile.NamedTemporaryFile('w', delete=False, prefix=f'tg-batch-{chat_id}-', suffix='.txt') as tmp_queue:
                tmp_queue.write('\n'.join(lines) + '\n')
                tmp_queue_path = tmp_queue.name

            run_env = os.environ.copy()
            run_env['QUEUE_FILE'] = tmp_queue_path
            run_env['MAX_SOURCES'] = str(len(lines))
            run_env.setdefault('TARGET_DURATION', env.get('SMART_TARGET_DURATION', '40'))
            run_env['DEFAULT_GAME_PROFILE'] = profile
            run_env['QUEUE_GAME_PROFILE'] = profile
            if len(lines) == 1:
                run_env['SINGLE_SOURCE_MODE'] = '1'
            completed = subprocess.run(
                [PROCESSOR],
                env=run_env,
                timeout=make_timeout,
                capture_output=True,
                text=True,
            )
            Path(tmp_queue_path).unlink(missing_ok=True)

            if completed.returncode == 0:
                mark_used([Path(line.split('|', 1)[0]) for line in lines], chat_id=chat_id)
                archive_processed(chat_id, lines)
                if not limited:
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


def _approve_preview_worker(chat_id: str, preview_id: str) -> None:
    """Heavy CLIP/rescore — must not block getUpdates poll loop."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from segment_preview import approve_preview, send_approved_montage

        pkg = approve_preview(preview_id, by_chat=str(chat_id))
        if not pkg:
            send_message(chat_id, f'REFUSED: preview, reason=unknown_id, visual_passed=0/0')
            return
        env_map = dict(os.environ)
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env_map.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        caption = f"{pkg.get('game', '')} | owner approved"
        send_approved_montage(pkg, env_map, caption)
        n = sum(len(s.get('screenshots', [])) for s in pkg.get('segments', []))
        ts = [s['start'] for s in pkg.get('segments', [])]
        send_message(chat_id, f'SENT: preview_id={preview_id}, screens={n}, timestamps={ts}')
    except Exception as exc:
        logging.exception('approve_preview failed')
        send_message(chat_id, f'REFUSED: preview, reason={exc}, visual_passed=0/0')


def start_processing(chat_id: str, only_paths: list[Path] | None = None):
    with PROCESSING_LOCK:
        if chat_id in PROCESSING_CHATS:
            if not is_limited_notify(chat_id):
                send_message(chat_id, 'Обработка уже запущена. Дождись результата или сообщения об ошибке.')
            return
        PROCESSING_CHATS.add(chat_id)
    thread = threading.Thread(target=process_chat_batch, args=(chat_id, only_paths), daemon=True)
    thread.start()


def _bot_command_list() -> list[dict[str, str]]:
    return [
        {'command': 'start', 'description': 'Начало работы'},
        {'command': 'ping', 'description': 'Статус бота и chat_id'},
        {'command': 'yt', 'description': 'Скачать YouTube / Shorts'},
        {'command': 'whoami', 'description': 'Ваш chat_id (как в ping)'},
        {'command': 'make', 'description': 'Собрать нарезку из видео'},
        {'command': 'upload_standoff2', 'description': 'Примеры Standoff 2 (владелец)'},
        {'command': 'upload_vkmlbb', 'description': 'Очередь клипов MLBB → VK'},
        {'command': 'status', 'description': 'Сколько видео в очереди'},
        {'command': 'process', 'description': 'Процесс пайплайна (что сейчас ищет)'},
        {'command': 'reset', 'description': 'Сброс исчерпанных VOD + поиск'},
        {'command': 'ad', 'description': 'Скрины рекламы (владелец)'},
        {'command': 'ad_done', 'description': 'Закончить приём скринов'},
        {'command': 'wm', 'description': 'Убрать водяной знак (владелец)'},
        {'command': 'mlbb_samples', 'description': 'MLBB Shorts на оценку (владелец)'},
        {'command': 'mlbb_vod', 'description': 'MLBB VOD — все куски отдельно (владелец)'},
        {'command': 'shorts_mode', 'description': 'Режим Shorts — 5 игр (владелец)'},
        {'command': 'vod_mode', 'description': 'Режим VOD-нарезки (владелец)'},
        {'command': 'shorts_all', 'description': '3 Shorts × 5 игр на оценку'},
        {'command': 'shorts', 'description': 'Shorts одной игры: /shorts pubg'},
        {'command': 'mode', 'description': 'Текущий режим бота'},
        {'command': 'mlbb_yes', 'description': 'MLBB Shorts — хороший (#id)'},
        {'command': 'mlbb_no', 'description': 'MLBB Shorts — плохой (#id)'},
    ]


def register_bot_commands() -> None:
    """Push command menu to Telegram (default + private chats)."""
    commands = _bot_command_list()
    scopes: list[dict | None] = [
        None,
        {'type': 'all_private_chats'},
        {'type': 'all_group_chats'},
    ]
    for scope in scopes:
        payload: dict = {'commands': commands}
        if scope is not None:
            payload['scope'] = scope
        try:
            api_call('setMyCommands', payload, timeout=30)
        except Exception as exc:
            logging.warning('setMyCommands scope=%s failed: %s', scope, exc)


def handle_message(message: dict):
    chat = message.get('chat', {})
    chat_id = str(chat.get('id', ''))
    if not chat_id:
        return
    if not chat_is_allowed(chat_id):
        logging.warning('unauthorized chat %s', chat_id)
        send_message(
            chat_id,
            f'Этот chat_id={chat_id} не в списке бота. '
            f'Добавьте в /root/.video_bot.env: TG_CHAT_ID={chat_id} '
            f'или TG_ALLOWED_CHAT_IDS=… и перезапустите бота.',
        )
        return

    text = (message.get('text') or '').strip()
    cmd = command_token(text)
    logging.info('message chat=%s cmd=%r text=%r', chat_id, cmd, text[:80] if text else '')
    caption = safe_label(message.get('caption'))
    limited = is_limited_notify(chat_id)

    if is_owner(chat_id):
        from telegram_owner_controls import (
            format_process_report,
            is_process_command,
            is_reset_command,
            parse_reset_game,
            run_reset,
        )

        if is_process_command(text):
            send_owner_controls(chat_id, format_process_report())
            return
        if is_reset_command(text) or cmd == '/reset':
            try:
                game = parse_reset_game(text)
            except ValueError as exc:
                send_owner_controls(chat_id, f'Сброс: {exc}')
                return
            send_owner_controls(chat_id, run_reset(game))
            return

    # YouTube / Shorts — сразу, до остальных команд (кроме явных /команд)
    if not (text.startswith('/') or caption.startswith('/')):
        if try_youtube_ingest(chat_id, message):
            return

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
            start_text = (
                'Отправь сюда 3-10 видео. Когда все загрузишь, дай /make — я соберу Smart Edit v1.1 из 3-4 хайлайтов на 33-57 секунд.\n\n'
                'YouTube / Shorts: пришли ссылку одной строкой (или /yt <url>) → /make.\n'
                'Примеры рекламы (скрины): /ad → фото → /ad_done.\n'
                'Водяной знак «god of mlbb»: /wm → фото → /wm_done.\n\n'
                'Кнопки внизу: «Процесс» — что сейчас ищет пайплайн, «Сброс» — снова открыть исчерпанные VOD.'
            )
            if is_owner(chat_id):
                send_owner_controls(chat_id, start_text)
            else:
                send_message(chat_id, start_text)
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
    if cmd in ('/upload_standoff2', '/standoff2_upload', '/so2_upload'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /upload_standoff2 только для владельца.')
            return
        set_standoff_exemplar_mode(chat_id, True)
        total = count_standoff_exemplar_clips()
        send_message(
            chat_id,
            'Режим примеров Standoff 2 включён на 2 часа.\n'
            'Пришли короткие клипы с дуэлями/клатчами — сохраню как эталоны для нарезки.\n'
            'Уже загруженные 9 роликов: /upload_standoff2_import\n'
            'Завершить: /upload_standoff2_done\n'
            f'Сейчас exemplars: {total} шт. (нужно ≥5).',
        )
        return
    if cmd in ('/upload_standoff2_import', '/standoff2_import'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда только для владельца.')
            return
        try:
            saved, skipped = import_owner_standoff_exemplars(chat_id, limit=9)
            total = count_standoff_exemplar_clips()
            lines = [f'Импорт Standoff exemplars: +{len(saved)}, пропущено {len(skipped)}, всего {total}.']
            for path in saved[:9]:
                lines.append(f'• {path.name}')
            if total >= 5:
                lines.append('Достаточно для /make standoff и ночной очереди.')
            else:
                lines.append(f'Ещё нужно {5 - total} примеров (или /upload_standoff2).')
            send_message(chat_id, '\n'.join(lines))
            notify_owner(f'Standoff exemplars import chat={chat_id}: +{len(saved)} total={total}')
        except Exception as exc:
            logging.exception('standoff exemplar import failed')
            send_message(chat_id, f'Импорт не удался: {exc}')
        return
    if cmd in ('/upload_standoff2_done', '/standoff2_done'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда только для владельца.')
            return
        was = is_standoff_exemplar_mode(chat_id)
        set_standoff_exemplar_mode(chat_id, False)
        total = count_standoff_exemplar_clips()
        send_message(
            chat_id,
            f'Режим /upload_standoff2 выключен. Exemplars Standoff: {total} шт.\n'
            + ('Последние клипы сохранены.' if was else ''),
        )
        return
    if cmd in ('/upload_vkmlbb', '/vkmlbb_upload', '/vk_mlbb'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда /upload_vkmlbb только для владельца.')
            return
        if env.get('VK_MLBB_DISABLED', '0') == '1':
            send_message(chat_id, 'VK MLBB отключён — рассылка и уведомления выключены.')
            return
        set_vk_mlbb_upload_mode(chat_id, True)
        q = count_vk_mlbb_pending()
        send_message(
            chat_id,
            'Режим загрузки MLBB → VK включён на 7 дней.\n'
            'Присылай видео (клипы/нарезки) — попадут в очередь на публикацию.\n'
            'Расписание VK: 09:00, 13:30, 18:00 МСК — по 3 ролика за раз.\n'
            f'Сейчас в очереди: {q} шт.\n'
            'Статус: /upload_vkmlbb_status | Завершить приём: /upload_vkmlbb_done',
        )
        return
    if cmd in ('/upload_vkmlbb_status', '/vkmlbb_status'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда только для владельца.')
            return
        q = count_vk_mlbb_pending()
        mode = 'включён' if is_vk_mlbb_upload_mode(chat_id) else 'выключен'
        send_message(
            chat_id,
            f'VK MLBB очередь: {q} видео.\n'
            f'Режим /upload_vkmlbb: {mode}.\n'
            'Слоты: 09:00 / 13:30 / 18:00 МСК (3 шт. за слот).',
        )
        return
    if cmd in ('/upload_vkmlbb_done', '/vkmlbb_done'):
        if not is_owner(chat_id):
            send_message(chat_id, 'Команда только для владельца.')
            return
        was = is_vk_mlbb_upload_mode(chat_id)
        set_vk_mlbb_upload_mode(chat_id, False)
        q = count_vk_mlbb_pending()
        send_message(
            chat_id,
            f'Режим /upload_vkmlbb выключен. В очереди VK: {q} шт.\n'
            + ('Последние видео сохранены в очередь.' if was else ''),
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
    if cmd in ('/whoami', '/id', '/chatid', '/chat_id'):
        register_bot_commands()
        send_message(
            chat_id,
            f'chat_id={chat_id}\n'
            f'владелец(TG_CHAT_ID)={"да" if is_owner(chat_id) else "нет"}\n'
            f'доступ={"да" if chat_is_allowed(chat_id) else "нет"}\n'
            f'owner_env={DEFAULT_CHAT_ID or "(пусто)"}\n'
            f'То же в /ping. Меню команд обновил — закройте чат и откройте снова.',
        )
        return
    if cmd == '/ping':
        register_bot_commands()
        yt_urls = [u for u in extract_urls_from_message(message) if looks_like_youtube_url(u)]
        send_message(
            chat_id,
            f'Бот на связи ({BOT_VERSION}).\n'
            f'chat_id={chat_id}\n'
            f'владелец(TG_CHAT_ID)={"да" if is_owner(chat_id) else "нет"} '
            f'(в .env: {DEFAULT_CHAT_ID or "пусто"})\n'
            f'youtube={"да" if youtube_ingest_allowed(chat_id) else "нет"} '
            f'PUBG={"да" if is_pubg_chat(chat_id) else "нет"}\n'
            f'yt-dlp={"ok" if shutil.which("yt-dlp") else "НЕТ на сервере"}\n'
            f'YouTube: ссылка Shorts или /yt <url> → /make'
            + (f'\nссылка в сообщении: {"да" if yt_urls else "нет"}' if yt_urls else ''),
        )
        return
    if cmd in ('/yt', '/youtube'):
        if not youtube_ingest_allowed(chat_id):
            send_message(chat_id, f'YouTube только для владельца. chat_id={chat_id}')
            return
        urls = extract_urls_from_message(message)
        yt_urls = [u for u in urls if looks_like_youtube_url(u)]
        if not yt_urls:
            send_message(
                chat_id,
                'Пришлите ссылку: /yt https://youtube.com/shorts/… или youtu.be/…',
            )
            return
        fake_msg = {'text': yt_urls[0]}
        try_youtube_ingest(chat_id, fake_msg)
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
    if is_owner(chat_id) and text.startswith('/approve_preview'):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, 'Использование: /approve_preview <preview_id>')
            return
        preview_id = parts[1].strip()
        send_message(
            chat_id,
            f'Принял /approve_preview — проверяю и отправляю видео (1–5 мин).\n'
            f'id={preview_id}',
        )
        threading.Thread(
            target=_approve_preview_worker,
            args=(chat_id, preview_id),
            daemon=True,
        ).start()
        return
    if is_owner(chat_id) and cmd in ('/mode', '/pipeline_mode'):
        try:
            from pipeline_mode_switch import mode_status

            send_message(chat_id, mode_status())
        except Exception as exc:
            send_message(chat_id, f'mode error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/shorts_mode', '/mode_shorts'):
        try:
            from pipeline_mode_switch import activate_shorts_mode

            send_message(chat_id, activate_shorts_mode())
        except Exception as exc:
            send_message(chat_id, f'shorts_mode error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/vod_mode', '/mode_vod'):
        try:
            from pipeline_mode_switch import activate_vod_mode

            send_message(chat_id, activate_vod_mode())
        except Exception as exc:
            send_message(chat_id, f'vod_mode error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/shorts_all', '/shorts_train'):
        try:
            from game_shorts_calibration import feed_all_games

            send_message(chat_id, 'Качаю и отправляю Shorts: MLBB, PUBG Metro, Standoff, WoT, Genshin (по 3)…')

            def _shorts_all_worker() -> None:
                feed_all_games(token=BOT_TOKEN, chat_id=str(chat_id))

            threading.Thread(target=_shorts_all_worker, daemon=True).start()
        except Exception as exc:
            send_message(chat_id, f'shorts_all error: {exc}')
        return
    if is_owner(chat_id) and cmd.startswith('/shorts'):
        parts = text.split(maxsplit=1)
        game = parts[1].strip().lower() if len(parts) > 1 else 'mlbb'
        try:
            from game_shorts_calibration import feed_game

            send_message(chat_id, f'Подбираю Shorts для {game}…')

            def _shorts_one_worker(g: str = game) -> None:
                feed_game(g, token=BOT_TOKEN, chat_id=str(chat_id))

            threading.Thread(target=_shorts_one_worker, daemon=True).start()
        except Exception as exc:
            send_message(chat_id, f'shorts error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/mlbb_vod', '/mlbb_segments'):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from mlbb_vod_segment_feed import main as mlbb_vod_feed_main

            send_message(chat_id, 'Ищу новые куски MLBB (~10с). Если текущий стрим исчерпан — скачаю новый с YouTube.')

            def _vod_feed_worker() -> None:
                os.environ['MLBB_VOD_FULL_SCAN'] = '1'
                os.environ['MLBB_VOD_BOOTSTRAP'] = '0'
                os.environ['MLBB_VOD_SEGMENT_SEC'] = '15'
                os.environ['HIGHLIGHT_WINDOW_SEC'] = '15'
                os.environ['MLBB_SEEK_PREROLL'] = '8'
                os.environ['MLBB_SEEK_PREROLL_60FPS'] = '12'
                os.environ['MLBB_FORCE_RERENDER'] = '1'
                os.environ['LOGO_FILE'] = '/nonexistent/mlbb_calibration_no_logo.png'
                mlbb_vod_feed_main()

            threading.Thread(target=_vod_feed_worker, daemon=True).start()
        except Exception as exc:
            send_message(chat_id, f'MLBB VOD feed error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/mlbb_samples', '/mlbb_sample'):
        if env.get('MLBB_VOD_ONLY', '0') == '1' and env.get('MULTI_GAME_SHORTS_MODE', '0') != '1':
            if env.get('MLBB_CALIBRATION_FEED_ENABLED', '1') == '0':
                send_message(
                    chat_id,
                    'Shorts отключены. Команды: /shorts_mode → /shorts_all\nИли /vod_mode для нарезки VOD.',
                )
                return
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from mlbb_calibration_feed import main as mlbb_feed_main

            send_message(chat_id, 'Подбираю MLBB Shorts на оценку…')
            threading.Thread(target=mlbb_feed_main, daemon=True).start()
        except Exception as exc:
            send_message(chat_id, f'MLBB feed error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/mlbb_yes', '/mlbb_good'):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            send_message(chat_id, 'Использование: /mlbb_yes {youtube_id} или кнопка 👍 под видео')
            return
        try:
            ok, reply = _mlbb_apply_owner_label(chat_id, parts[1].strip(), is_good=True)
            send_message(chat_id, reply)
        except Exception as exc:
            send_message(chat_id, f'mlbb_yes error: {exc}')
        return
    if is_owner(chat_id) and cmd in ('/mlbb_no', '/mlbb_bad'):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            send_message(chat_id, 'Использование: /mlbb_no {youtube_id} [причина] или кнопка 👎 под видео')
            return
        reason = parts[2].strip() if len(parts) > 2 else 'other'
        reason_aliases = {
            'реклама': 'promo',
            'промо': 'promo',
            'не геймплей': 'not_gameplay',
            'геймплей': 'not_gameplay',
            'скучно': 'boring',
            'герой': 'wrong_hero',
            'музыка': 'music',
            'старое': 'old',
            'старая': 'old',
            'мыльное': 'blurry',
            'мыльная': 'blurry',
            'blur': 'blurry',
        }
        from mlbb_calibration_store import DISLIKE_REASON_CODES

        low = reason.lower()
        reason = reason_aliases.get(low, reason)
        if reason not in DISLIKE_REASON_CODES:
            reason = 'other'
        try:
            ok, reply = _mlbb_apply_owner_label(
                chat_id,
                parts[1].strip(),
                is_good=False,
                reason=reason,
            )
            send_message(chat_id, reply)
        except Exception as exc:
            send_message(chat_id, f'mlbb_no error: {exc}')
        return
    if is_owner(chat_id) and text.startswith('/reject_preview'):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, 'Использование: /reject_preview <preview_id>')
            return
        preview_id = parts[1].strip()
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from segment_preview import reject_preview

            reject_preview(preview_id, by_chat=str(chat_id), reason='owner_rejected')
            send_message(chat_id, f'REFUSED: preview_id={preview_id}, reason=owner_rejected')
        except Exception as exc:
            send_message(chat_id, f'REFUSED: preview, reason={exc}')
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
            send_message(
                chat_id,
                'В очереди 0 видео. Сначала пришли файл (до ~20 МБ). '
                'Длинный ролик (15 мин) Telegram боту не отдаёт — сожми или пришли ссылку на .mp4 '
                '(владельцу). /status — проверить очередь.',
            )
            return
        parts = text.split()
        if len(parts) > 1:
            alias = PROFILE_ALIASES.get(parts[1].lower(), parts[1].lower())
            if alias not in STRICT_MONTAGE_PROFILES and normalize_montage_profile(alias) not in STRICT_MONTAGE_PROFILES:
                send_message(
                    chat_id,
                    'Игра: /make standoff | /make pubg | /make mlbb | /make genshin | /make wot',
                )
                return
            MAKE_PROFILE_OVERRIDES[chat_id] = alias
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
        if url and looks_like_video_url(url):
            send_message(chat_id, 'Качаю видео по ссылке на сервер (может занять несколько минут)…')
            try:
                saved = save_video_from_url(url, chat_id)
                pending_count = count_pending(chat_id)
                send_message(
                    chat_id,
                    f'Видео в очереди: {saved.name} ({pending_count} шт.). Можно /make.',
                )
            except Exception as exc:
                logging.exception('video url download failed')
                send_message(chat_id, f'Не скачалось по ссылке: {exc}\n\n{video_upload_help_text(True)}')
            return
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
            send_message(
                chat_id,
                'Cookies сохранены (формат Cookie-Editor исправлен).\n'
                'Instagram уже мог ругаться на автоматизацию — дайджест сразу не запускаю.\n'
                'Подождите 15–30 мин, затем /ig_digest (не чаще 1–2 раз в сутки).',
            )
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
            hint = 'Команды: /ping, /yt, /ad, /wm, /ig_digest, /start. Видео — файлом.'
            if extract_urls_from_message(message):
                hint += ' Ссылка YouTube? Нужен деплой бота (scripts/deploy_telegram_bot.sh) и /ping с youtube=да.'
            else:
                hint += ' Если /wm не находится — деплой бота на VPS.'
            send_message(chat_id, hint)
        return

    if media_too_large_for_bot(media):
        dur = int(media.get('duration') or 0)
        size_mb = int(media.get('file_size') or 0) / (1024 * 1024)
        logging.warning(
            'skip huge upload chat=%s size_mb=%.1f duration_sec=%s',
            chat_id,
            size_mb,
            dur,
        )
        send_message(chat_id, video_upload_help_text(is_owner(chat_id)))
        return

    pending_dir = chat_pending_dir(chat_id)
    try:
        file_url = get_file_url(media['file_id'])
        destination = pending_dir / f"{int(time.time())}_{media['file_unique_id']}{media['ext']}"
        download_file(file_url, destination)
    except Exception as exc:
        logging.exception('video download failed chat=%s', chat_id)
        send_message(
            chat_id,
            video_upload_help_text(is_owner(chat_id)) + f'\n\nОшибка Telegram: {exc}',
        )
        return
    label = game_label_for_chat(chat_id, caption)
    if is_standoff_exemplar_mode(chat_id):
        if not is_owner(chat_id):
            send_message(chat_id, 'Режим /upload_standoff2 только для владельца.')
            return
        try:
            saved = store_standoff_exemplar_video(chat_id, destination, label)
            total = count_standoff_exemplar_clips()
            send_message(
                chat_id,
                f'Сохранил Standoff exemplar: {saved.name}\n'
                f'Всего: {total} шт. Ещё клипы или /upload_standoff2_done.',
            )
        except Exception as exc:
            logging.exception('standoff exemplar save failed')
            send_message(chat_id, f'Не удалось сохранить exemplar: {exc}')
        return
    if is_vk_mlbb_upload_mode(chat_id):
        if not is_owner(chat_id):
            send_message(chat_id, 'Режим /upload_vkmlbb только для владельца.')
            return
        try:
            saved = enqueue_vk_mlbb_video(chat_id, destination, label)
            q = count_vk_mlbb_pending()
            send_message(
                chat_id,
                f'В очередь VK MLBB: {saved.name}\n'
                f'Всего в очереди: {q} шт. (по 3 за слот: 09:00 / 13:30 / 18:00 МСК).\n'
                'Ещё видео или /upload_vkmlbb_done.',
            )
        except Exception as exc:
            logging.exception('vk mlbb enqueue failed')
            send_message(chat_id, f'Не удалось добавить в очередь VK: {exc}')
        return
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
                    'allowed_updates': ['message', 'callback_query'],
                },
                timeout=POLL_TIMEOUT + 10,
            )
            for update in updates:
                state['last_update_id'] = update['update_id']
                save_state(state)
                callback = update.get('callback_query')
                if callback:
                    handle_callback_query(callback)
                    continue
                message = update.get('message')
                if message:
                    handle_message(message)
        except Exception:
            logging.exception('poll loop error')
            time.sleep(5)


if __name__ == '__main__':
    main()
