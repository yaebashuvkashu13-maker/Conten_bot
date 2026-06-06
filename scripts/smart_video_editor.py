#!/usr/bin/env python3
import fcntl
import hashlib
import itertools
import json
import logging
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import numpy as np

try:
    from gameplay_gate import (
        detect_game_viewport_crop,
        is_gameplay_video,
        load_csv_lookup,
        score_genshin_boss_likelihood,
        segment_is_valid_for_montage,
        segment_opens_with_training,
        source_has_valid_gameplay_window,
    )
    from source_freshness import mark_used
    from mlbb_popularity import popularity_boost
    from mlbb_popularity import extract_video_id as pop_video_id
except ImportError:
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from gameplay_gate import (
        detect_game_viewport_crop,
        is_gameplay_video,
        load_csv_lookup,
        score_genshin_boss_likelihood,
        segment_is_valid_for_montage,
        segment_opens_with_training,
        source_has_valid_gameplay_window,
    )
    from source_freshness import mark_used
    try:
        from mlbb_popularity import popularity_boost
        from mlbb_popularity import extract_video_id as pop_video_id
    except ImportError:
        def popularity_boost(_video_id: str) -> float:
            return 0.0

        def pop_video_id(text: str) -> str | None:
            return None

ENV_FILE = Path(os.environ.get('ENV_FILE', '/root/.video_bot.env'))
DEFAULT_LOG_FILE = Path(os.environ.get('LOG_FILE', '/root/smart_video_editor.log'))
DEFAULT_OUTPUT_DIR = Path(os.environ.get('OUTPUT_DIR', '/root/videos'))
DEFAULT_LOGO_FILE = Path(os.environ.get('LOGO_FILE', '/root/logo.png'))
DEFAULT_LOCK_FILE = Path(os.environ.get('LOCK_FILE', '/var/lock/smart_video_editor.lock'))
SEGMENT_HISTORY_FILE = Path(os.environ.get('SEGMENT_HISTORY_FILE', '/root/.smart_edit_segment_history.json'))
QUEUE_FILE = Path(os.environ.get('QUEUE_FILE', '/root/video_queue.txt'))
MAX_SOURCES = int(os.environ.get('MAX_SOURCES', '6'))
TARGET_DURATION = float(os.environ.get('TARGET_DURATION', os.environ.get('SMART_TARGET_DURATION', '45')))
MIN_FINAL_DURATION = float(os.environ.get('MIN_FINAL_DURATION', '33'))
MAX_FINAL_DURATION = float(os.environ.get('MAX_FINAL_DURATION', '57'))
MIN_HIGHLIGHTS = int(os.environ.get('MIN_HIGHLIGHTS', '3'))
MAX_HIGHLIGHTS = int(os.environ.get('MAX_HIGHLIGHTS', '4'))
TRANSITION_DURATION = float(os.environ.get('TRANSITION_DURATION', '0.28'))
SELECTION_VARIANT = int(os.environ.get('SELECTION_VARIANT', '0'))
EXCLUDED_SOURCE_SIGNATURES = {sig for sig in os.environ.get('EXCLUDED_SOURCE_SIGNATURES', '').split(',') if sig}
EXCLUDED_SEGMENT_KEYS = {sig for sig in os.environ.get('EXCLUDED_SEGMENT_KEYS', '').split(',') if sig}
NICK_BLUR_ENABLED = os.environ.get('BLUR_NICKNAME', '0') == '1'
SEND_TELEGRAM = os.environ.get('SEND_TELEGRAM', '1') == '1'
WINDOW_SECONDS = 1.0
SAMPLE_FPS = float(os.environ.get('SMART_SAMPLE_FPS', '4.0'))
LONG_VIDEO_MIN_SEC = float(os.environ.get('SMART_LONG_VIDEO_MIN_SEC', '1200'))  # 20+ min
LONG_WINDOW_SECONDS = float(os.environ.get('SMART_LONG_WINDOW_SEC', '2.0'))
LONG_SAMPLE_FPS = float(os.environ.get('SMART_LONG_SAMPLE_FPS', '1.0'))
TARGET_HEIGHT = 1280
TARGET_WIDTH = 720
OUTPUT_FPS = 30


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip()
    return env


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(message)s',
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('w')
    blocking = os.environ.get('SMART_BLOCKING_LOCK', '0') == '1'
    try:
        if blocking:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info('another smart editor instance is already running, exiting')
        raise SystemExit(3)
    return handle


