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
    from gameplay_gate import segment_is_valid_for_montage
    from source_freshness import mark_used
    from mlbb_popularity import popularity_boost
    from mlbb_popularity import extract_video_id as pop_video_id
except ImportError:
    import sys

    sys.path.insert(0, '/usr/local/bin')
    from gameplay_gate import segment_is_valid_for_montage
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
NICK_BLUR_ENABLED = os.environ.get('BLUR_NICKNAME', '1') == '1'
SEND_TELEGRAM = os.environ.get('SEND_TELEGRAM', '1') == '1'
WINDOW_SECONDS = 1.0
SAMPLE_FPS = float(os.environ.get('SMART_SAMPLE_FPS', '4.0'))
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
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info('another smart editor instance is already running, exiting')
        raise SystemExit(0)
    return handle


def run_command(args: list[str], *, capture_output: bool = False, check: bool = True, text: bool = True, input_data=None):
    logging.debug('running command: %s', ' '.join(shlex.quote(arg) for arg in args))
    return subprocess.run(args, capture_output=capture_output, check=check, text=text, input=input_data)


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


def detect_profile(game_names: list[str], env: dict[str, str]) -> str:
    joined = ' '.join(game_names).lower()
    default_profile = env.get('DEFAULT_GAME_PROFILE', 'generic').lower()
    if 'mobile legends' in joined or 'mlbb' in joined or default_profile == 'mobile_legends':
        return 'mobile_legends'
    if 'pubg' in joined or 'playerunknown' in joined or default_profile == 'pubg':
        return 'pubg'
    return 'generic'


def brightness_score(brightness: float) -> float:
    return float(max(0.0, 1.0 - abs(brightness - 0.58) / 0.38))


def saturation_score(saturation: float) -> float:
    return float(max(0.0, 1.0 - abs(saturation - 0.36) / 0.34))


def parse_queue_line(line: str, default_chat_id: str) -> tuple[str, str, str]:
    parts = line.split('|')
    source = parts[0].strip() if parts else ''
    game = parts[1].strip() if len(parts) > 1 and parts[1].strip() else 'Telegram upload'
    chat_id = parts[2].strip() if len(parts) > 2 and parts[2].strip() else default_chat_id
    return source, game, chat_id


def maybe_download_source(source: str, temp_dir: Path, impersonate: str) -> Path:
    if source.startswith('http://') or source.startswith('https://'):
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
        ])
        files = sorted(temp_dir.glob('source_*'))
        if not files:
            raise RuntimeError(f'yt-dlp did not produce an output file for {source}')
        return files[-1]
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f'source file not found: {source}')
    return path


def analyze_audio(path: Path, duration: float, bins: int) -> np.ndarray:
    if not ffprobe_has_audio(path):
        return np.zeros(bins, dtype=np.float32)
    result = run_command([
        'ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '11025', '-f', 's16le', '-'
    ], capture_output=True, text=False)
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return np.zeros(bins, dtype=np.float32)
    samples /= 32768.0
    samples_per_bin = max(int(len(samples) / max(duration, 1e-3) * WINDOW_SECONDS), 1)
    energy = np.zeros(bins, dtype=np.float32)
    for idx in range(bins):
        start = idx * samples_per_bin
        end = min(len(samples), start + samples_per_bin)
        if end <= start:
            continue
        chunk = samples[start:end]
        energy[idx] = float(np.sqrt(np.mean(chunk ** 2)))
    return energy