def run_command(
    args: list[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    text: bool = True,
    input_data=None,
    env: dict[str, str] | None = None,
):
    logging.debug('running command: %s', ' '.join(shlex.quote(arg) for arg in args))
    return subprocess.run(
        args,
        capture_output=capture_output,
        check=check,
        text=text,
        input=input_data,
        env=env,
    )


def ffprobe_json(path: Path) -> dict:
    result = run_command([
        'ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)
    ], capture_output=True)
    return json.loads(result.stdout)


def first_nonempty(lines: list[str], max_lines: int) -> list[str]:
    batch: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        batch.append(stripped)
        if len(batch) >= max_lines:
            break
    return batch


def drop_first_queue_lines(queue_file: Path, remove_count: int) -> None:
    raw_lines = queue_file.read_text().splitlines()
    remaining: list[str] = []
    removed = 0
    for line in raw_lines:
        stripped = line.strip()
        if removed < remove_count and stripped and not stripped.startswith('#'):
            removed += 1
            continue
        remaining.append(line)
    queue_file.write_text('\n'.join(remaining).rstrip() + ('\n' if remaining else ''))


def take_batch_lines(queue_file: Path, max_sources: int) -> list[str]:
    if not queue_file.exists():
        return []
    return first_nonempty(queue_file.read_text().splitlines(), max_sources)


def robust_scale(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    low = float(np.percentile(values, 10))
    high = float(np.percentile(values, 90))
    if math.isclose(high, low):
        return 0.5
    scaled = (value - low) / (high - low)
    return float(max(0.0, min(1.0, scaled)))


def ffprobe_duration(path: Path) -> float:
    meta = ffprobe_json(path)
    return float(meta.get('format', {}).get('duration', 0.0) or 0.0)


def ffprobe_has_audio(path: Path) -> bool:
    meta = ffprobe_json(path)
    for stream in meta.get('streams', []):
        if stream.get('codec_type') == 'audio':
            return True
    return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_slug(parts: list[str]) -> str:
    text = '_'.join(parts).lower()
    slug = ''.join(ch if ch.isalnum() else '_' for ch in text)
    while '__' in slug:
        slug = slug.replace('__', '_')
    return slug.strip('_') or 'smart_edit'


def resolve_profile(game_names: list[str], env: dict[str, str]) -> str:
    """Queue/chat profile wins over global DEFAULT_GAME_PROFILE in .video_bot.env."""
    forced = (os.environ.get('QUEUE_GAME_PROFILE') or env.get('QUEUE_GAME_PROFILE') or '').strip().lower()
    known = (
        'pubg',
        'mobile_legends',
        'genshin',
        'standoff',
        'wot',
        'world_of_tanks',
        'generic',
    )
    if forced in known:
        return forced

    joined = ' '.join(game_names).lower()
    if 'pubg' in joined or 'playerunknown' in joined or 'пабг' in joined:
        return 'pubg'
    if 'mobile legends' in joined or 'mlbb' in joined:
        return 'mobile_legends'
    if 'genshin' in joined or '原神' in joined:
        return 'genshin'
    if 'standoff' in joined or 'стандофф' in joined:
        return 'standoff'
    if 'blitz' in joined or 'wot blitz' in joined or 'tanks blitz' in joined:
        return 'wot'
    if (
        'world of tanks' in joined
        or 'modern armor' in joined
        or joined.strip() in ('wot', 'world_of_tanks')
        or 'танки' in joined
    ):
        return 'world_of_tanks'

    default_profile = env.get('DEFAULT_GAME_PROFILE', 'generic').lower()
    if default_profile in known:
        return default_profile
    return 'generic'


def detect_profile(game_names: list[str], env: dict[str, str]) -> str:
    return resolve_profile(game_names, env)


def brightness_score(brightness: float) -> float:
    return float(max(0.0, 1.0 - abs(brightness - 0.58) / 0.38))


def saturation_score(saturation: float) -> float:
    return float(max(0.0, 1.0 - abs(saturation - 0.36) / 0.34))


def parse_queue_line(line: str, default_chat_id: str) -> tuple[str, str, str]:
    """path|label|chat_id — label may contain '|' (e.g. 'Hero Highlights | Yu Zhong')."""
    line = line.strip()
    if not line:
        return '', 'Telegram upload', default_chat_id
    parts = line.split('|')
    if len(parts) >= 3:
        source = parts[0].strip()
        chat_id = parts[-1].strip() or default_chat_id
        game = '|'.join(parts[1:-1]).strip() or 'Telegram upload'
        return source, game, chat_id
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip() or 'Telegram upload', default_chat_id
    return parts[0].strip(), 'Telegram upload', default_chat_id


def maybe_download_source(source: str, temp_dir: Path, impersonate: str) -> Path:
    if source.startswith('http://') or source.startswith('https://'):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from youtube_download import subprocess_env_no_proxy  # noqa: WPS433

        template = temp_dir / 'source_%(id)s.%(ext)s'
        run_command([
            'yt-dlp',
            '--impersonate', impersonate,
            '--no-playlist',
            '--no-progress',
            '--restrict-filenames',
            '--merge-output-format', 'mp4',
            '-o', str(template),
            source,
        ], env=subprocess_env_no_proxy())
        files = sorted(temp_dir.glob('source_*'))
        if not files:
            raise RuntimeError(f'yt-dlp did not produce an output file for {source}')
        return files[-1]
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f'source file not found: {source}')
    return path


def analyze_audio(path: Path, duration: float, bins: int, window_sec: float = WINDOW_SECONDS) -> np.ndarray:
    layers = analyze_audio_layers(path, duration, bins, window_sec)
    return layers['energy']


def analyze_audio_layers(
    path: Path,
    duration: float,
    bins: int,
    window_sec: float = WINDOW_SECONDS,
) -> dict[str, np.ndarray]:
    zero = np.zeros(bins, dtype=np.float32)
    if not ffprobe_has_audio(path):
        return {'energy': zero, 'gunfire': zero}
    result = run_command([
        'ffmpeg', '-v', 'error', '-hwaccel', 'none', '-i', str(path), '-vn', '-ac', '1', '-ar', '11025', '-f', 's16le', '-'
    ], capture_output=True, text=False)
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return {'energy': zero, 'gunfire': zero}
    samples /= 32768.0
    samples_per_bin = max(int(len(samples) / max(duration, 1e-3) * window_sec), 1)
    micro_frame = 256
    energy = np.zeros(bins, dtype=np.float32)
    gunfire = np.zeros(bins, dtype=np.float32)
    for idx in range(bins):
        start = idx * samples_per_bin
        end = min(len(samples), start + samples_per_bin)
        if end <= start:
            continue
        chunk = samples[start:end]
        energy[idx] = float(np.sqrt(np.mean(chunk ** 2)))
        micro_energies: list[float] = []
        for offset in range(0, max(len(chunk) - micro_frame, 0), micro_frame):
            micro = chunk[offset : offset + micro_frame]
            micro_energies.append(float(np.sqrt(np.mean(micro * micro))))
        if len(micro_energies) < 2:
            continue
        micro_arr = np.asarray(micro_energies, dtype=np.float32)
        median = float(np.median(micro_arr))
        floor = max(median * 2.4, 0.009)
        spikes = 0
        for probe in range(1, len(micro_arr)):
            if micro_arr[probe] > floor and micro_arr[probe] > micro_arr[probe - 1] * 1.5:
                spikes += 1
        gunfire[idx] = spikes / max(len(micro_arr) - 1, 1)
    return {'energy': energy, 'gunfire': gunfire}


def analysis_sampling(duration: float) -> tuple[float, float, bool]:
    """Return (window_sec, sample_fps, use_seek_mode)."""
    if duration >= LONG_VIDEO_MIN_SEC:
        return LONG_WINDOW_SECONDS, LONG_SAMPLE_FPS, True
    return WINDOW_SECONDS, SAMPLE_FPS, False


def max_candidates_for_duration(duration: float) -> int:
    base = int(os.environ.get('SMART_MAX_CANDIDATES_PER_SOURCE', '4'))
    cap_n = int(os.environ.get('SMART_LONG_MAX_CANDIDATES', '20'))
    if duration < LONG_VIDEO_MIN_SEC:
        return base
    # +2 candidate slots per 10 minutes of source (more peaks to choose from on 2–3h VOD)
    bonus = int(duration // 600) * 2
    return min(cap_n, max(base + bonus, 8))


def _accumulate_frame_stats(
    frame,
    bin_idx: int,
    prev_gray,
    motion,
    center_motion,
    sharpness,
    brightness,
    saturation,
    scene,
    counts,
) -> object:
    frame_small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(frame_small, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame_small, cv2.COLOR_BGR2HSV)
    brightness[bin_idx] += float(gray.mean() / 255.0)
    saturation[bin_idx] += float(hsv[..., 1].mean() / 255.0)
    sharpness[bin_idx] += float(cv2.Laplacian(gray, cv2.CV_32F).var())
    counts[bin_idx] += 1.0
    if prev_gray is not None:
        diff = cv2.absdiff(gray, prev_gray)
        motion[bin_idx] += float(diff.mean() / 255.0)
        h, w = gray.shape
        y0, y1 = int(h * 0.22), int(h * 0.78)
        x0, x1 = int(w * 0.18), int(w * 0.82)
        center_motion[bin_idx] += float(diff[y0:y1, x0:x1].mean() / 255.0)
        hist_prev = cv2.calcHist([prev_gray], [0], None, [32], [0, 256])
        hist_now = cv2.calcHist([gray], [0], None, [32], [0, 256])
        hist_prev = cv2.normalize(hist_prev, hist_prev).flatten()
        hist_now = cv2.normalize(hist_now, hist_now).flatten()
        scene_delta = 1.0 - float(cv2.compareHist(hist_prev, hist_now, cv2.HISTCMP_CORREL))
        if scene_delta > 0.18:
            scene[bin_idx] += scene_delta
    return gray


def analyze_video(path: Path) -> dict:
    duration = ffprobe_duration(path)
    if duration <= 0:
        raise RuntimeError(f'could not determine duration for {path}')
    window_sec, sample_fps, seek_mode = analysis_sampling(duration)
    bins = max(1, int(math.ceil(duration / window_sec)))
    motion = np.zeros(bins, dtype=np.float32)
    center_motion = np.zeros(bins, dtype=np.float32)
    sharpness = np.zeros(bins, dtype=np.float32)
    brightness = np.zeros(bins, dtype=np.float32)
    saturation = np.zeros(bins, dtype=np.float32)
    scene = np.zeros(bins, dtype=np.float32)
    counts = np.zeros(bins, dtype=np.float32)

    try:
        from video_frame_io import ffmpeg_read_frame, prefer_ffmpeg_decode
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from video_frame_io import ffmpeg_read_frame, prefer_ffmpeg_decode

    prev_gray = None
    fw, fh = 160, 90
    use_ffmpeg = prefer_ffmpeg_decode(path) or os.environ.get('SMART_FFMPEG_ANALYSIS', '1') == '1'

    if use_ffmpeg:
        # Single ffmpeg pass (AV1-safe). Avoid per-timestamp ffmpeg spawns on 2–4h VOD.
        eff_fps = sample_fps
        if seek_mode and duration >= LONG_VIDEO_MIN_SEC:
            cap_fps = float(os.environ.get('SMART_LONG_ANALYSIS_MAX_FPS', '0.35'))
            eff_fps = min(sample_fps, cap_fps)
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-hwaccel', 'none',
            '-i', str(path), '-vf', f'fps={eff_fps},scale={fw}:{fh}',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-',
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        chunk = fw * fh * 3
        frame_idx = 0
        while True:
            raw = proc.stdout.read(chunk) if proc.stdout else b''
            if len(raw) < chunk:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((fh, fw, 3))
            timestamp = frame_idx / max(eff_fps, 0.1)
            bin_idx = min(bins - 1, int(timestamp // window_sec))
            prev_gray = _accumulate_frame_stats(
                frame,
                bin_idx,
                prev_gray,
                motion,
                center_motion,
                sharpness,
                brightness,
                saturation,
                scene,
                counts,
            )
            frame_idx += 1
        proc.wait()
    else:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f'failed to open video with OpenCV: {path}')
        native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if seek_mode:
            sample_count = max(2, int(math.ceil(duration * sample_fps)))
            for i in range(sample_count):
                timestamp = min(duration - 0.05, (i / max(sample_count - 1, 1)) * duration)
                cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    continue
                bin_idx = min(bins - 1, int(timestamp // window_sec))
                prev_gray = _accumulate_frame_stats(
                    frame,
                    bin_idx,
                    prev_gray,
                    motion,
                    center_motion,
                    sharpness,
                    brightness,
                    saturation,
                    scene,
                    counts,
                )
        else:
            sample_every = max(int(round(native_fps / sample_fps)), 1)
            frame_idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_idx % sample_every != 0:
                    frame_idx += 1
                    continue
                timestamp = frame_idx / native_fps
                bin_idx = min(bins - 1, int(timestamp // window_sec))
                prev_gray = _accumulate_frame_stats(
                    frame,
                    bin_idx,
                    prev_gray,
                    motion,
                    center_motion,
                    sharpness,
                    brightness,
                    saturation,
                    scene,
                    counts,
                )
                frame_idx += 1
        cap.release()
    counts = np.where(counts > 0, counts, 1.0)
    motion /= counts
    center_motion /= counts
    sharpness /= counts
    brightness /= counts
    saturation /= counts
    scene /= counts
    audio_layers = analyze_audio_layers(path, duration, bins, window_sec)

    return {
        'duration': duration,
        'bins': bins,
        'window_seconds': window_sec,
        'long_mode': seek_mode,
        'motion': motion,
        'center_motion': center_motion,
        'sharpness': sharpness,
        'brightness': brightness,
        'saturation': saturation,
        'scene': scene,
        'audio': audio_layers['energy'],
        'gunfire': audio_layers['gunfire'],
    }


def moving_average(values: np.ndarray) -> np.ndarray:
    if len(values) <= 2:
        return values.copy()
    kernel = np.array([0.2, 0.6, 0.2], dtype=np.float32)
    return np.convolve(values, kernel, mode='same')


ACTION_PROFILES = frozenset({
    'pubg', 'genshin', 'standoff', 'wot', 'world_of_tanks', 'mobile_legends',
})
GUNFIRE_PROFILES = frozenset({'pubg', 'standoff'})
IMPACT_PROFILES = frozenset({'pubg', 'standoff', 'wot', 'world_of_tanks'})


def profile_env_float(profile: str, name: str, default: str) -> float:
    return float(os.environ.get(name, default))


def profile_peak_percentile(profile: str) -> float:
    mapping = {
        'mobile_legends': ('SMART_MLBB_PEAK_PERCENTILE', '52'),
        'pubg': ('SMART_PUBG_PEAK_PERCENTILE', '36'),
        'genshin': ('SMART_GENSHIN_PEAK_PERCENTILE', '38'),
        'standoff': ('SMART_STANDOFF_PEAK_PERCENTILE', '34'),
        'wot': ('SMART_WOT_PEAK_PERCENTILE', '36'),
        'world_of_tanks': ('SMART_WOT_PEAK_PERCENTILE', '36'),
    }
    key, default = mapping.get(profile, ('SMART_PEAK_PERCENTILE', '52'))
    return profile_env_float(profile, key, default)


def profile_sustain_percentile(profile: str) -> float:
    mapping = {
        'pubg': ('SMART_PUBG_SUSTAIN_PERCENTILE', '28'),
        'genshin': ('SMART_GENSHIN_SUSTAIN_PERCENTILE', '30'),
        'standoff': ('SMART_STANDOFF_SUSTAIN_PERCENTILE', '28'),
        'wot': ('SMART_WOT_SUSTAIN_PERCENTILE', '30'),
        'world_of_tanks': ('SMART_WOT_SUSTAIN_PERCENTILE', '30'),
    }
    key, default = mapping.get(profile, ('SMART_SUSTAIN_PERCENTILE', '42'))
    return profile_env_float(profile, key, default)


def profile_motion_audio_percentiles(profile: str) -> tuple[float, float, float]:
    mapping = {
        'pubg': (
            ('SMART_PUBG_MOTION_PERCENTILE', '48'),
            ('SMART_PUBG_AUDIO_PERCENTILE', '46'),
            ('SMART_PUBG_GUNFIRE_PERCENTILE', '56'),
        ),
        'genshin': (
            ('SMART_GENSHIN_MOTION_PERCENTILE', '50'),
            ('SMART_GENSHIN_AUDIO_PERCENTILE', '48'),
            ('SMART_GENSHIN_SCENE_PERCENTILE', '52'),
        ),
        'standoff': (
            ('SMART_STANDOFF_MOTION_PERCENTILE', '48'),
            ('SMART_STANDOFF_AUDIO_PERCENTILE', '46'),
            ('SMART_STANDOFF_GUNFIRE_PERCENTILE', '54'),
        ),
        'wot': (
            ('SMART_WOT_MOTION_PERCENTILE', '48'),
            ('SMART_WOT_AUDIO_PERCENTILE', '50'),
            ('SMART_WOT_SCENE_PERCENTILE', '46'),
        ),
        'world_of_tanks': (
            ('SMART_WOT_MOTION_PERCENTILE', '48'),
            ('SMART_WOT_AUDIO_PERCENTILE', '50'),
            ('SMART_WOT_SCENE_PERCENTILE', '46'),
        ),
    }
    motion_key, motion_def = ('SMART_MOTION_PERCENTILE', '52')
    audio_key, audio_def = ('SMART_AUDIO_PERCENTILE', '50')
    third_key, third_def = ('SMART_SCENE_PERCENTILE', '54')
    if profile in mapping:
        (motion_key, motion_def), (audio_key, audio_def), (third_key, third_def) = mapping[profile]
    return (
        profile_env_float(profile, motion_key, motion_def),
        profile_env_float(profile, audio_key, audio_def),
        profile_env_float(profile, third_key, third_def),
    )


def profile_combat_min(profile: str) -> float:
    mapping = {
        'pubg': ('SMART_PUBG_COMBAT_MIN', '0.20'),
        'genshin': ('SMART_GENSHIN_COMBAT_MIN', '0.17'),
        'standoff': ('SMART_STANDOFF_COMBAT_MIN', '0.19'),
        'wot': ('SMART_WOT_COMBAT_MIN', '0.16'),
        'world_of_tanks': ('SMART_WOT_COMBAT_MIN', '0.16'),
    }
    key, default = mapping.get(profile, ('SMART_COMBAT_MIN', '0.14'))
    return profile_env_float(profile, key, default)


def profile_action_clip_bounds(profile: str) -> tuple[float, float]:
    if profile == 'pubg':
        return (
            float(os.environ.get('SMART_PUBG_CLIP_MIN_SEC', os.environ.get('SMART_ACTION_CLIP_MIN_SEC', '7'))),
            float(os.environ.get('SMART_PUBG_CLIP_MAX_SEC', os.environ.get('SMART_ACTION_CLIP_MAX_SEC', '9.5'))),
        )
    if profile in ACTION_PROFILES:
        return (
            float(os.environ.get('SMART_ACTION_CLIP_MIN_SEC', '7')),
            float(os.environ.get('SMART_ACTION_CLIP_MAX_SEC', '10')),
        )
    return 9.5, 15.0


def build_candidates(
    source_index: int,
    source_path: Path,
    game_name: str,
    analysis: dict,
    global_values: dict[str, list[float]],
    profile: str,
    source_signature: str,
    *,
    relax_segment_gate: bool = False,
) -> list[dict]:
    bins = analysis['bins']
    win = float(analysis.get('window_seconds', WINDOW_SECONDS))
    max_keep = max_candidates_for_duration(float(analysis.get('duration', 0.0)))
    raw_scores = np.zeros(bins, dtype=np.float32)
    bursts = np.zeros(bins, dtype=np.float32)
    for idx in range(bins):
        motion = robust_scale(global_values['motion'], float(analysis['motion'][idx]))
        center = robust_scale(global_values['center_motion'], float(analysis['center_motion'][idx]))
        sharp = robust_scale(global_values['sharpness'], float(analysis['sharpness'][idx]))
        scene = robust_scale(global_values['scene'], float(analysis['scene'][idx]))
        audio = robust_scale(global_values['audio'], float(analysis['audio'][idx]))
        gunshot = robust_scale(
            global_values.get('gunfire', global_values['audio']),
            float(analysis.get('gunfire', analysis['audio'])[idx]),
        )
        bright = brightness_score(float(analysis['brightness'][idx]))
        sat = saturation_score(float(analysis['saturation'][idx]))

        if profile == 'mobile_legends':
            base = (
                0.26 * motion +
                0.17 * center +
                0.18 * audio +
                0.16 * scene +
                0.10 * sharp +
                0.08 * bright +
                0.05 * sat
            )
            # For MLBB, central screen chaos plus fast bursts often means skill trades, dodges, ults.
            base += 0.09 * max(0.0, center - motion * 0.55)
            base += 0.05 * max(0.0, scene - 0.35)
        elif profile == 'pubg':
            # Metro Royale / PUBG: gunshot transients + bursts (not loot/walking).
            base = (
                0.16 * motion +
                0.10 * center +
                0.18 * audio +
                0.34 * gunshot +
                0.05 * scene +
                0.10 * sharp +
                0.04 * bright +
                0.03 * sat
            )
            base += 0.22 * max(0.0, gunshot - 0.28)
            base += 0.12 * max(0.0, audio - 0.30)
            base += 0.08 * max(0.0, motion - 0.22)
            base += 0.06 * max(0.0, sharp - 0.32)
        elif profile == 'genshin':
            base = (
                0.24 * motion +
                0.20 * center +
                0.18 * audio +
                0.20 * scene +
                0.10 * sharp +
                0.05 * bright +
                0.03 * sat
            )
            base += 0.10 * max(0.0, scene - 0.36)
            base += 0.08 * max(0.0, audio - 0.34)
            base += 0.06 * max(0.0, center - 0.28)
        elif profile == 'standoff':
            base = (
                0.18 * motion +
                0.18 * center +
                0.16 * audio +
                0.32 * gunshot +
                0.08 * scene +
                0.08 * sharp +
                0.02 * bright
            )
            base += 0.18 * max(0.0, gunshot - 0.30)
            base += 0.10 * max(0.0, audio - 0.38)
            base += 0.08 * max(0.0, center - 0.26)
        elif profile in ('wot', 'world_of_tanks'):
            base = (
                0.10 * motion +
                0.08 * center +
                0.18 * audio +
                0.32 * gunshot +
                0.16 * scene +
                0.10 * sharp +
                0.04 * bright +
                0.02 * sat
            )
            base += 0.16 * max(0.0, gunshot - 0.28)
            base += 0.10 * max(0.0, audio - 0.34)
            base += 0.06 * max(0.0, scene - 0.30)
        else:
            base = (
                0.28 * motion +
                0.14 * center +
                0.16 * audio +
                0.16 * scene +
                0.12 * sharp +
                0.08 * bright +
                0.06 * sat
            )

        prev_slice = raw_scores[max(0, idx - 3):idx + 1]
        prev_mean = float(prev_slice.mean()) if len(prev_slice) else 0.0
        burst = max(0.0, base - prev_mean)
        penalty = 0.0
        if profile == 'pubg':
            if gunshot < 0.18 and audio < 0.24:
                penalty += 0.42
            if motion > 0.30 and gunshot < 0.22:
                penalty += 0.28
            if scene > 0.50 and gunshot < 0.20:
                penalty += 0.14
        elif profile == 'standoff':
            if gunshot < 0.20 and audio < 0.26:
                penalty += 0.40
            if motion > 0.28 and gunshot < 0.20:
                penalty += 0.26
        elif profile == 'genshin':
            if motion < 0.16 and audio < 0.18 and scene < 0.14:
                penalty += 0.34
            if scene < 0.12 and center < 0.14:
                penalty += 0.18
        elif profile in ('wot', 'world_of_tanks'):
            if gunshot < 0.18 and audio < 0.20:
                penalty += 0.40
            if motion > 0.26 and gunshot < 0.15:
                penalty += 0.34
            if scene < 0.10 and gunshot < 0.14:
                penalty += 0.14
        elif motion < 0.14 and audio < 0.16 and scene < 0.08:
            penalty += 0.20
        if bright < 0.18:
            penalty += 0.10
        if sharp < 0.12:
            penalty += 0.08
        burst_w = 0.24
        if profile in ACTION_PROFILES:
            burst_w = float(os.environ.get('SMART_BURST_WEIGHT', '0.46'))
        raw_scores[idx] = max(0.0, base + burst_w * burst - penalty)
        bursts[idx] = burst

    smooth_scores = moving_average(raw_scores)
    candidates: list[dict] = []
    peak_pct = profile_peak_percentile(profile) if profile in ACTION_PROFILES else float(
        os.environ.get('SMART_PEAK_PERCENTILE', '52')
    )
    if profile == 'mobile_legends':
        peak_pct = float(os.environ.get('SMART_MLBB_PEAK_PERCENTILE', str(peak_pct)))
    peak_threshold = float(np.percentile(smooth_scores, peak_pct)) if bins > 4 else float(smooth_scores.max())
    sustain_pct = profile_sustain_percentile(profile) if profile in ACTION_PROFILES else 42.0
    sustain_threshold = float(np.percentile(smooth_scores, sustain_pct)) if bins > 4 else peak_threshold * 0.72
    motion_pct, audio_pct, scene_pct = profile_motion_audio_percentiles(profile) if profile in ACTION_PROFILES else (52.0, 50.0, 54.0)
    if profile in GUNFIRE_PROFILES:
        gunfire_pct = profile_motion_audio_percentiles(profile)[2]
    elif profile in ('wot', 'world_of_tanks'):
        gunfire_pct = float(os.environ.get('SMART_WOT_IMPACT_PERCENTILE', '50'))
    else:
        gunfire_pct = audio_pct
    motion_threshold = float(np.percentile(analysis['motion'], motion_pct)) if bins > 3 else float(np.max(analysis['motion']))
    scene_threshold = float(np.percentile(analysis['scene'], scene_pct)) if bins > 3 else float(np.max(analysis['scene']))
    audio_threshold = float(np.percentile(analysis['audio'], audio_pct)) if bins > 3 else float(np.max(analysis['audio']))
    gunfire_threshold = float(
        np.percentile(analysis.get('gunfire', analysis['audio']), gunfire_pct)
    ) if bins > 3 else float(np.max(analysis.get('gunfire', analysis['audio'])))

    for idx in range(bins):
        score = float(smooth_scores[idx])
        if score < peak_threshold:
            continue
        local_left = max(0, idx - 1)
        local_right = min(bins, idx + 2)
        if score < float(smooth_scores[local_left:local_right].max()):
            continue

        left = idx
        quiet_steps = 0
        while left > 0 and idx - left < 7:
            probe = left - 1
            active = (
                float(smooth_scores[probe]) >= sustain_threshold or
                float(analysis['motion'][probe]) >= motion_threshold or
                float(analysis['scene'][probe]) >= scene_threshold or
                float(analysis['audio'][probe]) >= audio_threshold or
                float(analysis.get('gunfire', analysis['audio'])[probe]) >= gunfire_threshold
            )
            left = probe
            if active:
                quiet_steps = 0
            else:
                quiet_steps += 1
                if quiet_steps >= 2:
                    break

        right = idx
        quiet_steps = 0
        while right < bins - 1 and right - idx < 11:
            probe = right + 1
            active = (
                float(smooth_scores[probe]) >= sustain_threshold * 0.96 or
                float(analysis['motion'][probe]) >= motion_threshold or
                float(analysis['scene'][probe]) >= scene_threshold or
                float(analysis['audio'][probe]) >= audio_threshold or
                float(analysis.get('gunfire', analysis['audio'])[probe]) >= gunfire_threshold
            )
            right = probe
            if active:
                quiet_steps = 0
            else:
                quiet_steps += 1
                if quiet_steps >= 2:
                    break

        region_slice = slice(max(0, left), min(bins, right + 1))
        mean_region = float(np.mean(smooth_scores[region_slice]))
        mean_motion = float(np.mean(analysis['motion'][region_slice]))
        if profile == 'mobile_legends' and mean_motion < float(os.environ.get('SMART_MIN_BIN_MOTION', '0.012')):
            continue
        mean_audio = float(np.mean(analysis['audio'][region_slice]))
        mean_gunfire = float(np.mean(analysis.get('gunfire', analysis['audio'])[region_slice]))
        if profile in GUNFIRE_PROFILES:
            prefix = 'SMART_PUBG_' if profile == 'pubg' else 'SMART_STANDOFF_'
            combat_min = profile_combat_min(profile)
            gunfire_min = float(os.environ.get(f'{prefix}BIN_GUNFIRE_MIN', '0.08'))
            gunfire_track = analysis.get('gunfire', analysis['audio'])
            if bins > 8:
                adaptive_floor = float(np.percentile(gunfire_track, 52)) * 0.72
                effective_gunfire_min = min(gunfire_min, max(0.028, adaptive_floor))
            else:
                effective_gunfire_min = gunfire_min
            if (
                mean_gunfire < effective_gunfire_min
                and mean_audio < combat_min * 0.78
                and mean_motion < combat_min * 0.78
            ):
                continue
            if mean_motion < combat_min * 0.72 and mean_gunfire < effective_gunfire_min * 0.90:
                continue
        elif profile in ('wot', 'world_of_tanks'):
            combat_min = profile_combat_min(profile)
            impact_min = float(os.environ.get('SMART_WOT_BIN_IMPACT_MIN', '0.10'))
            cluster_sec = (right - left + 1) * win
            if cluster_sec < float(os.environ.get('SMART_WOT_MIN_CLUSTER_SEC', '12')):
                continue
            if mean_gunfire < impact_min and mean_audio < combat_min:
                continue
            if mean_motion > 0.24 and mean_gunfire < impact_min * 0.82:
                continue
        elif profile == 'genshin':
            combat_min = profile_combat_min(profile)
            mean_scene = float(np.mean(analysis['scene'][region_slice]))
            mean_center = float(np.mean(analysis['center_motion'][region_slice]))
            cluster_sec = (right - left + 1) * win
            min_center = float(os.environ.get('SMART_GENSHIN_MIN_CENTER_MOTION', '0.018'))
            if cluster_sec < float(os.environ.get('SMART_GENSHIN_MIN_CLUSTER_SEC', '16')):
                continue
            if mean_motion < combat_min and mean_audio < combat_min and mean_scene < combat_min:
                continue
            if mean_center < min_center and mean_audio < combat_min * 0.90:
                continue
            if mean_audio < combat_min * 0.82 and mean_center < min_center * 1.15:
                continue
        clip_lo, clip_hi = profile_action_clip_bounds(profile)
        desired_duration = max(clip_lo, min(clip_hi, (right - left + 1) * win + 2.0))
        desired_pre = min(5.2, max(3.6, desired_duration * 0.34))
        desired_post = desired_duration - desired_pre
        peak_time = idx * win
        start = max(0.0, peak_time - desired_pre)
        end = min(analysis['duration'], peak_time + desired_post)
        current_duration = end - start
        if current_duration < desired_duration:
            deficit = desired_duration - current_duration
            start = max(0.0, start - deficit * 0.55)
            end = min(analysis['duration'], end + deficit * 0.45)
            current_duration = end - start
            if current_duration < desired_duration:
                if start <= 0.01:
                    end = min(analysis['duration'], start + desired_duration)
                elif end >= analysis['duration'] - 0.01:
                    start = max(0.0, end - desired_duration)
        input_floor = clip_lo if profile in ACTION_PROFILES else 9.0
        input_cap = clip_hi if profile in ACTION_PROFILES else 15.0
        input_duration = max(input_floor, min(input_cap, end - start))
        end = min(analysis['duration'], start + input_duration)
        start = max(0.0, end - input_duration)

        if profile in ACTION_PROFILES:
            skip_intro = float(
                os.environ.get(
                    'SMART_PUBG_SKIP_INTRO_SEC' if profile == 'pubg' else 'SMART_SKIP_INTRO_SEC',
                    '120' if profile == 'pubg' else '90',
                )
            )
            if start < skip_intro:
                continue

        if profile == 'mobile_legends' and os.environ.get('SMART_REJECT_TRAINING', '1') == '1':
            crop_probe = detect_game_viewport_crop(source_path, start, input_duration)
            for _ in range(6):
                if not segment_opens_with_training(source_path, start, crop_box=crop_probe):
                    break
                start = min(start + 2.2, max(0.0, analysis['duration'] - 9.0))
                end = min(analysis['duration'], start + input_duration)
                crop_probe = detect_game_viewport_crop(source_path, start, input_duration)

        if input_duration > 12.5 and mean_motion < motion_threshold and mean_audio < audio_threshold:
            speed = 1.03
        elif input_duration > 11.0 and mean_motion < motion_threshold * 1.03:
            speed = 1.02
        else:
            speed = 1.0

        output_duration = input_duration / speed
        combo_score = score * 0.70 + mean_region * 0.25 + float(bursts[idx]) * 0.12
        if profile in ('mobile_legends', 'pubg'):
            combo_score += 0.08 * max(0.0, mean_motion - 0.5 * motion_threshold)
            combo_score += 0.06 * max(0.0, mean_audio - 0.5 * audio_threshold)
        if profile in GUNFIRE_PROFILES:
            combo_score += 0.20 * max(0.0, mean_gunfire - gunfire_threshold)
            combo_score += 0.14 * max(0.0, float(bursts[idx]) - 0.04)
            combo_score += 0.10 * max(0.0, mean_audio - audio_threshold)
            combo_score += 0.06 * max(0.0, mean_motion - motion_threshold)
        elif profile == 'genshin':
            mean_scene = float(np.mean(analysis['scene'][region_slice]))
            combo_score += 0.12 * max(0.0, mean_scene - scene_threshold)
            combo_score += 0.10 * max(0.0, mean_audio - audio_threshold)
            combo_score += 0.08 * max(0.0, mean_motion - motion_threshold)
            combo_score += 0.06 * max(0.0, (right - left + 1) * win - 12.0)
        elif profile in ('wot', 'world_of_tanks'):
            combo_score += 0.18 * max(0.0, mean_gunfire - gunfire_threshold)
            combo_score += 0.12 * max(0.0, mean_audio - audio_threshold)
            combo_score += 0.06 * max(0.0, mean_motion - motion_threshold)
            combo_score += 0.08 * max(0.0, float(bursts[idx]) - 0.04)

        candidates.append({
            'source_index': source_index,
            'source_signature': source_signature,
            'source_path': str(source_path),
            'game_name': game_name,
            'start': round(start, 3),
            'output_duration': round(output_duration, 3),
            'input_duration': round(input_duration, 3),
            'speed': round(speed, 3),
            'score': round(combo_score, 4),
            'peak_second': idx,
            'cluster_start': left,
            'cluster_end': right,
        })

    candidates.sort(key=lambda item: item['score'], reverse=True)
    pruned: list[dict] = []
    for candidate in candidates:
        if any(candidate_overlap_seconds(candidate, existing) > min(candidate['input_duration'], existing['input_duration']) * 0.45 for existing in pruned):
            continue
        gate_kwargs: dict[str, float] = {}
        if profile == 'mobile_legends':
            try:
                # Normal montages: env can only tighten gates. Etalon builds may loosen
                # subtitle/reject thresholds while keeping combat + chat checks.
                etalon = os.environ.get('ETALON_MONTAGE', '0') == '1'
                default_min_hud = 17.0 if etalon else 17.0
                default_max_text = 0.52 if etalon else 0.28
                default_max_cartoon = 0.50 if etalon else 0.45
                default_max_reject = 0.99 if etalon else 0.78
                env_min_hud = float(os.environ.get('SMART_MIN_HUD', str(default_min_hud)))
                env_max_text = float(os.environ.get('SMART_MAX_OVERLAY_TEXT', str(default_max_text)))
                env_max_cartoon = float(os.environ.get('SMART_MAX_CARTOON_RATIO', str(default_max_cartoon)))
                env_max_reject = float(os.environ.get('SMART_MAX_REJECT_SIM', str(default_max_reject)))
                if etalon:
                    gate_kwargs = {
                        'min_hud': env_min_hud,
                        'max_text': env_max_text,
                        'max_cartoon_ratio': env_max_cartoon,
                        'max_reject_similarity': env_max_reject,
                        'min_hud_frame_rate': float(
                            os.environ.get('SMART_MIN_HUD_FRAME_RATE', '0.62')
                        ),
                    }
                else:
                    gate_kwargs = {
                        'min_hud': max(default_min_hud, env_min_hud),
                        'max_text': min(default_max_text, env_max_text),
                        'max_cartoon_ratio': min(default_max_cartoon, env_max_cartoon),
                        'max_reject_similarity': min(default_max_reject, env_max_reject),
                        'min_hud_frame_rate': max(
                            0.60,
                            float(os.environ.get('SMART_MIN_HUD_FRAME_RATE', '0.60')),
                        ),
                    }
            except ValueError:
                gate_kwargs = {}
        if relax_segment_gate and profile == 'mobile_legends':
            gate_kwargs = {
                'min_hud': 15.0,
                'max_text': 0.11,
                'max_cartoon_ratio': 0.55,
                'min_hud_frame_rate': max(0.68, float(os.environ.get('SMART_MIN_HUD_FRAME_RATE', '0.72'))),
            }
        elif relax_segment_gate and profile in GUNFIRE_PROFILES:
            relax_key = 'SMART_PUBG_RELAX_MIN_GUNFIRE' if profile == 'pubg' else 'SMART_STANDOFF_RELAX_MIN_GUNFIRE'
            gate_kwargs = {
                'min_gunfire': float(os.environ.get(relax_key, '0.045')),
            }
        elif relax_segment_gate and profile == 'genshin':
            gate_kwargs = {
                'min_boss': float(os.environ.get('SMART_GENSHIN_RELAX_MIN_BOSS_BAR', '0.16')),
            }
        elif relax_segment_gate and profile in ('wot', 'world_of_tanks'):
            gate_kwargs = {
                'min_gunfire': float(os.environ.get('SMART_WOT_RELAX_MIN_IMPACT', '0.04')),
            }
        elif relax_segment_gate and os.environ.get('STRICT_GAMEPLAY', '0') != '1':
            gate_kwargs = {'min_hud': 10.0, 'max_text': 0.14, 'max_cartoon_ratio': 0.7}
        ok_segment, reason = segment_is_valid_for_montage(
            Path(candidate['source_path']),
            float(candidate['start']),
            float(candidate['input_duration']),
            profile=profile,
            **gate_kwargs,
        )
        if not ok_segment:
            logging.info(
                'skip segment source=%s start=%.2f reason=%s',
                candidate['source_path'],
                candidate['start'],
                reason,
            )
            continue
        crop_box = detect_game_viewport_crop(
            Path(candidate['source_path']),
            float(candidate['start']),
            float(candidate['input_duration']),
        )
        if crop_box:
            candidate['crop_box'] = crop_box
        if profile == 'genshin':
            try:
                boss_bar, center_motion, boss_score, bar_peak = score_genshin_boss_likelihood(
                    Path(candidate['source_path']),
                    float(candidate['start']),
                    float(candidate['input_duration']),
                    crop_box=candidate.get('crop_box'),
                )
                candidate['boss_bar'] = round(boss_bar, 4)
                candidate['boss_peak'] = round(bar_peak, 4)
                candidate['boss_score'] = round(boss_score, 4)
                candidate['center_motion'] = round(center_motion, 4)
                candidate['score'] = round(
                    float(candidate['score'])
                    + bar_peak * 0.28
                    + boss_bar * 0.24
                    + boss_score * 0.14
                    + min(center_motion, 0.06) * 0.8,
                    4,
                )
            except Exception:
                pass
        vid_pop = pop_video_id(f"{candidate['source_path']} {game_name}")
        if vid_pop:
            candidate['score'] = round(float(candidate['score']) + popularity_boost(vid_pop), 4)
        pruned.append(candidate)
        if len(pruned) >= max_keep:
            break
    return pruned


def candidate_overlap_seconds(candidate: dict, other: dict) -> float:
    if candidate['source_index'] != other['source_index']:
        return 0.0
    start_a = candidate['start']
    end_a = candidate['start'] + candidate['input_duration']
    start_b = other['start']
    end_b = other['start'] + other['input_duration']
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def overlaps(candidate: dict, selected: list[dict]) -> bool:
    profile = os.environ.get('QUEUE_GAME_PROFILE', os.environ.get('DEFAULT_GAME_PROFILE', '')).lower()
    for existing in selected:
        if candidate_overlap_seconds(candidate, existing) > min(
            candidate['input_duration'], existing['input_duration']
        ) * 0.35:
            return True
        if profile == 'genshin' and candidate.get('source_signature') == existing.get('source_signature'):
            gap = float(os.environ.get('SMART_GENSHIN_MIN_SEGMENT_GAP', '75'))
            if abs(float(candidate['start']) - float(existing['start'])) < gap:
                return True
    return False


def effective_duration(selected: list[dict]) -> float:
    if not selected:
        return 0.0
    total = sum(item['output_duration'] for item in selected)
    total -= TRANSITION_DURATION * (len(selected) - 1)
    return total


def game_audio_filter_chain(speed: float) -> str:
    """Suppress TikTok music bed; keep combat SFX transients (no added background track)."""
    base = f'aresample=44100,atempo={speed:.3f},'
    if os.environ.get('SMART_STRIP_MUSIC_BED', '1') != '1':
        return (
            f'{base}'
            'highpass=f=220,lowpass=f=11500,'
            'acompressor=threshold=-24dB:ratio=2.8:attack=8:release=120:makeup=1.5,'
            'volume=1.06'
        )
    return (
        f'{base}'
        'highpass=f=480,lowpass=f=8800,'
        'afftdn=nr=14:nf=-21,'
        'agate=threshold=0.010:ratio=5:attack=25:release=220:makeup=4,'
        'acompressor=threshold=-30dB:ratio=4:attack=5:release=85:makeup=2,'
        'compand=attacks=0.02:points=-80/-900|-45/-30|-25/-15|-10/-8|0/-6|20/-6:soft-knee=6,'
        'volume=1.14'
    )


def extend_selected_to_min_duration(selected: list[dict], profile: str = 'mobile_legends') -> list[dict]:
    """Stretch segment windows until montage meets MIN_FINAL_DURATION when possible."""
    items = [dict(item) for item in selected]
    guard = 0
    while effective_duration(items) < MIN_FINAL_DURATION and guard < 24:
        guard += 1
        changed = False
        for item in sorted(items, key=lambda entry: entry.get('score', 0.0)):
            source_path = Path(item['source_path'])
            src_duration = ffprobe_duration(source_path)
            speed = float(item.get('speed', 1.0) or 1.0)
            start = float(item['start'])
            input_duration = float(item['input_duration'])
            extra = min(2.0, MIN_FINAL_DURATION - effective_duration(items) + 0.5)
            input_cap = 15.8
            if profile in ACTION_PROFILES:
                _lo, input_cap = profile_action_clip_bounds(profile)
            new_input = min(input_cap, input_duration + extra)
            new_start = max(0.0, start - extra * 0.35)
            if new_start + new_input > src_duration - 0.2:
                new_input = max(input_duration, src_duration - new_start - 0.2)
            if new_input <= input_duration + 0.15:
                continue
            item['start'] = round(new_start, 3)
            item['input_duration'] = round(new_input, 3)
            item['output_duration'] = round(new_input / speed, 3)
            crop_box = detect_game_viewport_crop(source_path, new_start, new_input)
            if crop_box:
                item['crop_box'] = crop_box
            ok_segment, _reason = segment_is_valid_for_montage(
                source_path,
                new_start,
                new_input,
                profile=profile,
            )
            if not ok_segment:
                item['start'] = round(start, 3)
                item['input_duration'] = round(input_duration, 3)
                item['output_duration'] = round(input_duration / speed, 3)
                continue
            changed = True
            if effective_duration(items) >= MIN_FINAL_DURATION:
                break
        if not changed:
            break
    return items


def combo_is_valid(combo: tuple[dict, ...]) -> bool:
    selected = list(combo)
    for idx, candidate in enumerate(selected):
        if overlaps(candidate, selected[:idx]):
            return False
    return True


def combo_rank(combo: tuple[dict, ...]) -> float:
    duration = effective_duration(list(combo))
    score_sum = sum(item['score'] for item in combo)
    diversity = len({item.get('source_signature', item['source_index']) for item in combo})
    distance_penalty = abs(duration - TARGET_DURATION) * 0.07
    range_penalty = 0.0
    if duration < MIN_FINAL_DURATION:
        range_penalty += (MIN_FINAL_DURATION - duration) * 0.9
    if duration > MAX_FINAL_DURATION:
        range_penalty += (duration - MAX_FINAL_DURATION) * 0.35
    count_penalty = 0.05 if len(combo) == 4 and duration >= TARGET_DURATION - 6 else 0.0
    return score_sum + diversity * 0.08 - distance_penalty - range_penalty - count_penalty


def best_combo_for_size(pool: list[dict], size: int, require_range: bool, unique_sources_only: bool = False) -> list[dict]:
    ranked_combos: list[tuple[float, tuple[dict, ...]]] = []
    for combo in itertools.combinations(pool, size):
        if unique_sources_only and len({item.get('source_signature', item['source_index']) for item in combo}) != len(combo):
            continue
        if not combo_is_valid(combo):
            continue
        duration = effective_duration(list(combo))
        if require_range and not (MIN_FINAL_DURATION <= duration <= MAX_FINAL_DURATION):
            continue
        ranked_combos.append((combo_rank(combo), combo))
    if not ranked_combos:
        return []
    ranked_combos.sort(key=lambda item: item[0], reverse=True)
    explore_window = min(5, len(ranked_combos))
    chosen_idx = SELECTION_VARIANT % explore_window
    return list(ranked_combos[chosen_idx][1])


def candidate_hero_id(candidate: dict) -> str:
    path = Path(candidate.get('source_path', ''))
    parts = path.parts
    if 'hero_datasets' in parts:
        idx = parts.index('hero_datasets')
        if idx + 1 < len(parts):
            return parts[idx + 1].lower()
    label = str(candidate.get('game_name', '')).lower()
    for prefix in ('mlbb etalon ', 'hero highlights | ', 'hero highlights '):
        if prefix in label:
            return label.split(prefix, 1)[-1].strip().split()[0] if label else ''
    return ''


PUBG_RESCUE_TIERS: list[dict[str, str]] = [
    {
        'SMART_PUBG_PEAK_PERCENTILE': '34',
        'SMART_PUBG_COMBAT_MIN': '0.16',
        'SMART_PUBG_BIN_GUNFIRE_MIN': '0.07',
        'SMART_PUBG_MIN_GUNFIRE_DENSITY': '0.050',
        'SMART_PUBG_MIN_BURST_RATIO': '2.3',
        'SMART_PUBG_RELAX_MIN_GUNFIRE': '0.042',
        'SMART_PUBG_SUSTAIN_PERCENTILE': '30',
        'SMART_PUBG_GUNFIRE_PERCENTILE': '48',
        'SMART_PUBG_MOTION_PERCENTILE': '44',
        'SMART_PUBG_AUDIO_PERCENTILE': '42',
    },
    {
        'SMART_PUBG_PEAK_PERCENTILE': '28',
        'SMART_PUBG_COMBAT_MIN': '0.14',
        'SMART_PUBG_BIN_GUNFIRE_MIN': '0.055',
        'SMART_PUBG_MIN_GUNFIRE_DENSITY': '0.042',
        'SMART_PUBG_MIN_BURST_RATIO': '2.1',
        'SMART_PUBG_RELAX_MIN_GUNFIRE': '0.036',
        'SMART_PUBG_MIN_CENTER_MOTION': '0.015',
        'SMART_PUBG_SUSTAIN_PERCENTILE': '26',
        'SMART_PUBG_GUNFIRE_PERCENTILE': '44',
        'SMART_PUBG_MOTION_PERCENTILE': '40',
        'SMART_PUBG_AUDIO_PERCENTILE': '40',
    },
    {
        'SMART_PUBG_PEAK_PERCENTILE': '22',
        'SMART_PUBG_COMBAT_MIN': '0.11',
        'SMART_PUBG_BIN_GUNFIRE_MIN': '0.040',
        'SMART_PUBG_MIN_GUNFIRE_DENSITY': '0.034',
        'SMART_PUBG_MIN_BURST_RATIO': '1.9',
        'SMART_PUBG_RELAX_MIN_GUNFIRE': '0.028',
        'SMART_PUBG_MIN_CENTER_MOTION': '0.013',
        'SMART_PUBG_MIN_AUDIO_RMS': '0.006',
        'SMART_PUBG_SUSTAIN_PERCENTILE': '22',
        'SMART_PUBG_GUNFIRE_PERCENTILE': '38',
        'MIN_HIGHLIGHTS': '4',
        'MIN_FINAL_DURATION': '36',
    },
]

STANDOFF_RESCUE_TIERS: list[dict[str, str]] = [
    {
        'SMART_STANDOFF_PEAK_PERCENTILE': '28',
        'SMART_STANDOFF_COMBAT_MIN': '0.14',
        'SMART_STANDOFF_BIN_GUNFIRE_MIN': '0.07',
        'SMART_STANDOFF_MIN_GUNFIRE_DENSITY': '0.042',
        'SMART_STANDOFF_MIN_BURST_RATIO': '2.1',
        'SMART_STANDOFF_RELAX_MIN_GUNFIRE': '0.038',
        'SMART_STANDOFF_SUSTAIN_PERCENTILE': '24',
        'SMART_STANDOFF_GUNFIRE_PERCENTILE': '46',
        'SMART_STANDOFF_MOTION_PERCENTILE': '42',
        'SMART_STANDOFF_AUDIO_PERCENTILE': '40',
    },
    {
        'SMART_STANDOFF_PEAK_PERCENTILE': '22',
        'SMART_STANDOFF_COMBAT_MIN': '0.11',
        'SMART_STANDOFF_BIN_GUNFIRE_MIN': '0.050',
        'SMART_STANDOFF_MIN_GUNFIRE_DENSITY': '0.034',
        'SMART_STANDOFF_MIN_BURST_RATIO': '1.9',
        'SMART_STANDOFF_RELAX_MIN_GUNFIRE': '0.030',
        'SMART_STANDOFF_MIN_CENTER_MOTION': '0.014',
        'SMART_STANDOFF_SUSTAIN_PERCENTILE': '20',
        'SMART_STANDOFF_GUNFIRE_PERCENTILE': '40',
        'SMART_STANDOFF_MOTION_PERCENTILE': '38',
        'SMART_STANDOFF_AUDIO_PERCENTILE': '36',
    },
    {
        'SMART_STANDOFF_PEAK_PERCENTILE': '18',
        'SMART_STANDOFF_COMBAT_MIN': '0.09',
        'SMART_STANDOFF_BIN_GUNFIRE_MIN': '0.035',
        'SMART_STANDOFF_MIN_GUNFIRE_DENSITY': '0.028',
        'SMART_STANDOFF_MIN_BURST_RATIO': '1.7',
        'SMART_STANDOFF_RELAX_MIN_GUNFIRE': '0.024',
        'SMART_STANDOFF_MIN_CENTER_MOTION': '0.012',
        'SMART_STANDOFF_MIN_AUDIO_RMS': '0.006',
        'SMART_STANDOFF_SUSTAIN_PERCENTILE': '18',
        'SMART_STANDOFF_GUNFIRE_PERCENTILE': '34',
        'MIN_HIGHLIGHTS': '4',
        'MIN_FINAL_DURATION': '36',
    },
]

WOT_RESCUE_TIERS: list[dict[str, str]] = [
    {
        'SMART_WOT_PEAK_PERCENTILE': '30',
        'SMART_WOT_COMBAT_MIN': '0.14',
        'SMART_WOT_BIN_IMPACT_MIN': '0.08',
        'SMART_WOT_MIN_IMPACT_DENSITY': '0.045',
        'SMART_WOT_SUSTAIN_PERCENTILE': '26',
        'SMART_WOT_AUDIO_PERCENTILE': '46',
    },
    {
        'SMART_WOT_PEAK_PERCENTILE': '24',
        'SMART_WOT_COMBAT_MIN': '0.12',
        'SMART_WOT_BIN_IMPACT_MIN': '0.06',
        'SMART_WOT_MIN_IMPACT_DENSITY': '0.038',
        'SMART_WOT_SUSTAIN_PERCENTILE': '22',
        'SMART_WOT_AUDIO_PERCENTILE': '42',
    },
]

GUNFIRE_RESCUE_BY_PROFILE: dict[str, list[dict[str, str]]] = {
    'pubg': PUBG_RESCUE_TIERS,
    'standoff': STANDOFF_RESCUE_TIERS,
    'wot': WOT_RESCUE_TIERS,
    'world_of_tanks': WOT_RESCUE_TIERS,
}


def apply_rescue_tier(tier: dict[str, str]) -> None:
    global MIN_FINAL_DURATION, MIN_HIGHLIGHTS, MAX_HIGHLIGHTS
    for key, value in tier.items():
        os.environ[key] = value
    if 'MIN_FINAL_DURATION' in tier:
        MIN_FINAL_DURATION = float(tier['MIN_FINAL_DURATION'])
    if 'MIN_HIGHLIGHTS' in tier:
        MIN_HIGHLIGHTS = int(tier['MIN_HIGHLIGHTS'])
    if 'MAX_HIGHLIGHTS' in tier:
        MAX_HIGHLIGHTS = int(tier['MAX_HIGHLIGHTS'])


def action_montage_ready(selected: list[dict], arranged: list[dict]) -> bool:
    if len(arranged) < min(MIN_HIGHLIGHTS, 3):
        return False
    return effective_duration(arranged) >= MIN_FINAL_DURATION


def segment_is_excluded(candidate: dict) -> bool:
    source_key = candidate.get('source_signature', str(candidate['source_index']))
    segment_key = f"{source_key}:{round(candidate['start'], 3)}"
    return source_key in EXCLUDED_SOURCE_SIGNATURES or segment_key in EXCLUDED_SEGMENT_KEYS


def select_candidates(all_candidates: list[dict], source_count: int) -> list[dict]:
    if not all_candidates:
        return []
    allow_excluded_fallback = os.environ.get('SMART_ALLOW_EXCLUDED_FALLBACK', '0') == '1'
    if os.environ.get('SINGLE_HERO_MODE', '0') == '1':
        forced = (os.environ.get('SINGLE_HERO_ID') or '').strip().lower()
        if not forced:
            heroes = [candidate_hero_id(item) for item in all_candidates if candidate_hero_id(item)]
            if heroes:
                from collections import Counter

                forced = Counter(heroes).most_common(1)[0][0]
        if forced:
            filtered = [item for item in all_candidates if candidate_hero_id(item) == forced]
            if len(filtered) >= MIN_HIGHLIGHTS:
                all_candidates = filtered
    per_source_count: dict[str, int] = {}
    pool: list[dict] = []
    fallback_pool: list[dict] = []
    single_source_mode = source_count == 1 or os.environ.get('SINGLE_SOURCE_MODE') == '1'
    max_per_source = MAX_HIGHLIGHTS if single_source_mode else 2
    for candidate in all_candidates:
        source_key = candidate.get('source_signature', str(candidate['source_index']))
        segment_key = f"{source_key}:{round(candidate['start'], 3)}"
        if per_source_count.get(source_key, 0) >= max_per_source:
            continue
        excluded = segment_is_excluded(candidate)
        target_pool = fallback_pool if excluded else pool
        target_pool.append(candidate)
        per_source_count[source_key] = per_source_count.get(source_key, 0) + 1
        if len(pool) >= 16:
            break
    if len(pool) < 16 and allow_excluded_fallback:
        for candidate in fallback_pool:
            if candidate in pool:
                continue
            pool.append(candidate)
            if len(pool) >= 16:
                break
    if len(pool) < MIN_HIGHLIGHTS:
        fresh = [item for item in all_candidates if not segment_is_excluded(item)]
        if fresh:
            pool = fresh[:max(len(fresh), MIN_HIGHLIGHTS)]

    unique_pool_sources = len({item.get('source_signature', item['source_index']) for item in pool})

    if len(pool) >= 4:
        best_four = best_combo_for_size(pool, 4, True, unique_sources_only=unique_pool_sources >= 4)
        if best_four:
            return best_four
    if len(pool) >= MIN_HIGHLIGHTS:
        best_three = best_combo_for_size(pool, 3, True, unique_sources_only=unique_pool_sources >= 3)
        if best_three:
            return best_three

    fallback_options: list[list[dict]] = []
    if len(pool) >= 4:
        fallback_options.append(best_combo_for_size(pool, 4, False, unique_sources_only=unique_pool_sources >= 4))
    if len(pool) >= MIN_HIGHLIGHTS:
        fallback_options.append(best_combo_for_size(pool, 3, False, unique_sources_only=unique_pool_sources >= 3))
    fallback_options = [option for option in fallback_options if option]
    if fallback_options:
        fallback_options.sort(key=lambda combo: combo_rank(tuple(combo)), reverse=True)
        return fallback_options[0]

    selected: list[dict] = []
    for candidate in all_candidates:
        if overlaps(candidate, selected):
            continue
        selected.append(candidate)
        if len(selected) >= min(MAX_HIGHLIGHTS, max(1, len(all_candidates))):
            break
    return selected


def arrange_candidates(candidates: list[dict]) -> list[dict]:
    if len(candidates) <= 1:
        return candidates
    candidates = sorted(candidates, key=lambda item: item['score'])
    arranged: list[dict] = [candidates[0]]
    remaining = candidates[1:-1]
    previous_source = arranged[0].get('source_signature', arranged[0]['source_index'])
    while remaining:
        next_candidate = min(
            remaining,
            key=lambda item: abs(item['score'] - arranged[-1]['score']) + (0.10 if item.get('source_signature', item['source_index']) == previous_source else 0.0)
        )
        arranged.append(next_candidate)
        remaining.remove(next_candidate)
        previous_source = next_candidate.get('source_signature', next_candidate['source_index'])
    if len(candidates) > 1:
        arranged.append(candidates[-1])
    return arranged


def render_segment(candidate: dict, output_path: Path, logo_path: Path) -> float:
    source_path = Path(candidate['source_path'])
    has_audio = ffprobe_has_audio(source_path)
    speed = candidate['speed']
    profile_hint = os.environ.get('DEFAULT_GAME_PROFILE', '').lower()
    game_hint = str(candidate.get('game_name', '')).lower()
    blur_nickname = NICK_BLUR_ENABLED and (
        os.environ.get('BLUR_NICKNAME', '0') == '1'
        and ('mobile legends' in game_hint or 'hayabusa' in game_hint)
    )
    crop = candidate.get('crop_box')
    if not crop:
        crop = detect_game_viewport_crop(
            source_path,
            float(candidate['start']),
            float(candidate['input_duration']),
        )
        if crop:
            candidate['crop_box'] = crop
    crop_prefix = ''
    if crop and len(crop) == 4:
        x, y, w, h = crop
        if w > 0 and h > 0:
            crop_prefix = f'crop={w}:{h}:{x}:{y},'
    base_video_filter = (
        f'{crop_prefix}'
        f'scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,'
        f'pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,'
        f'fps={OUTPUT_FPS},'
        'eq=brightness=0.015:contrast=1.05:saturation=1.03,'
        'unsharp=5:5:0.18:5:5:0.0,'
        f'setpts=PTS/{speed:.3f},format=yuv420p'
    )
    game_audio_only = os.environ.get('SMART_GAME_AUDIO_ONLY', '1') == '1'
    audio_filter = (
        game_audio_filter_chain(speed)
        if game_audio_only
        else f'aresample=44100,atempo={speed:.3f},highpass=f=80,lowpass=f=14000,volume=1.02'
    )
    command = ['ffmpeg', '-y', '-hwaccel', 'none', '-ss', f"{candidate['start']:.3f}", '-t', f"{candidate['input_duration']:.3f}", '-i', str(source_path)]

    if logo_path.exists():
        command.extend(['-i', str(logo_path)])

    blur_chain = '[base]split[clean][nicksrc];[nicksrc]crop=w=420:h=130:x=(iw-420)/2:y=500,boxblur=14:3[nick];[clean][nick]overlay=x=(W-420)/2:y=500[stage]' if blur_nickname else '[base]null[stage]'

    if has_audio:
        if logo_path.exists():
            filter_complex = f'[0:v]{base_video_filter}[base];{blur_chain};[1:v]scale=150:-1[wm];[stage][wm]overlay=W-w-24:H-h-24[v]'
            command.extend([
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-map', '0:a:0',
                '-af', audio_filter,
            ])
        else:
            filter_complex = f'[0:v]{base_video_filter}[base];{blur_chain};[stage]null[v]'
            command.extend([
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-map', '0:a:0',
                '-af', audio_filter,
            ])
    else:
        command.extend(['-f', 'lavfi', '-t', f"{candidate['input_duration']:.3f}", '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100'])
        if logo_path.exists():
            filter_complex = f'[0:v]{base_video_filter}[base];{blur_chain};[1:v]scale=150:-1[wm];[stage][wm]overlay=W-w-24:H-h-24[v]'
            command.extend([
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-map', '2:a:0',
            ])
        else:
            filter_complex = f'[0:v]{base_video_filter}[base];{blur_chain};[stage]null[v]'
            command.extend([
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-map', '1:a:0',
            ])

    output_crf = os.environ.get('SMART_OUTPUT_CRF', '15')
    output_preset = os.environ.get('SMART_OUTPUT_PRESET', 'slow')
    command.extend([
        '-c:v', 'libx264',
        '-preset', output_preset,
        '-crf', output_crf,
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'high',
        '-c:a', 'aac',
        '-b:a', '160k',
        '-movflags', '+faststart',
        '-shortest',
        str(output_path),
    ])
    run_command(command)
    return ffprobe_duration(output_path)


def build_xfade_command(segment_paths: list[Path], durations: list[float], output_path: Path) -> list[str]:
    command: list[str] = ['ffmpeg', '-y']
    for segment_path in segment_paths:
        command.extend(['-i', str(segment_path)])
    if len(segment_paths) == 1:
        command.extend([
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '18',
            '-pix_fmt', 'yuv420p',
            '-profile:v', 'high',
            '-c:a', 'aac',
            '-b:a', '160k',
            '-movflags', '+faststart',
            str(output_path),
        ])
        return command

    filters: list[str] = []
    for idx in range(len(segment_paths)):
        filters.append(f'[{idx}:v]settb=AVTB,format=yuv420p[v{idx}]')
        filters.append(f'[{idx}:a]aresample=44100[a{idx}]')

    video_prev = 'v0'
    audio_prev = 'a0'
    cumulative = durations[0]
    for idx in range(1, len(segment_paths)):
        offset = max(0.0, cumulative - TRANSITION_DURATION)
        video_next = f'vx{idx}'
        audio_next = f'ax{idx}'
        filters.append(
            f'[{video_prev}][v{idx}]xfade=transition=fade:duration={TRANSITION_DURATION:.3f}:offset={offset:.3f}[{video_next}]'
        )
        filters.append(
            f'[{audio_prev}][a{idx}]acrossfade=d={TRANSITION_DURATION:.3f}:curve1=qsin:curve2=qsin[{audio_next}]'
        )
        video_prev = video_next
        audio_prev = audio_next
        cumulative = cumulative + durations[idx] - TRANSITION_DURATION

    filters.append(f'[{video_prev}]format=yuv420p[vout]')
    command.extend([
        '-filter_complex', ';'.join(filters),
        '-map', '[vout]',
        '-map', f'[{audio_prev}]',
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '18',
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'high',
        '-c:a', 'aac',
        '-b:a', '160k',
        '-movflags', '+faststart',
        str(output_path),
    ])
    return command


def mix_background_music(output_path: Path, music_path: Path, final_duration: float) -> None:
    if not music_path.exists() or final_duration <= 0:
        return
    mixed_path = output_path.with_name(output_path.stem + '_music.mp4')
    fade_start = max(0.0, final_duration - 1.2)
    run_command([
        'ffmpeg', '-y', '-i', str(output_path), '-stream_loop', '-1', '-i', str(music_path),
        '-filter_complex',
        f'[1:a]atrim=0:{final_duration:.3f},asetpts=N/SR/TB,afade=t=out:st={fade_start:.3f}:d=1.2,volume=0.18[musicbed];'
        f'[musicbed][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=10:release=220:makeup=1[ducked];'
        f'[0:a]volume=1.0[game];'
        f'[game][ducked]amix=inputs=2:weights=1 0.45:duration=first:normalize=0[aout]',
        '-map', '0:v', '-map', '[aout]',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(mixed_path),
    ])
    mixed_path.replace(output_path)


def load_segment_history(path: Path = SEGMENT_HISTORY_FILE) -> tuple[set[str], set[str]]:
    if not path.exists():
        return set(), set()
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return set(), set()
    sources = {str(item) for item in payload.get('source_signatures', []) if item}
    segments = {str(item) for item in payload.get('segment_keys', []) if item}
    return sources, segments


def register_segment_history(selected: list[dict], path: Path = SEGMENT_HISTORY_FILE, keep: int = 240) -> None:
    sources, segments = load_segment_history(path)
    for item in selected:
        source_key = str(item.get('source_signature', item.get('source_index', '')))
        if source_key:
            sources.add(source_key)
        segment_key = f"{source_key}:{round(float(item.get('start', 0.0)), 3)}"
        segments.add(segment_key)
    payload = {
        'source_signatures': sorted(sources)[-keep:],
        'segment_keys': sorted(segments)[-keep:],
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def telegram_curl_env() -> dict[str, str]:
    """Telegram API must not go through dead HTTP proxies from .video_bot.env."""
    env = os.environ.copy()
    for key in (
        'HTTP_PROXY',
        'HTTPS_PROXY',
        'http_proxy',
        'https_proxy',
        'ALL_PROXY',
        'all_proxy',
    ):
        env.pop(key, None)
    return env


def send_telegram_video(bot_token: str, chat_id: str, video_path: Path, caption: str) -> None:
    """Upload via curl - manual multipart often triggers Telegram HTTP 400."""
    short_cap = caption[:900]
    url = f'https://api.telegram.org/bot{bot_token}/sendVideo'
    curl_env = telegram_curl_env()
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            result = run_command(
                [
                    'curl',
                    '-sS',
                    '-m',
                    '600',
                    '-F',
                    f'chat_id={chat_id}',
                    '-F',
                    'supports_streaming=true',
                    '-F',
                    f'caption={short_cap}',
                    '-F',
                    f'video=@{video_path}',
                    url,
                ],
                capture_output=True,
                check=True,
                env=curl_env,
            )
            payload = json.loads(result.stdout)
            if not payload.get('ok'):
                raise RuntimeError(f'Telegram sendVideo failed: {payload}')
            return
        except Exception as exc:
            last_error = exc
            logging.warning('telegram sendVideo attempt %s failed: %s', attempt, exc)
            time.sleep(3 * attempt)
    doc_url = f'https://api.telegram.org/bot{bot_token}/sendDocument'
    try:
        result = run_command(
            [
                'curl',
                '-sS',
                '-m',
                '600',
                '-F',
                f'chat_id={chat_id}',
                '-F',
                f'caption={short_cap}',
                '-F',
                f'document=@{video_path}',
                doc_url,
            ],
            capture_output=True,
            check=True,
            env=curl_env,
        )
        payload = json.loads(result.stdout)
        if not payload.get('ok'):
            raise RuntimeError(f'Telegram sendDocument failed: {payload}')
        return
    except Exception as doc_exc:
        logging.warning('telegram sendDocument fallback failed: %s', doc_exc)
    raise RuntimeError(f'Telegram send failed after retries: {last_error}')


def save_analysis(output_path: Path, payload: dict) -> None:
    analysis_path = output_path.with_suffix('.json')
    analysis_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def short_file_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()[:8]


def main() -> int:
    global TARGET_DURATION, MIN_FINAL_DURATION, MAX_FINAL_DURATION, MIN_HIGHLIGHTS, MAX_HIGHLIGHTS, TRANSITION_DURATION, SELECTION_VARIANT, EXCLUDED_SOURCE_SIGNATURES, EXCLUDED_SEGMENT_KEYS, NICK_BLUR_ENABLED, SEND_TELEGRAM

    env = load_env(ENV_FILE)
    for key, value in env.items():
        os.environ.setdefault(key, value)

    TARGET_DURATION = float(os.environ.get('TARGET_DURATION', os.environ.get('SMART_TARGET_DURATION', str(TARGET_DURATION))))
    MIN_FINAL_DURATION = float(os.environ.get('MIN_FINAL_DURATION', str(MIN_FINAL_DURATION)))
    MAX_FINAL_DURATION = float(os.environ.get('MAX_FINAL_DURATION', str(MAX_FINAL_DURATION)))
    MIN_HIGHLIGHTS = int(os.environ.get('MIN_HIGHLIGHTS', str(MIN_HIGHLIGHTS)))
    MAX_HIGHLIGHTS = int(os.environ.get('MAX_HIGHLIGHTS', str(MAX_HIGHLIGHTS)))
    TRANSITION_DURATION = float(os.environ.get('TRANSITION_DURATION', str(TRANSITION_DURATION)))
    SELECTION_VARIANT = int(os.environ.get('SELECTION_VARIANT', str(SELECTION_VARIANT)))
    EXCLUDED_SOURCE_SIGNATURES = {sig for sig in os.environ.get('EXCLUDED_SOURCE_SIGNATURES', '').split(',') if sig}
    EXCLUDED_SEGMENT_KEYS = {sig for sig in os.environ.get('EXCLUDED_SEGMENT_KEYS', '').split(',') if sig}
    seg_hist_path = Path(os.environ.get('SEGMENT_HISTORY_FILE', str(SEGMENT_HISTORY_FILE)))
    if os.environ.get('OVERNIGHT_FRESH_SEGMENTS', '0') == '1':
        hist_sources, hist_segments = set(), set()
    else:
        hist_sources, hist_segments = load_segment_history(seg_hist_path)
    EXCLUDED_SOURCE_SIGNATURES |= hist_sources
    EXCLUDED_SEGMENT_KEYS |= hist_segments
    NICK_BLUR_ENABLED = os.environ.get('BLUR_NICKNAME', '0') == '1'
    SEND_TELEGRAM = os.environ.get('SEND_TELEGRAM', '1') == '1'
    profile_boot = os.environ.get('QUEUE_GAME_PROFILE', os.environ.get('DEFAULT_GAME_PROFILE', '')).lower()
    overnight = os.environ.get('OVERNIGHT_BATCH', '0') == '1'
    if (
        os.environ.get('SINGLE_SOURCE_MODE') == '1'
        and os.environ.get('STRICT_GAMEPLAY', '0') != '1'
        and not overnight
        and profile_boot not in ('pubg',)
    ):
        MIN_HIGHLIGHTS = max(2, min(MIN_HIGHLIGHTS, 2))
        MIN_FINAL_DURATION = max(22.0, MIN_FINAL_DURATION - 11.0)

    setup_logging(DEFAULT_LOG_FILE)
    lock_handle = acquire_lock(DEFAULT_LOCK_FILE)
    try:
        return _run_smart_edit(
            env,
            bot_token=os.environ.get('TG_BOT_TOKEN', env.get('TG_BOT_TOKEN', '')),
            default_chat_id=os.environ.get('TG_CHAT_ID', env.get('TG_CHAT_ID', '')),
            impersonate=os.environ.get('YTDLP_IMPERSONATE', env.get('YTDLP_IMPERSONATE', 'chrome-131')),
        )
    finally:
        lock_handle.close()


def _run_smart_edit(
    env: dict[str, str],
    *,
    bot_token: str,
    default_chat_id: str,
    impersonate: str,
) -> int:
    queue_file = Path(os.environ.get('QUEUE_FILE', str(QUEUE_FILE)))
    output_dir = Path(os.environ.get('OUTPUT_DIR', str(DEFAULT_OUTPUT_DIR)))
    logo_path = Path(os.environ.get('LOGO_FILE', str(DEFAULT_LOGO_FILE)))
    music_path = Path(os.environ.get('BACKGROUND_MUSIC_FILE', env.get('BACKGROUND_MUSIC_FILE', '/root/background_music.mp3')))

    if not bot_token:
        logging.error('TG_BOT_TOKEN is not configured')
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)

    batch_lines = take_batch_lines(queue_file, int(os.environ.get('MAX_SOURCES', str(MAX_SOURCES))))
    if not batch_lines:
        logging.info('queue is empty')
        return 0

    temp_dir = Path(tempfile.mkdtemp(prefix='smart-video-editor-'))
    logging.info('processing %s sources from %s', len(batch_lines), queue_file)
    try:
        sources: list[dict] = []
        batch_chat_id = ''
        for idx, line in enumerate(batch_lines):
            source_ref, game_name, line_chat_id = parse_queue_line(line, default_chat_id)
            if not source_ref:
                logging.warning('skipping invalid queue entry: %s', line)
                continue
            if batch_chat_id and line_chat_id != batch_chat_id:
                logging.warning('mixed chat ids are not supported in one batch, skipping: %s', line)
                continue
            batch_chat_id = line_chat_id
            work_dir = temp_dir / f'source_{idx}'
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                source_path = maybe_download_source(source_ref, work_dir, impersonate)
            except Exception as exc:
                logging.warning('failed to prepare source %s: %s', source_ref, exc)
                continue
            analysis = analyze_video(source_path)
            source_signature = file_sha256(source_path)
            logging.info(
                'analyzed source=%s duration=%.2fs bins=%s long_mode=%s sha=%s',
                source_path,
                analysis['duration'],
                analysis['bins'],
                analysis.get('long_mode', False),
                source_signature[:12],
            )
            sources.append({
                'source_index': idx,
                'source_signature': source_signature,
                'source_path': source_path,
                'game_name': game_name,
                'chat_id': line_chat_id,
                'analysis': analysis,
            })

        if not sources:
            logging.error('no usable sources available in current batch')
            return 1

        profile = resolve_profile([item['game_name'] for item in sources], env)
        try:
            from montage_env import profile_montage_env
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from montage_env import profile_montage_env
        if os.environ.get('SMART_FORCE_PASSTHROUGH_AUDIO', '1') == '1':
            for key, value in profile_montage_env(profile).items():
                os.environ[key] = value
        logging.info(
            'using profile=%s (queue_game_profile=%s default=%s labels=%s)',
            profile,
            os.environ.get('QUEUE_GAME_PROFILE', ''),
            env.get('DEFAULT_GAME_PROFILE', ''),
            [item['game_name'] for item in sources],
        )
        if profile == 'mobile_legends' and os.environ.get('STRICT_GAMEPLAY', '0') == '1':
            csv_path = Path(os.environ.get('GAMEPLAY_CSV', '/root/data/mlbb/gameplay_filter_latest.csv'))
            lookup = load_csv_lookup(csv_path)
            filtered_sources: list[dict] = []
            for source in sources:
                ok_src, src_score, src_reason = is_gameplay_video(
                    source['source_path'], csv_lookup=lookup
                )
                if ok_src:
                    filtered_sources.append(source)
                else:
                    logging.warning(
                        'skip non-gameplay source=%s score=%.2f reason=%s',
                        source['source_path'].name,
                        src_score,
                        src_reason,
                    )
            sources = filtered_sources
            if not sources:
                logging.error('no usable MLBB gameplay sources after STRICT_GAMEPLAY filter')
                return 1

        global_values = {
            'motion': [],
            'center_motion': [],
            'sharpness': [],
            'scene': [],
            'audio': [],
            'gunfire': [],
        }
        for source in sources:
            analysis = source['analysis']
            for key in global_values:
                if key in analysis:
                    global_values[key].extend(float(value) for value in analysis[key])

        def collect_candidates(*, relax_gate: bool = False) -> list[dict]:
            found: list[dict] = []
            for source in sources:
                candidates = build_candidates(
                    source['source_index'],
                    source['source_path'],
                    source['game_name'],
                    source['analysis'],
                    global_values,
                    profile,
                    source['source_signature'],
                    relax_segment_gate=relax_gate,
                )
                logging.info('source=%s yielded %s candidates', source['source_path'].name, len(candidates))
                found.extend(candidates)
            return found

        single_source = len(sources) == 1 and os.environ.get('SINGLE_SOURCE_MODE') == '1'
        strict_gameplay = os.environ.get('STRICT_GAMEPLAY', '0') == '1'
        all_candidates = collect_candidates()
        if not all_candidates and single_source and not strict_gameplay:
            logging.warning('single-source retry with relaxed segment gate')
            all_candidates = collect_candidates(relax_gate=True)

        selected: list[dict] = []
        arranged: list[dict] = []
        eff_duration = 0.0
        rescue_attempts = (
            list(GUNFIRE_RESCUE_BY_PROFILE.get(profile, []))
            if single_source and profile in GUNFIRE_RESCUE_BY_PROFILE
            else []
        )
        while True:
            if not all_candidates and rescue_attempts:
                tier = rescue_attempts.pop(0)
                logging.warning('%s rescue scoring tier: %s', profile, tier)
                apply_rescue_tier(tier)
                all_candidates = collect_candidates(relax_gate=True)
            if not all_candidates:
                break

            all_candidates.sort(key=lambda item: item['score'], reverse=True)
            selected = select_candidates(all_candidates, len(sources))
            arranged = arrange_candidates(selected)
            if profile in ('mobile_legends', 'pubg', 'standoff', 'wot', 'world_of_tanks'):
                arranged = extend_selected_to_min_duration(arranged, profile)
            eff_duration = effective_duration(arranged)
            logging.info('selected %s clips, effective duration %.2fs', len(arranged), eff_duration)
            if action_montage_ready(selected, arranged) or not rescue_attempts:
                break
            logging.warning(
                '%s montage insufficient (%s clips, %.1fs) — trying looser rescue tier',
                profile,
                len(arranged),
                eff_duration,
            )
            all_candidates = []

        if not all_candidates:
            logging.error('smart scoring produced no candidates')
            return 1
        if eff_duration < MIN_FINAL_DURATION:
            logging.error(
                'montage too short (%.1fs < %.1fs) — not enough real gameplay segments',
                eff_duration,
                MIN_FINAL_DURATION,
            )
            return 1

        segment_paths: list[Path] = []
        segment_durations: list[float] = []
        for index, candidate in enumerate(arranged):
            segment_path = temp_dir / f'segment_{index:02d}.mp4'
            actual_duration = render_segment(candidate, segment_path, logo_path)
            segment_paths.append(segment_path)
            segment_durations.append(actual_duration)
            candidate['rendered_duration'] = round(actual_duration, 3)

        basename = os.environ.get('OUTPUT_BASENAME', '').strip()
        if basename:
            game_slug = sanitize_slug([basename])
        else:
            unique_names = list(dict.fromkeys(item['game_name'] for item in sources if item.get('game_name')))
            game_slug = sanitize_slug(unique_names[:1] or ['smart_edit'])
        output_path = output_dir / f'{game_slug}_{time.strftime("%Y%m%d_%H%M%S")}.mp4'
        final_command = build_xfade_command(segment_paths, segment_durations, output_path)
        run_command(final_command)
        final_duration = ffprobe_duration(output_path)
        if os.environ.get('SMART_ADD_MUSIC', '0') == '1' and music_path.exists():
            mix_background_music(output_path, music_path, final_duration)
            final_duration = ffprobe_duration(output_path)
        file_id = short_file_id(output_path)
        # `game_name` in the queue is a user-provided label (caption/folder/tag), not a guaranteed hero in-frame.
        hints = [item.get('game_name', '').strip() for item in sources if item.get('game_name')]
        unique_hints = []
        for h in hints:
            if h and h not in unique_hints:
                unique_hints.append(h)
        hint_text = ''
        if unique_hints:
            joined_hint = ', '.join(unique_hints[:3])
            hint_text = f' | hint={joined_hint[:60]}'
        duration_label = f'{final_duration:.1f}s'
        if os.environ.get('ETALON_MONTAGE', '0') == '1':
            hero_slug = (os.environ.get('SINGLE_HERO_ID') or '').strip()
            if not hero_slug and arranged:
                hero_slug = candidate_hero_id(arranged[0])
            hero_part = f' | {hero_slug.replace("_", " ").title()}' if hero_slug else ''
            caption = (
                f'Etalon MLBB{hero_part} | {len(arranged)} clips | {duration_label} '
                f'(цель {int(MIN_FINAL_DURATION)}–{int(MAX_FINAL_DURATION)} с) | id={file_id}{hint_text}'
            )
        else:
            custom = os.environ.get('MONTAGE_CAPTION', '').strip()
            caption = custom or (
                f'Smart Edit v1.1 | {profile.replace("_", " ").title()}{hint_text} | '
                f'{duration_label} | id={file_id}'
            )
        save_analysis(output_path, {
            'profile': profile,
            'target_duration': TARGET_DURATION,
            'min_final_duration': MIN_FINAL_DURATION,
            'max_final_duration': MAX_FINAL_DURATION,
            'final_duration': final_duration,
            'output_id': file_id,
            'sources': [
                {
                    'path': str(item['source_path']),
                    'game_name': item['game_name'],
                    'duration': item['analysis']['duration'],
                }
                for item in sources
            ],
            'selected_segments': arranged,
        })
        if SEND_TELEGRAM:
            try:
                send_telegram_video(bot_token, batch_chat_id or default_chat_id, output_path, caption)
            except Exception as exc:
                logging.warning('telegram send failed for %s: %s', output_path.name, exc)
        register_segment_history(
            arranged,
            path=Path(os.environ.get('SEGMENT_HISTORY_FILE', str(SEGMENT_HISTORY_FILE))),
        )
        if os.environ.get('SMART_SKIP_MARK_USED', '0') != '1':
            mark_used([Path(item['source_path']) for item in sources])
        drop_first_queue_lines(queue_file, len(batch_lines))
        logging.info('smart edit completed successfully: %s', output_path)
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