def analyze_video(path: Path) -> dict:
    duration = ffprobe_duration(path)
    if duration <= 0:
        raise RuntimeError(f'could not determine duration for {path}')
    bins = max(1, int(math.ceil(duration / WINDOW_SECONDS)))
    motion = np.zeros(bins, dtype=np.float32)
    center_motion = np.zeros(bins, dtype=np.float32)
    sharpness = np.zeros(bins, dtype=np.float32)
    brightness = np.zeros(bins, dtype=np.float32)
    saturation = np.zeros(bins, dtype=np.float32)
    scene = np.zeros(bins, dtype=np.float32)
    counts = np.zeros(bins, dtype=np.float32)

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f'failed to open video with OpenCV: {path}')
    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sample_every = max(int(round(native_fps / SAMPLE_FPS)), 1)
    frame_idx = 0
    prev_gray = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % sample_every != 0:
            frame_idx += 1
            continue
        timestamp = frame_idx / native_fps
        bin_idx = min(bins - 1, int(timestamp // WINDOW_SECONDS))
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
        prev_gray = gray
        frame_idx += 1

    cap.release()
    counts = np.where(counts > 0, counts, 1.0)
    motion /= counts
    center_motion /= counts
    sharpness /= counts
    brightness /= counts
    saturation /= counts
    scene /= counts
    audio = analyze_audio(path, duration, bins)

    return {
        'duration': duration,
        'bins': bins,
        'motion': motion,
        'center_motion': center_motion,
        'sharpness': sharpness,
        'brightness': brightness,
        'saturation': saturation,
        'scene': scene,
        'audio': audio,
    }


def moving_average(values: np.ndarray) -> np.ndarray:
    if len(values) <= 2:
        return values.copy()
    kernel = np.array([0.2, 0.6, 0.2], dtype=np.float32)
    return np.convolve(values, kernel, mode='same')


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
    raw_scores = np.zeros(bins, dtype=np.float32)
    bursts = np.zeros(bins, dtype=np.float32)
    for idx in range(bins):
        motion = robust_scale(global_values['motion'], float(analysis['motion'][idx]))
        center = robust_scale(global_values['center_motion'], float(analysis['center_motion'][idx]))
        sharp = robust_scale(global_values['sharpness'], float(analysis['sharpness'][idx]))
        scene = robust_scale(global_values['scene'], float(analysis['scene'][idx]))
        audio = robust_scale(global_values['audio'], float(analysis['audio'][idx]))
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
            # PUBG stream: emphasize motion bursts and gunfire audio spikes.
            base = (
                0.30 * motion +
                0.14 * center +
                0.20 * audio +
                0.14 * scene +
                0.10 * sharp +
                0.07 * bright +
                0.05 * sat
            )
            base += 0.08 * max(0.0, audio - 0.4)
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
        if motion < 0.14 and audio < 0.16 and scene < 0.08:
            penalty += 0.20
        if bright < 0.18:
            penalty += 0.10
        if sharp < 0.12:
            penalty += 0.08
        raw_scores[idx] = max(0.0, base + 0.24 * burst - penalty)
        bursts[idx] = burst

    smooth_scores = moving_average(raw_scores)
    candidates: list[dict] = []
    peak_threshold = float(np.percentile(smooth_scores, 68)) if bins > 4 else float(smooth_scores.max())
    sustain_threshold = float(np.percentile(smooth_scores, 48)) if bins > 4 else peak_threshold * 0.72
    motion_threshold = float(np.percentile(analysis['motion'], 55)) if bins > 3 else float(np.max(analysis['motion']))
    scene_threshold = float(np.percentile(analysis['scene'], 58)) if bins > 3 else float(np.max(analysis['scene']))
    audio_threshold = float(np.percentile(analysis['audio'], 52)) if bins > 3 else float(np.max(analysis['audio']))

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
                float(analysis['audio'][probe]) >= audio_threshold
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
                float(analysis['audio'][probe]) >= audio_threshold
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
        mean_audio = float(np.mean(analysis['audio'][region_slice]))
        desired_duration = max(9.5, min(15.0, (right - left + 1) * WINDOW_SECONDS + 3.2))
        desired_pre = min(5.2, max(3.6, desired_duration * 0.34))
        desired_post = desired_duration - desired_pre
        peak_time = idx * WINDOW_SECONDS
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
        input_duration = max(9.0, min(15.0, end - start))
        end = min(analysis['duration'], start + input_duration)
        start = max(0.0, end - input_duration)

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

        vid_pop = pop_video_id(f"{source_path} {game_name}")
        if vid_pop:
            combo_score += popularity_boost(vid_pop)

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
                # Allow env overrides only to be stricter (never looser), to avoid
                # letting non-gameplay/promo segments slip into montages.
                default_min_hud = 14.0
                # TikTok gameplay often has subtitles/overlays; too-low max_text
                # makes montage fail even when HUD is present and gameplay is real.
                default_max_text = 0.35
                default_max_cartoon = 0.55
                env_min_hud = float(os.environ.get('SMART_MIN_HUD', str(default_min_hud)))
                env_max_text = float(os.environ.get('SMART_MAX_OVERLAY_TEXT', str(default_max_text)))
                env_max_cartoon = float(os.environ.get('SMART_MAX_CARTOON_RATIO', str(default_max_cartoon)))
                gate_kwargs = {
                    'min_hud': max(default_min_hud, env_min_hud),
                    'max_text': min(default_max_text, env_max_text),
                    'max_cartoon_ratio': min(default_max_cartoon, env_max_cartoon),
                }
            except ValueError:
                gate_kwargs = {}
        if relax_segment_gate:
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
        pruned.append(candidate)
        if len(pruned) >= 4:
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
    return any(candidate_overlap_seconds(candidate, existing) > min(candidate['input_duration'], existing['input_duration']) * 0.35 for existing in selected)


def effective_duration(selected: list[dict]) -> float:
    if not selected:
        return 0.0
    total = sum(item['output_duration'] for item in selected)
    total -= TRANSITION_DURATION * (len(selected) - 1)
    return total


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
        range_penalty += (MIN_FINAL_DURATION - duration) * 0.35
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


def select_candidates(all_candidates: list[dict], source_count: int) -> list[dict]:
    if not all_candidates:
        return []
    per_source_count: dict[str, int] = {}
    pool: list[dict] = []
    fallback_pool: list[dict] = []
    for candidate in all_candidates:
        source_key = candidate.get('source_signature', str(candidate['source_index']))
        segment_key = f"{source_key}:{round(candidate['start'], 3)}"
        if per_source_count.get(source_key, 0) >= 2:
            continue
        excluded = source_key in EXCLUDED_SOURCE_SIGNATURES or segment_key in EXCLUDED_SEGMENT_KEYS
        target_pool = fallback_pool if excluded else pool
        target_pool.append(candidate)
        per_source_count[source_key] = per_source_count.get(source_key, 0) + 1
        if len(pool) >= 16:
            break
    if len(pool) < 16:
        for candidate in fallback_pool:
            if candidate in pool:
                continue
            pool.append(candidate)
            if len(pool) >= 16:
                break
    if len(pool) < MIN_HIGHLIGHTS:
        pool = all_candidates[:max(len(all_candidates), MIN_HIGHLIGHTS)]

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
        'mobile legends' in game_hint or 'hayabusa' in game_hint or profile_hint == 'mobile_legends'
    )
    base_video_filter = (
        f'scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,'
        f'pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,'
        f'fps={OUTPUT_FPS},'
        'eq=brightness=0.015:contrast=1.05:saturation=1.03,'
        'unsharp=5:5:0.18:5:5:0.0,'
        f'setpts=PTS/{speed:.3f},format=yuv420p'
    )
    audio_filter = f'aresample=44100,atempo={speed:.3f},highpass=f=80,lowpass=f=14000,volume=1.02'
    command = ['ffmpeg', '-y', '-ss', f"{candidate['start']:.3f}", '-t', f"{candidate['input_duration']:.3f}", '-i', str(source_path)]

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

    command.extend([
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '18',
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


def send_telegram_video(bot_token: str, chat_id: str, video_path: Path, caption: str) -> None:
    """Upload via curl - manual multipart often triggers Telegram HTTP 400."""
    short_cap = caption[:900]
    url = f'https://api.telegram.org/bot{bot_token}/sendVideo'
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
    hist_sources, hist_segments = load_segment_history()
    EXCLUDED_SOURCE_SIGNATURES |= hist_sources
    EXCLUDED_SEGMENT_KEYS |= hist_segments
    NICK_BLUR_ENABLED = os.environ.get('BLUR_NICKNAME', '1') == '1'
    SEND_TELEGRAM = os.environ.get('SEND_TELEGRAM', '1') == '1'
    if os.environ.get('SINGLE_SOURCE_MODE') == '1':
        MIN_HIGHLIGHTS = max(2, min(MIN_HIGHLIGHTS, 2))
        MIN_FINAL_DURATION = max(22.0, MIN_FINAL_DURATION - 11.0)

    setup_logging(DEFAULT_LOG_FILE)
    acquire_lock(DEFAULT_LOCK_FILE)

    queue_file = Path(os.environ.get('QUEUE_FILE', str(QUEUE_FILE)))
    output_dir = Path(os.environ.get('OUTPUT_DIR', str(DEFAULT_OUTPUT_DIR)))
    logo_path = Path(os.environ.get('LOGO_FILE', str(DEFAULT_LOGO_FILE)))
    music_path = Path(os.environ.get('BACKGROUND_MUSIC_FILE', env.get('BACKGROUND_MUSIC_FILE', '/root/background_music.mp3')))
    impersonate = os.environ.get('YTDLP_IMPERSONATE', env.get('YTDLP_IMPERSONATE', 'chrome-131'))
    bot_token = os.environ.get('TG_BOT_TOKEN', env.get('TG_BOT_TOKEN', ''))
    default_chat_id = os.environ.get('TG_CHAT_ID', env.get('TG_CHAT_ID', ''))

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
            logging.info('analyzed source=%s duration=%.2fs bins=%s sha=%s', source_path, analysis['duration'], analysis['bins'], source_signature[:12])
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

        profile = detect_profile([item['game_name'] for item in sources], env)
        logging.info('using profile=%s', profile)
        global_values = {
            'motion': [],
            'center_motion': [],
            'sharpness': [],
            'scene': [],
            'audio': [],
        }
        for source in sources:
            analysis = source['analysis']
            for key in global_values:
                global_values[key].extend(float(value) for value in analysis[key])

        all_candidates: list[dict] = []
        single_source = len(sources) == 1 and os.environ.get('SINGLE_SOURCE_MODE') == '1'
        for source in sources:
            candidates = build_candidates(
                source['source_index'],
                source['source_path'],
                source['game_name'],
                source['analysis'],
                global_values,
                profile,
                source['source_signature'],
            )
            logging.info('source=%s yielded %s candidates', source['source_path'].name, len(candidates))
            all_candidates.extend(candidates)

        if not all_candidates and single_source:
            logging.warning('single-source retry with relaxed segment gate')
            source = sources[0]
            all_candidates = build_candidates(
                source['source_index'],
                source['source_path'],
                source['game_name'],
                source['analysis'],
                global_values,
                profile,
                source['source_signature'],
                relax_segment_gate=True,
            )

        all_candidates.sort(key=lambda item: item['score'], reverse=True)
        if not all_candidates:
            logging.error('smart scoring produced no candidates')
            return 1

        selected = select_candidates(all_candidates, len(sources))
        arranged = arrange_candidates(selected)
        logging.info('selected %s clips, effective duration %.2fs', len(arranged), effective_duration(arranged))

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
        caption = f'Smart Edit v1.1 | {profile.replace("_", " ").title()}{hint_text} | {round(final_duration)}s | id={file_id}'
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
        register_segment_history(arranged)
        mark_used([Path(item['source_path']) for item in sources])
        drop_first_queue_lines(queue_file, len(batch_lines))
        logging.info('smart edit completed successfully: %s', output_path)
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
