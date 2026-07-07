#!/usr/bin/env python3
"""
Industry-style highlight detection: PANNs audio + CLIP exemplars + game rules + optional LR meta-model.

Replaces score_pubg_gunfire_audio / center_edge as PRIMARY signals.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("highlight_scorer")

def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


REPO_ROOT = _repo_root()
EXEMPLAR_ROOT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO_ROOT / "data" / "highlight_exemplars")))
CLASSIFIER_PATH = Path(
    os.environ.get(
        "HIGHLIGHT_CLASSIFIER_PATH",
        str(REPO_ROOT / "data" / "mlbb" / "highlight_classifier.joblib"),
    )
)
OWNER_LABEL_PROFILES = frozenset({"pubg", "standoff", "mobile_legends", "genshin", "wot"})

_OWNER_LABEL_FILES: dict[str, tuple[str, str]] = {
    "pubg": ("PUBG_OWNER_LABELS_PATH", "pubg_owner_labels.json"),
    "standoff": ("STANDOFF_OWNER_LABELS_PATH", "standoff_owner_labels.json"),
    "mobile_legends": ("MLBB_OWNER_LABELS_PATH", "mobile_legends_owner_labels.json"),
    "genshin": ("GENSHIN_OWNER_LABELS_PATH", "genshin_owner_labels.json"),
    "wot": ("WOT_OWNER_LABELS_PATH", "wot_owner_labels.json"),
}


def _owner_labels_path(profile: str) -> Path | None:
    profile = normalize_profile(profile)
    if profile not in OWNER_LABEL_PROFILES:
        return None
    env_key, default_name = _OWNER_LABEL_FILES[profile]
    path = Path(os.environ.get(env_key, str(REPO_ROOT / "data" / default_name)))
    if path.exists():
        return path
    fallback = Path(f"/root/data/mlbb/{default_name}")
    if fallback.exists():
        return fallback
    legacy = Path("/root/data/mlbb/pubg_owner_labels.json")
    if profile == "pubg" and legacy.exists():
        return legacy
    return path if path.exists() else None


def _vod_segment_labels_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_VOD_SEGMENT_LABELS",
            str(Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "vod_segment_labels.json"),
        )
    )


def _video_id_from_path(video_path: Path) -> str:
    stem = video_path.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _owner_label_pad(label: str) -> float:
    if label == "bad":
        return float(os.environ.get("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90"))
    if label == "good":
        return float(os.environ.get("HIGHLIGHT_OWNER_GOOD_PAD_SEC", "45"))
    return float(os.environ.get("HIGHLIGHT_SOFT_BAD_PAD_SEC", "60"))


def _labels_from_vod_segment_store(video_path: Path, profile: str) -> list[dict]:
    """Owner 👍/👎 on sent VOD clips — must block/rescore on next scan."""
    if normalize_profile(profile) != "mobile_legends":
        return []
    path = _vod_segment_labels_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vid = _video_id_from_path(video_path)
    out: list[dict] = []
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for row in data.get(bucket, []):
            sid = str(row.get("segment_id", ""))
            vod_field = str(row.get("vod", ""))
            row_vid = ""
            if vod_field:
                vp = Path(vod_field)
                row_vid = vp.stem[3:] if vp.stem.startswith("yt_") else vp.stem
            elif "_" in sid:
                row_vid = sid.rsplit("_", 1)[0]
            if row_vid != vid:
                continue
            time_sec = row.get("peak_start")
            if time_sec is None:
                time_sec = row.get("start")
            if time_sec is None and "_" in sid:
                try:
                    time_sec = float(sid.rsplit("_", 1)[-1])
                except ValueError:
                    continue
            if time_sec is None:
                continue
            out.append(
                {
                    "time_sec": float(time_sec),
                    "label": label,
                    "source": "vod_segment_labels",
                    "reason": str(row.get("reason") or ""),
                    "by_chat": str(row.get("by_chat") or ""),
                }
            )
    return out


QUERY_CONFIG = Path(
    os.environ.get("HIGHLIGHT_QUERY_CONFIG", str(REPO_ROOT / "config" / "highlight_queries.yaml"))
)

WINDOW_SEC = float(os.environ.get("HIGHLIGHT_WINDOW_SEC", "10"))
STEP_SEC = float(os.environ.get("HIGHLIGHT_STEP_SEC", "2"))
MIN_GAP_SEC = float(os.environ.get("HIGHLIGHT_MIN_GAP_SEC", "90"))
MIN_CLIPS = int(os.environ.get("HIGHLIGHT_MIN_CLIPS", "3"))
TARGET_CLIPS = int(os.environ.get("HIGHLIGHT_TARGET_CLIPS", "4"))

PANN_GUN_MIN = float(os.environ.get("HIGHLIGHT_PANN_GUN_MIN", "0.25"))
PANN_GUN_INFERENCE_FLOOR = float(os.environ.get("HIGHLIGHT_PANN_INFERENCE_FLOOR", "0.18"))
PANN_GUN_SPEECH_RATIO_MIN = float(os.environ.get("HIGHLIGHT_PANN_GUN_SPEECH_RATIO", "0.08"))
CLIP_MIN_SHOOTER = float(os.environ.get("HIGHLIGHT_CLIP_MIN_SHOOTER", "0.10"))
CLASSIFIER_MIN = float(os.environ.get("HIGHLIGHT_CLASSIFIER_MIN", "0.6"))

PANN_GUN_IDX = {
    "gunshot": 427,
    "machine_gun": 428,
    "explosion": 426,
    "artillery": 430,
    "speech": 0,
    "music": 137,
}

GAME_LABELS = {
    "pubg": "PUBG",
    "standoff": "Standoff",
    "mobile_legends": "MLBB",
    "genshin": "Genshin",
    "wot": "WoT",
}

SHOOTER_PROFILES = frozenset({"pubg", "standoff"})


def owner_anchors_enabled() -> bool:
    """Hard inject owner windows into stage1 — off by default in inference."""
    return os.environ.get("HIGHLIGHT_USE_OWNER_ANCHORS", "0") == "1"


def classifier_path_for_profile(profile: str) -> Path:
    profile = normalize_profile(profile)
    override = os.environ.get("HIGHLIGHT_CLASSIFIER_PATH", "").strip()
    if override:
        return Path(override)
    per_game = REPO_ROOT / "data" / "mlbb" / f"highlight_classifier_{profile}.joblib"
    if per_game.exists():
        return per_game
    return CLASSIFIER_PATH


def _labels_for_vod(video_path: Path, profile: str) -> list[dict]:
    if normalize_profile(profile) == "mobile_legends":
        from mlbb_owner_learning import owner_labels_for_vod_scan

        return owner_labels_for_vod_scan(video_path, profile)
    from vod_owner_learning import owner_labels_for_vod_scan

    return owner_labels_for_vod_scan(video_path, profile)


def vod_has_owner_labels(video_path: Path, profile: str) -> bool:
    return bool(_labels_for_vod(video_path, profile))


def soft_anchor_enabled(video_path: Path, profile: str) -> bool:
    """Boost (not inject) owner good windows when VOD has labels in JSON."""
    if os.environ.get("HIGHLIGHT_SOFT_ANCHOR", "1") == "0":
        return False
    if owner_anchors_enabled():
        return False
    return vod_has_owner_labels(video_path, profile)


def _owner_bad_blocks_scan(row: dict, profile: str) -> bool:
    """Only real owner 👎 should veto kill-banner windows — not auto backfill noise."""
    if normalize_profile(profile) != "mobile_legends":
        return True
    source = str(row.get("source") or "")
    if source == "vod_segment_backfill":
        return os.environ.get("MLBB_BLOCK_BACKFILL_BAD", "0") == "1"
    if source == "vod_segment_labels":
        if row.get("by_chat"):
            return True
        reason = str(row.get("reason") or "").strip().lower()
        if reason in ("boring", "unspecified", "", "button_dislike"):
            return os.environ.get("MLBB_BLOCK_AUTO_BAD", "0") == "1"
    return True


def segment_overlaps_owner_label(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    label: str,
    pad_sec: float = 60.0,
) -> bool:
    end_sec = start_sec + duration_sec
    for row in _labels_for_vod(video_path, profile):
        if row.get("label") != label:
            continue
        if label == "bad" and not _owner_bad_blocks_scan(row, profile):
            continue
        center = float(row["time_sec"])
        if start_sec - pad_sec <= center <= end_sec + pad_sec:
            return True
    return False


def _filter_bad_label_starts(
    video_path: Path,
    profile: str,
    starts: list[float],
    *,
    pad_sec: float | None = None,
) -> list[float]:
    pad = pad_sec if pad_sec is not None else _owner_label_pad("bad")
    kept: list[float] = []
    for start in starts:
        if segment_overlaps_owner_label(
            video_path, start, WINDOW_SEC, profile, label="bad", pad_sec=pad
        ):
            log.info("soft anchor exclude bad label near start=%.1f pad=%.0f", start, pad)
            continue
        kept.append(start)
    return kept


def _mlbb_skip_intro_sec() -> float:
    return float(os.environ.get("HIGHLIGHT_MLBB_SKIP_INTRO_SEC", "300"))


def _action_peak_starts(analysis: dict, profile: str, *, limit: int = 48) -> list[float]:
    """Top motion/audio peaks spread across full VOD — avoids intro-only traps on long streams."""
    win = float(analysis.get("window_seconds", 2.0))
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    if profile in SHOOTER_PROFILES:
        combined = gun * 0.62 + motion * 0.22 + audio * 0.16
        skip_intro = 90.0
    elif profile == "genshin":
        scene = np.asarray(analysis["scene"], dtype=np.float32)
        combined = motion * 0.35 + audio * 0.30 + scene * 0.35
        skip_intro = 120.0
    elif profile == "mobile_legends":
        combined = motion * 0.45 + audio * 0.35 + gun * 0.20
        skip_intro = _mlbb_skip_intro_sec()
    else:
        combined = motion * 0.40 + audio * 0.35 + gun * 0.25
        skip_intro = 120.0

    min_gap = float(os.environ.get("HIGHLIGHT_PEAK_MIN_GAP_SEC", "75"))
    order = np.argsort(combined)[::-1]
    starts: list[float] = []
    for idx in order:
        start = float(idx) * win
        if start < skip_intro:
            continue
        if any(abs(start - s) < min_gap for s in starts):
            continue
        starts.append(round(start, 1))
        if len(starts) >= limit:
            break
    return starts


def _rank_stage1_starts(
    analysis: dict,
    profile: str,
    starts: list[float],
    *,
    video_path: Path | None = None,
) -> list[float]:
    """Score windows by local action — probe high-motion regions before chronological intro."""
    if not starts:
        return []
    if profile == "mobile_legends" and os.environ.get("MLBB_TEAMFIGHT_RANK", "1") == "1":
        try:
            from mlbb_teamfight_detector import rank_starts_by_teamfight

            return rank_starts_by_teamfight(
                analysis,
                starts,
                video_path=video_path,
            )
        except Exception as exc:
            log.warning("teamfight rank failed: %s", exc)
    win = float(analysis.get("window_seconds", 2.0))
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    if profile in SHOOTER_PROFILES:
        weights = (0.62, 0.22, 0.16, 0.0)
    elif profile == "mobile_legends":
        weights = (0.20, 0.45, 0.35, 0.0)
    elif profile == "genshin":
        scene = np.asarray(analysis.get("scene", motion), dtype=np.float32)
        scored: list[tuple[float, float]] = []
        for start in starts:
            i0 = max(0, int(start / win))
            i1 = min(len(motion), int((start + WINDOW_SEC) / win))
            if i1 <= i0:
                continue
            val = (
                float(np.max(motion[i0:i1])) * 0.35
                + float(np.max(audio[i0:i1])) * 0.30
                + float(np.max(scene[i0:i1])) * 0.35
            )
            scored.append((val, start))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [s for _, s in scored]
    else:
        weights = (0.25, 0.40, 0.35, 0.0)

    scored = []
    for start in starts:
        i0 = max(0, int(start / win))
        i1 = min(len(motion), int((start + WINDOW_SEC) / win))
        if i1 <= i0:
            continue
        val = (
            float(np.max(gun[i0:i1])) * weights[0]
            + float(np.max(motion[i0:i1])) * weights[1]
            + float(np.max(audio[i0:i1])) * weights[2]
        )
        scored.append((val, start))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [s for _, s in scored]


def classifier_available(profile: str | None = None) -> bool:
    prof = normalize_profile(profile or os.environ.get("_HIGHLIGHT_PROFILE", "pubg"))
    return classifier_path_for_profile(prof).exists()


def normalize_profile(profile: str) -> str:
    p = profile.strip().lower()
    if p == "mlbb":
        return "mobile_legends"
    if p == "world_of_tanks":
        return "wot"
    return p


@dataclass
class HighlightMetrics:
    start: float
    duration: float
    profile: str
    panns_gunshot: float = 0.0
    panns_machine_gun: float = 0.0
    panns_explosion: float = 0.0
    panns_artillery: float = 0.0
    panns_speech: float = 0.0
    panns_music: float = 0.0
    panns_gun_max: float = 0.0
    panns_gun_threshold: float = PANN_GUN_MIN
    clip_score: float = 0.0
    ocr_text: str = ""
    ocr_hits: int = 0
    center_motion: float = 0.0
    boss_bar: float = 0.0
    minimap_delta: float = 0.0
    skill_delta: float = 0.0
    classifier_prob: float = 0.0
    intelliclip_score: float = 0.0
    hook_score: float = 0.0
    visual_dynamics: float = 0.0
    heatmap_intensity: float = 0.0
    viral_score: float = 0.0
    combined_score: float = 0.0
    pass_reason: str = ""
    visual_pass: bool = False
    audio_pass: bool = False
    rule_pass: bool = False
    frames: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "profile": self.profile,
            "panns_gunshot": round(self.panns_gunshot, 4),
            "panns_machine_gun": round(self.panns_machine_gun, 4),
            "panns_explosion": round(self.panns_explosion, 4),
            "panns_artillery": round(self.panns_artillery, 4),
            "panns_speech": round(self.panns_speech, 4),
            "panns_music": round(self.panns_music, 4),
            "panns_gun_max": round(self.panns_gun_max, 4),
            "panns_gun_threshold": round(self.panns_gun_threshold, 4),
            "clip_score": round(self.clip_score, 4),
            "ocr_text": self.ocr_text,
            "ocr_hits": self.ocr_hits,
            "center_motion": round(self.center_motion, 4),
            "boss_bar": round(self.boss_bar, 4),
            "minimap_delta": round(self.minimap_delta, 4),
            "skill_delta": round(self.skill_delta, 4),
            "classifier_prob": round(self.classifier_prob, 4),
            "intelliclip_score": round(self.intelliclip_score, 4),
            "hook_score": round(self.hook_score, 4),
            "visual_dynamics": round(self.visual_dynamics, 4),
            "heatmap_intensity": round(self.heatmap_intensity, 4),
            "viral_score": round(self.viral_score, 4),
            "combined_score": round(self.combined_score, 4),
            "pass_reason": self.pass_reason,
            "visual_pass": self.visual_pass,
            "audio_pass": self.audio_pass,
            "rule_pass": self.rule_pass,
            "frames": self.frames,
        }


def _extract_audio_32k(video_path: Path, start_sec: float, duration_sec: float) -> np.ndarray:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "32000",
        "-f",
        "f32le",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=60)
    except subprocess.TimeoutExpired:
        return np.array([], dtype=np.float32)
    if result.returncode != 0 or not result.stdout:
        return np.array([], dtype=np.float32)
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if audio.size < 3200:
        return np.array([], dtype=np.float32)
    return audio


@lru_cache(maxsize=1)
def _panns_tagger():
    from panns_inference import AudioTagging

    device = "cuda" if os.environ.get("HIGHLIGHT_PANN_DEVICE", "cpu") == "cuda" else "cpu"
    try:
        import torch

        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
    except ImportError:
        device = "cpu"
    return AudioTagging(device=device)


def score_panns_audio(video_path: Path, start_sec: float, duration_sec: float) -> dict[str, float]:
    audio = _extract_audio_32k(video_path, start_sec, duration_sec)
    out = {
        "panns_gunshot": 0.0,
        "panns_machine_gun": 0.0,
        "panns_explosion": 0.0,
        "panns_artillery": 0.0,
        "panns_speech": 0.0,
        "panns_music": 0.0,
        "panns_gun_max": 0.0,
    }
    if audio.size == 0:
        return out
    tagger = _panns_tagger()
    clipwise, _emb = tagger.inference(audio[None, :])
    scores = clipwise[0]
    out["panns_gunshot"] = float(scores[PANN_GUN_IDX["gunshot"]])
    out["panns_machine_gun"] = float(scores[PANN_GUN_IDX["machine_gun"]])
    out["panns_explosion"] = float(scores[PANN_GUN_IDX["explosion"]])
    out["panns_artillery"] = float(scores[PANN_GUN_IDX["artillery"]])
    out["panns_speech"] = float(scores[PANN_GUN_IDX["speech"]])
    out["panns_music"] = float(scores[PANN_GUN_IDX["music"]])
    out["panns_gun_max"] = max(
        out["panns_gunshot"],
        out["panns_machine_gun"],
        out["panns_explosion"],
        out["panns_artillery"],
    )
    return out


@lru_cache(maxsize=1)
def _clip_bundle():
    import open_clip
    import torch

    model_name = os.environ.get("HIGHLIGHT_CLIP_MODEL", "ViT-B-32")
    pretrained = os.environ.get("HIGHLIGHT_CLIP_PRETRAINED", "laion2b_s34b_b79k")
    device = "cuda" if torch.cuda.is_available() and os.environ.get("HIGHLIGHT_CLIP_DEVICE") == "cuda" else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
    model = model.to(device).eval()
    tokenizer = open_clip.get_tokenizer(model_name)
    return model, preprocess, tokenizer, device


def _frame_to_pil(frame: np.ndarray):
    from PIL import Image
    import cv2

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


@lru_cache(maxsize=32)
def _exemplar_embeddings(game: str, label: str) -> tuple[np.ndarray, ...]:
    import open_clip
    import torch

    game = normalize_profile(game)
    folder = EXEMPLAR_ROOT / game / label
    if not folder.exists():
        return tuple()
    model, preprocess, _, device = _clip_bundle()
    paths = sorted(folder.glob("*.mp4")) + sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.png"))
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    max_n = int(os.environ.get("HIGHLIGHT_EXEMPLAR_MAX", "0"))
    if max_n > 0:
        paths = paths[:max_n]
    embs: list[np.ndarray] = []
    from gameplay_gate import _read_frame_at

    from smart_video_editor import ffprobe_duration

    for path in paths:
        frames_to_encode: list = []
        if path.suffix.lower() in (".jpg", ".png"):
            import cv2

            frame = cv2.imread(str(path))
            if frame is not None:
                frames_to_encode.append(frame)
        else:
            dur = float(ffprobe_duration(path) or 10.0)
            for frac in (0.25, 0.5, 0.75):
                frame = _read_frame_at(path, max(0.1, dur * frac))
                if frame is not None:
                    frames_to_encode.append(frame)
        for frame in frames_to_encode:
            tensor = preprocess(_frame_to_pil(frame)).unsqueeze(0).to(device)
            with torch.no_grad():
                emb = model.encode_image(tensor)
                emb = emb / emb.norm(dim=-1, keepdim=True)
            embs.append(emb.cpu().numpy()[0])
    return tuple(embs)


def clear_exemplar_cache() -> None:
    _exemplar_embeddings.cache_clear()
    _clip_bundle.cache_clear()


def _hist_vector(frame: np.ndarray) -> np.ndarray:
    import cv2

    small = cv2.resize(frame, (64, 36))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten()


@lru_cache(maxsize=1)
def _load_highlight_queries() -> dict:
    if not QUERY_CONFIG.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(QUERY_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("highlight_queries load failed: %s", exc)
        return {}


def score_text_query_clip(
    video_path: Path, start_sec: float, duration_sec: float, profile: str
) -> float:
    """HL-CLIP style text-query scoring from config/highlight_queries.yaml."""
    profile = normalize_profile(profile)
    queries = _load_highlight_queries().get(profile, {})
    good_q = queries.get("good_queries") or []
    bad_q = queries.get("bad_queries") or []
    if not good_q and not bad_q:
        return 0.0

    if os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") == "1":
        return 0.0
    try:
        import open_clip
        import torch

        model, preprocess, tokenizer, device = _clip_bundle()
    except Exception as exc:
        log.warning("text CLIP unavailable: %s", exc)
        return 0.0

    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    frame = _read_frame_at(video_path, start_sec + 0.2)
    if frame is None:
        return 0.0
    if crop is not None:
        x, y, w, h = crop
        frame = frame[y : y + h, x : x + w]

    tensor = preprocess(_frame_to_pil(frame)).unsqueeze(0).to(device)
    with torch.no_grad():
        img_emb = model.encode_image(tensor)
        img_emb = (img_emb / img_emb.norm(dim=-1, keepdim=True)).cpu().numpy()[0]

    def text_emb(texts: list[str]) -> np.ndarray:
        tokens = tokenizer(texts).to(device)
        with torch.no_grad():
            t = model.encode_text(tokens)
            t = (t / t.norm(dim=-1, keepdim=True)).cpu().numpy()
        return t.mean(axis=0)

    good_mat = text_emb(good_q) if good_q else np.zeros_like(img_emb)
    bad_mat = text_emb(bad_q) if bad_q else np.zeros_like(img_emb)
    return float(np.dot(img_emb, good_mat) - np.dot(img_emb, bad_mat))


def _score_hist_exemplar_fallback(
    video_path: Path, start_sec: float, duration_sec: float, profile: str
) -> tuple[float, list[dict]]:
    import cv2
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    game = normalize_profile(profile)
    good_paths = list((EXEMPLAR_ROOT / game / "good").glob("*.jpg")) + list(
        (EXEMPLAR_ROOT / game / "good").glob("*.mp4")
    )
    bad_paths = list((EXEMPLAR_ROOT / game / "bad").glob("*.jpg")) + list(
        (EXEMPLAR_ROOT / game / "bad").glob("*.mp4")
    )
    good_vecs: list[np.ndarray] = []
    bad_vecs: list[np.ndarray] = []
    for path in good_paths[:12]:
        frame = cv2.imread(str(path)) if path.suffix.lower() in (".jpg", ".png") else _read_frame_at(path, 1.0)
        if frame is not None:
            good_vecs.append(_hist_vector(frame))
    for path in bad_paths[:12]:
        frame = cv2.imread(str(path)) if path.suffix.lower() in (".jpg", ".png") else _read_frame_at(path, 1.0)
        if frame is not None:
            bad_vecs.append(_hist_vector(frame))

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    times = [
        ("start", start_sec + 0.15),
        ("mid", start_sec + duration_sec * 0.5),
        ("end", start_sec + max(0.2, duration_sec - 0.25)),
    ]
    scores: list[float] = []
    rows: list[dict] = []
    for label, t in times:
        frame = _read_frame_at(video_path, t)
        if frame is None:
            rows.append({"label": label, "timestamp": round(t, 3), "clip_score": 0.0, "pass": False})
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        vec = _hist_vector(frame)
        sim_g = float(np.mean([cv2.compareHist(vec.reshape(16, 16), g.reshape(16, 16), cv2.HISTCMP_CORREL) for g in good_vecs])) if good_vecs else 0.0
        sim_b = float(np.mean([cv2.compareHist(vec.reshape(16, 16), b.reshape(16, 16), cv2.HISTCMP_CORREL) for b in bad_vecs])) if bad_vecs else 0.0
        clip_s = sim_g - sim_b
        scores.append(clip_s)
        rows.append(
            {
                "label": label,
                "timestamp": round(t, 3),
                "clip_score": round(clip_s, 4),
                "pass": clip_s > CLIP_MIN_SHOOTER,
                "fallback": "hist",
            }
        )
    log.warning("CLIP disabled/unavailable — histogram exemplar emergency fallback for %s", video_path.name)
    return (float(np.mean(scores)) if scores else 0.0, rows)


def _text_bootstrap_embeddings(game: str) -> tuple[np.ndarray, np.ndarray]:
    """Fallback when exemplar clips are sparse (Mixpeek-style text anchors)."""
    import open_clip
    import torch

    model, _, tokenizer, device = _clip_bundle()
    if game in SHOOTER_PROFILES or game == "pubg" or game == "standoff":
        good = ["fps gunfight firefight shooting enemies combat", "battle royale shootout fire exchange"]
        bad = ["running looting inventory menu walking empty field", "streamer talking no combat"]
    elif game == "mobile_legends":
        good = ["moba teamfight abilities skills minimap combat"]
        bad = ["lane farming walking draft hero select"]
    elif game == "genshin":
        good = ["boss fight hp bar elemental combat"]
        bad = ["exploration walking open world idle"]
    else:
        good = ["tank battle explosion hit"]
        bad = ["driving empty field cruise"]

    def enc(texts: list[str]) -> np.ndarray:
        tokens = tokenizer(texts).to(device)
        with torch.no_grad():
            t = model.encode_text(tokens)
            t = t / t.norm(dim=-1, keepdim=True)
        return t.cpu().numpy().mean(axis=0)

    return enc(good), enc(bad)


def score_clip_exemplar(video_path: Path, start_sec: float, duration_sec: float, profile: str) -> tuple[float, list[dict]]:
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    profile = normalize_profile(profile)
    game = profile
    text_score = score_text_query_clip(video_path, start_sec, duration_sec, profile)
    if os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") == "1":
        if os.environ.get("HIGHLIGHT_TRAIN_MODE", "0") == "1":
            hist_score, rows = _score_hist_exemplar_fallback(
                video_path, start_sec, duration_sec, profile
            )
            combined = text_score if text_score else hist_score
            return combined, rows
        log.error("HIGHLIGHT_CLIP_DISABLED=1 blocked for inference on %s", video_path.name)
        return -1.0, [{"pass": False, "reason": "clip_disabled"}]
    try:
        import open_clip
        import torch

        model, preprocess, _, device = _clip_bundle()
    except Exception as exc:
        if os.environ.get("HIGHLIGHT_TRAIN_MODE", "0") == "1":
            hist_score, rows = _score_hist_exemplar_fallback(
                video_path, start_sec, duration_sec, profile
            )
            combined = (hist_score + text_score) / 2 if text_score else hist_score
            return combined, rows
        log.error("CLIP unavailable (%s) — REFUSE inference", exc)
        return -1.0, [{"pass": False, "reason": f"clip_unavailable:{exc}"}]
    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    times = [
        ("start", start_sec + 0.15),
        ("mid", start_sec + duration_sec * 0.5),
        ("end", start_sec + max(0.2, duration_sec - 0.25)),
    ]

    good_embs = list(_exemplar_embeddings(game, "good"))
    bad_embs = list(_exemplar_embeddings(game, "bad"))
    if len(good_embs) < 3 or len(bad_embs) < 3:
        g, b = _text_bootstrap_embeddings(game)
        good_embs.append(g)
        bad_embs.append(b)

    good_mat = np.stack(good_embs)
    bad_mat = np.stack(bad_embs)
    frame_rows: list[dict] = []
    scores: list[float] = []

    for label, t in times:
        frame = _read_frame_at(video_path, t)
        if frame is None:
            frame_rows.append({"label": label, "timestamp": round(t, 3), "clip_score": 0.0, "pass": False})
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        tensor = preprocess(_frame_to_pil(frame)).unsqueeze(0).to(device)
        with torch.no_grad():
            emb = model.encode_image(tensor)
            emb = (emb / emb.norm(dim=-1, keepdim=True)).cpu().numpy()[0]
        sim_good = float(np.mean(good_mat @ emb))
        sim_bad = float(np.mean(bad_mat @ emb))
        bad_lambda = float(os.environ.get("HIGHLIGHT_BAD_EXEMPLAR_LAMBDA", "0.5"))
        clip_s = sim_good - bad_lambda * sim_bad
        scores.append(clip_s)
        frame_rows.append(
            {
                "label": label,
                "timestamp": round(t, 3),
                "clip_score": round(clip_s, 4),
                "sim_good": round(sim_good, 4),
                "sim_bad": round(sim_bad, 4),
                "pass": clip_s > CLIP_MIN_SHOOTER,
            }
        )

    exemplar_score = float(np.mean(scores)) if scores else 0.0
    if good_embs and bad_embs:
        clip_final = (exemplar_score + text_score) / 2.0 if text_score else exemplar_score
    else:
        clip_final = text_score if text_score else exemplar_score
    return clip_final, frame_rows


def score_killfeed_ocr(video_path: Path, start_sec: float, duration_sec: float) -> tuple[str, int]:
    try:
        from pubg_combat_gate import _pubg_killfeed_hits
    except ImportError:
        pass
    else:
        return _pubg_killfeed_hits(video_path, start_sec, duration_sec)

    try:
        import cv2
        import pytesseract
        from gameplay_gate import _read_frame_at, detect_game_viewport_crop
    except ImportError:
        return "", 0

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    merged = ""
    best_hits = 0
    for frac in (0.2, 0.5, 0.8):
        frame = _read_frame_at(video_path, start_sec + duration_sec * frac)
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        small = cv2.resize(frame, (320, 180))
        h, w = small.shape[:2]
        zone = small[int(h * 0.02) : int(h * 0.22), int(w * 0.62) : int(w * 0.98)]
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        text = pytesseract.image_to_string(
            gray,
            config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+- ",
        )
        text = " ".join(text.split())
        merged = f"{merged} {text}".strip()
        hits = sum(
            1
            for kw in ("kill", "knock", "eliminated", "headshot", "убил", "убийство")
            if kw.lower() in text.lower()
        )
        best_hits = max(best_hits, hits)
    if best_hits == 0 and merged:
        best_hits = sum(
            1
            for kw in ("kill", "knock", "eliminated", "headshot", "убил", "убийство")
            if kw.lower() in merged.lower()
        )
    return merged[:120], best_hits


def _load_classifier(profile: str | None = None):
    path = classifier_path_for_profile(normalize_profile(profile or os.environ.get("_HIGHLIGHT_PROFILE", "pubg")))
    if not path.exists():
        return None
    try:
        import joblib

        return joblib.load(path)
    except Exception as exc:
        log.warning("classifier load failed %s: %s", path, exc)
        return None


def classifier_probability(metrics: HighlightMetrics, profile: str | None = None) -> float:
    clf = _load_classifier(profile or metrics.profile)
    if clf is None:
        return 0.0
    prof = normalize_profile(profile or metrics.profile)
    if prof == "mobile_legends":
        from mlbb_owner_learning import mlbb_classifier_features

        feats = np.array([mlbb_classifier_features(metrics)])
    else:
        feats = np.array(
            [
                [
                    metrics.panns_gunshot,
                    metrics.panns_machine_gun,
                    metrics.panns_explosion,
                    metrics.clip_score,
                    metrics.center_motion,
                    metrics.boss_bar,
                ]
            ]
        )
    try:
        if hasattr(clf, "predict_proba"):
            return float(clf.predict_proba(feats)[0][1])
        return float(clf.decision_function(feats)[0])
    except Exception:
        return 0.0


def _motion_context(video_path: Path, start_sec: float, duration_sec: float) -> dict[str, float]:
    from gameplay_gate import detect_game_viewport_crop, score_genshin_boss_likelihood, score_segment_combat

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    motion, mini, skill, _text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop, sample_frames=5
    )
    boss_bar = 0.0
    if normalize_profile(os.environ.get("_HIGHLIGHT_PROFILE", "")) == "genshin":
        bar, _, _, _ = score_genshin_boss_likelihood(video_path, start_sec, duration_sec, crop_box=crop)
        boss_bar = bar
    return {
        "center_motion": motion,
        "minimap_delta": mini,
        "skill_delta": skill,
        "boss_bar": boss_bar,
    }


def calibrated_pann_gun_min(video_path: Path, profile: str) -> float:
    """Owner-label separation — stream RU Metro has low absolute PANNs gun scores."""
    if os.environ.get("HIGHLIGHT_PANN_FIXED", "0") == "1":
        return PANN_GUN_MIN
    starts = _owner_anchor_starts(video_path, profile)
    labels_path = _owner_labels_path(normalize_profile(profile))
    if not starts or labels_path is None:
        return PANN_GUN_MIN
    try:
        data = json.loads(labels_path.read_text(encoding="utf-8"))
        vid = video_path.stem[3:] if video_path.stem.startswith("yt_") else video_path.stem
        rows = data.get("videos", {}).get(vid, [])
    except (json.JSONDecodeError, OSError):
        return PANN_GUN_MIN

    good_scores: list[float] = []
    bad_scores: list[float] = []
    for row in rows:
        if "time_sec" not in row:
            continue
        t = float(row["time_sec"]) - WINDOW_SEC * 0.5
        p = score_panns_audio(video_path, max(0, t), WINDOW_SEC)
        s = p["panns_gun_max"]
        if row.get("label") == "good":
            good_scores.append(s)
        elif row.get("label") == "bad":
            bad_scores.append(s)

    if not good_scores:
        return PANN_GUN_MIN
    good_p90 = float(np.percentile(good_scores, 90))
    bad_p50 = float(np.percentile(bad_scores, 50)) if bad_scores else 0.0
    # Separate good from bad on this VOD; never drop below inference floor (RU streams).
    dynamic = max(good_p90 * 0.85, bad_p50 * 1.35, PANN_GUN_INFERENCE_FLOOR)
    return max(PANN_GUN_INFERENCE_FLOOR, min(dynamic, PANN_GUN_MIN))


def audio_passes_shooter(
    panns: dict[str, float],
    *,
    gun_min: float | None = None,
) -> tuple[bool, str]:
    gun_max = panns["panns_gun_max"]
    threshold = PANN_GUN_MIN if gun_min is None else gun_min
    speech = max(panns["panns_speech"], 0.01)
    gun_ratio = gun_max / speech
    if gun_max < threshold:
        return False, f"panns_gun_low={gun_max:.3f}:thr{threshold:.3f}"
    if panns["panns_music"] > 0.55 and gun_max < threshold * 1.15:
        return False, f"music_dominant={panns['panns_music']:.3f}"
    if gun_ratio < PANN_GUN_SPEECH_RATIO_MIN:
        return False, f"speech_dominant=ratio{gun_ratio:.3f}"
    return True, f"panns_gun_ok={gun_max:.3f}"


def exemplars_sufficient(profile: str, min_good: int = 5) -> tuple[bool, str]:
    if os.environ.get("HIGHLIGHT_COLD_START", "0") == "1":
        return True, ""
    game = normalize_profile(profile)
    root = EXEMPLAR_ROOT / game / "good"
    good = [
        p
        for p in root.glob("*")
        if p.suffix.lower() in (".jpg", ".png", ".jpeg", ".mp4", ".webp")
    ]
    if len(good) < min_good:
        return (
            False,
            f"upload exemplars to data/highlight_exemplars/{game}/good/ "
            f"(have {len(good)}, need {min_good})",
        )
    return True, ""


def require_inference_ready(profile: str) -> tuple[bool, str]:
    """Exemplars + CLIP required for inference (not train/bootstrap)."""
    if os.environ.get("HIGHLIGHT_TRAIN_MODE", "0") == "1":
        return True, ""
    ok, msg = exemplars_sufficient(profile)
    if not ok:
        return False, msg
    if os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") == "1":
        return True, ""
    try:
        _clip_bundle()
        return True, ""
    except Exception as exc:
        return False, f"CLIP weights unavailable: {exc}"


def rule_gate(
    profile: str,
    metrics: HighlightMetrics,
    *,
    video_path: Path | None = None,
    start_sec: float = 0,
    duration_sec: float = WINDOW_SEC,
) -> tuple[bool, str]:
    profile = normalize_profile(profile)
    if not metrics.visual_pass:
        return False, "visual_fail"
    if profile in SHOOTER_PROFILES:
        if not metrics.audio_pass:
            return False, metrics.pass_reason or "audio_fail"
        if video_path is None:
            return False, "combat_gate_no_video"
        from pubg_combat_gate import pubg_passes_combat_gate

        ok, reason, _ = pubg_passes_combat_gate(
            video_path, start_sec, duration_sec, profile, metrics=metrics
        )
        return ok, reason

    if profile == "genshin":
        if metrics.boss_bar < 0.35 and metrics.center_motion < 0.18:
            return False, f"no_boss=motion{metrics.center_motion:.3f}:bar{metrics.boss_bar:.3f}"
        if metrics.clip_score <= CLIP_MIN_SHOOTER:
            return False, f"clip_low={metrics.clip_score:.3f}"
        if metrics.center_motion < 0.18:
            return False, f"motion_low={metrics.center_motion:.3f}"
        return True, "genshin_boss_ok"

    if profile == "mobile_legends":
        if metrics.minimap_delta < 0.012 or metrics.skill_delta < 0.007:
            return False, f"hud_low=mini{metrics.minimap_delta:.3f}:skill{metrics.skill_delta:.3f}"
        if metrics.clip_score <= 0.03:
            return False, f"clip_low={metrics.clip_score:.3f}"
        return True, "mlbb_fight_ok"

    if profile == "wot":
        if metrics.panns_explosion < 0.20 and metrics.panns_gun_max < 0.20:
            return False, f"panns_explosion_low={metrics.panns_explosion:.3f}"
        if metrics.clip_score <= 0.03:
            return False, f"clip_low={metrics.clip_score:.3f}"
        return True, "wot_impact_ok"

    return False, "unknown_profile"


def score_candidate_window(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    skip_owner_bad: bool = False,
) -> HighlightMetrics:
    profile = normalize_profile(profile)
    os.environ["_HIGHLIGHT_PROFILE"] = profile

    panns = score_panns_audio(video_path, start_sec, duration_sec)
    clip_score, frames = score_clip_exemplar(video_path, start_sec, duration_sec, profile)
    if clip_score < 0:
        m = HighlightMetrics(
            start=start_sec,
            duration=duration_sec,
            profile=profile,
            clip_score=0.0,
            pass_reason=frames[0].get("reason", "clip_unavailable") if frames else "clip_unavailable",
            rule_pass=False,
        )
        return m
    ocr_text, ocr_hits = score_killfeed_ocr(video_path, start_sec, duration_sec)
    motion = _motion_context(video_path, start_sec, duration_sec)

    m = HighlightMetrics(
        start=start_sec,
        duration=duration_sec,
        profile=profile,
        clip_score=clip_score,
        ocr_text=ocr_text,
        ocr_hits=ocr_hits,
        center_motion=motion["center_motion"],
        boss_bar=motion["boss_bar"],
        minimap_delta=motion["minimap_delta"],
        skill_delta=motion["skill_delta"],
        frames=frames,
        **{k: v for k, v in panns.items()},
    )

    gun_min = calibrated_pann_gun_min(video_path, profile) if profile in SHOOTER_PROFILES else PANN_GUN_MIN
    m.panns_gun_threshold = round(gun_min, 4)

    if profile in SHOOTER_PROFILES:
        m.audio_pass, audio_reason = audio_passes_shooter(panns, gun_min=gun_min)
        if not m.audio_pass:
            m.pass_reason = audio_reason
    else:
        m.audio_pass = True

    from visual_action_check import extract_and_check_segment

    vis_row = extract_and_check_segment(video_path, start_sec, duration_sec, profile)
    m.visual_pass = bool(vis_row.get("visual_pass"))
    if not m.visual_pass:
        m.pass_reason = vis_row.get("fail_reason") or "visual_multi_fail"

    if os.environ.get("HIGHLIGHT_HEATMAP", "1") == "1":
        try:
            from youtube_heatmap_peaks import load_heatmap_intensity_map, nearest_heatmap_intensity

            hm_map = load_heatmap_intensity_map(video_path)
            m.heatmap_intensity = nearest_heatmap_intensity(start_sec + duration_sec * 0.5, hm_map)
        except Exception:
            m.heatmap_intensity = 0.0

    if (
        not skip_owner_bad
        and segment_overlaps_owner_label(
        video_path, start_sec, duration_sec, profile, label="bad", pad_sec=_owner_label_pad("bad")
    )
    ):
        m.rule_pass = False
        m.pass_reason = "owner_bad_window"
        m.combined_score = 0.0
        return m

    m.rule_pass, rule_reason = rule_gate(
        profile, m, video_path=video_path, start_sec=start_sec, duration_sec=duration_sec
    )
    m.pass_reason = rule_reason if m.rule_pass else (m.pass_reason or rule_reason)

    if classifier_available(profile):
        m.classifier_prob = classifier_probability(m, profile)
    else:
        m.classifier_prob = 0.5

    combat_authoritative = profile in SHOOTER_PROFILES and m.rule_pass
    clf_ok = m.classifier_prob >= CLASSIFIER_MIN
    if not classifier_available(profile) and m.rule_pass and m.visual_pass:
        clf_ok = True
    if (
        profile == "mobile_legends"
        and m.rule_pass
        and m.visual_pass
        and (not classifier_available(profile) or os.environ.get("MLBB_USE_CLASSIFIER", "0") != "1")
    ):
        # No trained classifier on disk — do not block MLBB windows.
        clf_ok = True
    if m.rule_pass and (combat_authoritative or clf_ok):
        m.combined_score = (
            m.panns_gun_max * 0.45
            + max(m.clip_score, 0) * 0.35
            + m.classifier_prob * 0.15
            + m.center_motion * 0.05
            + min(m.ocr_hits, 3) * 0.02
        )
        if segment_overlaps_owner_label(
            video_path,
            start_sec,
            duration_sec,
            profile,
            label="good",
            pad_sec=_owner_label_pad("good"),
        ):
            boost = float(os.environ.get("HIGHLIGHT_GOOD_SOFT_BOOST", "0.25"))
            m.combined_score *= 1.0 + boost
    else:
        m.combined_score = 0.0
        gate_reason = m.pass_reason or rule_reason
        m.rule_pass = False
        if profile in SHOOTER_PROFILES:
            m.pass_reason = gate_reason or "combat_gate_fail"
        elif not clf_ok:
            m.pass_reason = f"classifier_low={m.classifier_prob:.3f}"
        elif not m.pass_reason:
            m.pass_reason = gate_reason or "rule_fail"

    if os.environ.get("INTELLICLIP", "1") == "1":
        try:
            from intelliclip_scorer import enrich_highlight_metrics

            enrich_highlight_metrics(m, video_path, profile)
        except Exception as exc:
            log.warning("intelliclip enrich failed: %s", exc)

    try:
        from viral_scorer import hook_score, segment_viral_score

        hook, _ = hook_score(video_path, start_sec, profile, duration_sec=duration_sec)
        if m.hook_score <= 0:
            m.hook_score = hook
        m.viral_score = segment_viral_score(m, video_path)
    except Exception as exc:
        log.warning("viral score failed: %s", exc)

    return m


def _owner_vicinity_gun_starts(video_path: Path, profile: str) -> list[float]:
    """Scan near owner good labels for real gun peaks — not raw timestamp injection."""
    profile = normalize_profile(profile)
    if profile not in OWNER_LABEL_PROFILES or _owner_labels_path(profile) is None:
        return []
    anchors = _owner_anchor_starts(video_path, profile)
    if not anchors:
        return []
    radius = float(os.environ.get("HIGHLIGHT_OWNER_VICINITY_SEC", "120"))
    step = float(os.environ.get("HIGHLIGHT_OWNER_VICINITY_STEP", "4"))
    gun_floor = float(os.environ.get("HIGHLIGHT_OWNER_VICINITY_GUN_MIN", "0.18"))
    peaks: list[tuple[float, float]] = []
    for anchor in anchors:
        lo = max(60.0, anchor - radius)
        hi = anchor + radius
        t = lo
        while t <= hi:
            win_start = max(0.0, t - WINDOW_SEC * 0.5)
            panns = score_panns_audio(video_path, win_start, WINDOW_SEC)
            gun = panns["panns_gun_max"]
            if gun >= gun_floor:
                peaks.append((gun, round(win_start, 1)))
            t += step
    peaks.sort(key=lambda row: row[0], reverse=True)
    starts: list[float] = []
    min_gap = 45.0
    for gun, start in peaks:
        if any(abs(start - s) < min_gap for s in starts):
            continue
        starts.append(start)
        if len(starts) >= int(os.environ.get("HIGHLIGHT_OWNER_VICINITY_MAX", "12")):
            break
    if starts:
        log.info("owner vicinity gun peaks %s: %s windows (top gun=%.3f)", video_path.name, len(starts), peaks[0][0])
    return starts


def _owner_anchor_stage1_starts(video_path: Path, profile: str) -> list[float]:
    """Probe windows near owner good labels — still passes full gates later."""
    anchors = _owner_anchor_starts(video_path, profile)
    if not anchors:
        return []
    out: list[float] = []
    for anchor in anchors:
        for off in (-90, -60, -30, 0, 30, 60, 90):
            s = anchor + off - WINDOW_SEC * 0.5
            if s >= 60:
                out.append(round(s, 1))
    if out:
        log.info("owner anchor vicinity %s: %s probe windows near %s labels", video_path.name, len(out), len(anchors))
    return out


def _owner_anchor_starts(video_path: Path, profile: str) -> list[float]:
    profile = normalize_profile(profile)
    rows = _labels_for_vod(video_path, profile)
    return [float(r["time_sec"]) for r in rows if r.get("label") == "good" and "time_sec" in r]


def _heatmap_stage0_starts(video_path: Path) -> list[float]:
    if os.environ.get("HIGHLIGHT_HEATMAP", "1") != "1":
        return []
    try:
        from youtube_heatmap_peaks import heatmap_peak_starts

        peaks = heatmap_peak_starts(video_path, window_sec=WINDOW_SEC)
        if peaks:
            log.info("heatmap stage0 %s: %s peaks", video_path.name, len(peaks))
        return peaks
    except Exception as exc:
        log.warning("heatmap stage0 failed: %s", exc)
        return []


def stage1_candidates(video_path: Path, profile: str) -> list[float]:
    """Stage0 heatmap + Stage1 peaks; soft owner boost when VOD has labels (not hard inject)."""
    profile = normalize_profile(profile)
    max_stage1 = int(os.environ.get("HIGHLIGHT_MAX_STAGE1", "60"))
    starts: set[float] = set(_heatmap_stage0_starts(video_path))

    if soft_anchor_enabled(video_path, profile):
        good_anchors = _owner_anchor_starts(video_path, profile)
        if good_anchors:
            try:
                from intelliclip_scorer import merge_starts_with_anchors, rank_hybrid_starts

                prev_boost = os.environ.get("INTELLICLIP_ANCHOR_BOOST")
                os.environ["INTELLICLIP_ANCHOR_BOOST"] = os.environ.get(
                    "HIGHLIGHT_SOFT_ANCHOR_BOOST", "0.5"
                )
                ranked = rank_hybrid_starts(
                    video_path,
                    profile,
                    good_anchors,
                    window_sec=WINDOW_SEC,
                    limit=max_stage1,
                )
                merged = merge_starts_with_anchors(ranked, good_anchors, limit=max_stage1)
                if prev_boost is None:
                    os.environ.pop("INTELLICLIP_ANCHOR_BOOST", None)
                else:
                    os.environ["INTELLICLIP_ANCHOR_BOOST"] = prev_boost
                for start in merged:
                    starts.add(start)
                log.info(
                    "soft anchor stage1 %s: %s windows (good_labels=%s boost=%s)",
                    video_path.name,
                    len(merged),
                    len(good_anchors),
                    os.environ.get("INTELLICLIP_ANCHOR_BOOST", "0.5"),
                )
            except Exception as exc:
                log.warning("soft anchor stage1 failed: %s", exc)

    if owner_anchors_enabled():
        if profile in SHOOTER_PROFILES and _owner_anchor_starts(video_path, profile):
            for vicinity_start in _owner_vicinity_gun_starts(video_path, profile):
                starts.add(vicinity_start)
        elif _owner_anchor_starts(video_path, profile):
            for vicinity_start in _owner_anchor_stage1_starts(video_path, profile):
                starts.add(vicinity_start)
        for vicinity_start in _owner_anchor_stage1_starts(video_path, profile):
            starts.add(vicinity_start)

    seed_raw = os.environ.get("HIGHLIGHT_SEED_STARTS", "")
    if seed_raw.strip() and os.environ.get("HIGHLIGHT_ALLOW_SEED_STARTS", "0") == "1":
        for part in seed_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                s = float(part) - WINDOW_SEC * 0.5
                if s >= 60:
                    starts.add(round(s, 1))
            except ValueError:
                pass
        out = sorted(starts)[:max_stage1]
        log.info("highlight seed-debug %s: %s windows", video_path.name, len(out))
        return out

    skip_intelliclip = profile in SHOOTER_PROFILES and os.environ.get(
        "SHOOTER_VOD_SKIP_INTELLICLIP", "1"
    ) == "1"
    if skip_intelliclip:
        log.info("intelliclip stage1 skipped %s (shooter fast path)", video_path.name)
    elif os.environ.get("INTELLICLIP_STAGE1", "1") == "1":
        try:
            from intelliclip_scorer import rank_window_starts

            ranked = rank_window_starts(
                video_path,
                profile,
                window_sec=WINDOW_SEC,
                step_sec=STEP_SEC,
                limit=max_stage1,
            )
            for start, _, _ in ranked:
                starts.add(start)
            log.info("intelliclip stage1 %s: %s windows", video_path.name, len(ranked))
        except Exception as exc:
            log.warning("intelliclip stage1 failed: %s", exc)

    from vod_analysis_cache import analyze_video_cached

    analysis = analyze_video_cached(video_path)
    if not owner_anchors_enabled() and profile in ("mobile_legends", "genshin", "wot"):
        peak_limit = int(os.environ.get("HIGHLIGHT_ACTION_PEAK_LIMIT", "40"))
        for peak_start in _action_peak_starts(analysis, profile, limit=peak_limit):
            starts.add(peak_start)
        log.info(
            "highlight action peaks %s: %s windows (anchors_off)",
            video_path.name,
            min(peak_limit, len(starts)),
        )

    win = float(analysis.get("window_seconds", 2.0))
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    duration = float(analysis.get("duration") or (len(motion) * win))

    p90 = float(np.percentile(motion, 90)) if motion.size else 0.02
    motion_thr = max(0.018, p90 * 0.55)

    skip_intro = 90.0
    if profile == "mobile_legends":
        skip_intro = _mlbb_skip_intro_sec()
    elif profile == "pubg":
        skip_intro = 90.0
    else:
        skip_intro = float(os.environ.get("SMART_SKIP_INTRO_SEC", "120"))

    t = skip_intro
    while t + WINDOW_SEC <= duration - 30:
        i0 = int(t / win)
        i1 = int((t + WINDOW_SEC) / win)
        chunk_m = motion[i0:i1] if i1 <= len(motion) else motion[i0:]
        chunk_g = gun[i0:i1] if i1 <= len(gun) else gun[i0:]
        if chunk_m.size == 0:
            t += STEP_SEC
            continue
        if float(np.max(chunk_m)) >= motion_thr or float(np.percentile(chunk_m, 90)) >= motion_thr:
            starts.add(round(t, 1))
        elif float(np.max(chunk_g)) >= float(np.percentile(gun, 85) if gun.size else 0.05):
            starts.add(round(t, 1))
        t += STEP_SEC

    if owner_anchors_enabled():
        for anchor in _owner_anchor_starts(video_path, profile):
            for off in (-60, -30, 0, 30, 60):
                s = anchor + off - WINDOW_SEC * 0.5
                if s >= 60:
                    starts.add(round(s, 1))

    if not starts and profile in SHOOTER_PROFILES:
        log.warning("highlight stage1 %s: no combat windows — refusing filler grid", video_path.name)

    ranked = _rank_stage1_starts(analysis, profile, sorted(starts), video_path=video_path)
    if not ranked:
        ranked = sorted(starts)
    ranked = _filter_bad_label_starts(video_path, profile, ranked)
    return ranked[:max_stage1]


def _parallel_workers() -> int:
    """CPU workers for parallel PANNs/CLIP window scoring (one thread per core)."""
    raw = (os.environ.get("HIGHLIGHT_PARALLEL_WORKERS") or "").strip()
    if raw:
        return max(1, int(raw))
    cpus = os.cpu_count() or 4
    # ~75% of cores — leave headroom for ffmpeg/OS on 8-core VPS.
    return max(2, min(6, cpus - 2, int(cpus * 0.75)))


def _pann_probe_limit(profile: str) -> int:
    profile = normalize_profile(profile)
    if profile in SHOOTER_PROFILES or os.environ.get("SHOOTER_VOD_FEED", "0") == "1":
        return max(
            8,
            int(
                os.environ.get(
                    "SHOOTER_VOD_MAX_PANN_PROBE",
                    os.environ.get("HIGHLIGHT_MAX_PANN_PROBE", "24"),
                )
            ),
        )
    max_pann = int(os.environ.get("HIGHLIGHT_MAX_PANN_PROBE", "36"))
    if os.environ.get("MLBB_VOD_ONLY", "0") == "1":
        max_pann = int(os.environ.get("HIGHLIGHT_MAX_PANN_PROBE", "5"))
    return max_pann


def stage1_panns_prefilter(
    video_path: Path,
    starts: list[float],
    profile: str,
    *,
    pinned: set[float] | None = None,
) -> list[float]:
    """Keep windows where PANNs gun max is promising (cheap batch on sparse set)."""
    profile = normalize_profile(profile)
    max_pann = _pann_probe_limit(profile)
    pinned_set = pinned or set()
    pinned_list = sorted(s for s in starts if any(abs(s - p) <= 1.0 for p in pinned_set))
    rest = [s for s in starts if s not in pinned_list]
    if profile not in SHOOTER_PROFILES:
        cap = max(0, max_pann - len(pinned_list))
        return sorted(set(pinned_list + rest[:cap]))
    starts = rest[:max_pann]
    pre_min = float(os.environ.get("HIGHLIGHT_PANN_PREFILTER_MIN", "0.12"))
    workers = _parallel_workers()

    def _probe(start: float) -> float | None:
        panns = score_panns_audio(video_path, start, WINDOW_SEC)
        return start if panns["panns_gun_max"] >= pre_min else None

    kept: list[float] = []
    if workers > 1 and len(starts) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for hit in pool.map(_probe, starts):
                if hit is not None:
                    kept.append(hit)
        kept.sort()
    else:
        for start in starts:
            hit = _probe(start)
            if hit is not None:
                kept.append(hit)
    if not kept and profile in OWNER_LABEL_PROFILES and _owner_anchor_starts(video_path, profile):
        kept = _owner_vicinity_gun_starts(video_path, profile)
        if kept:
            log.info(
                "highlight panns prefilter %s: owner-vicinity fallback %s windows",
                video_path.name,
                len(kept),
            )
    if not kept:
        log.warning("highlight panns prefilter %s: 0/%s passed min=%.3f", video_path.name, len(starts), pre_min)
    return kept


def _evaluate_highlight_start(
    video_path: Path,
    start: float,
    profile: str,
    *,
    skip_owner_bad: bool = False,
) -> tuple[float, HighlightMetrics] | None:
    metrics = score_candidate_window(
        video_path, start, WINDOW_SEC, profile, skip_owner_bad=skip_owner_bad
    )
    try:
        from viral_scorer import trim_segment_start

        trimmed = trim_segment_start(video_path, start, profile, window_sec=WINDOW_SEC)
        if abs(trimmed - start) > 0.1:
            metrics = score_candidate_window(
                video_path, trimmed, WINDOW_SEC, profile, skip_owner_bad=skip_owner_bad
            )
            start = trimmed
    except Exception:
        pass
    return start, metrics


def _accept_highlight_candidate(
    video_path: Path,
    start: float,
    metrics: HighlightMetrics,
    profile: str,
    *,
    banner_anchored: bool = False,
) -> bool:
    status = "PASS" if metrics.rule_pass else "FAIL"
    log.info(
        "[%s] highlight start=%.1f panns=%.3f clip=%.3f hook=%.3f viral=%.3f heat=%.3f reason=%s",
        status,
        start,
        metrics.panns_gun_max,
        metrics.clip_score,
        metrics.hook_score,
        metrics.viral_score,
        metrics.heatmap_intensity,
        metrics.pass_reason,
    )
    if not metrics.rule_pass or not metrics.visual_pass:
        return False
    if profile == "mobile_legends" and not owner_anchors_enabled():
        min_clip = float(os.environ.get("HIGHLIGHT_MLBB_AUTO_CLIP_MIN", "0.10"))
        if start < _mlbb_skip_intro_sec() and metrics.clip_score < min_clip:
            log.info(
                "[FAIL] highlight start=%.1f intro_clip=%.3f < %.3f",
                start,
                metrics.clip_score,
                min_clip,
            )
            return False
    hook_min = float(os.environ.get("VIRAL_SEGMENT_HOOK_MIN", "0.35"))
    if profile == "mobile_legends" and metrics.rule_pass and metrics.visual_pass:
        hook_min = float(os.environ.get("VIRAL_MLBB_HOOK_MIN", "0.06"))
        if banner_anchored:
            hook_min = min(
                hook_min,
                float(os.environ.get("MLBB_BANNER_ANCHOR_HOOK_MIN", "0.04")),
            )
    elif (
        metrics.hook_score < hook_min
        and profile in SHOOTER_PROFILES
        and metrics.panns_gun_max >= 0.35
        and metrics.visual_pass
    ):
        hook_min = float(os.environ.get("VIRAL_COMBAT_HOOK_MIN", "0.06"))
    if metrics.hook_score < hook_min:
        clip_bypass = float(os.environ.get("VIRAL_MLBB_CLIP_HOOK_MIN", "0.12"))
        if profile == "mobile_legends" and metrics.clip_score >= clip_bypass:
            return True
        if profile == "mobile_legends" and banner_anchored and metrics.clip_score >= 0.06:
            return True
        log.info(
            "[FAIL] highlight start=%.1f hook=%.3f < %.3f",
            start,
            metrics.hook_score,
            hook_min,
        )
        return False
    return True


def discover_highlight_candidates(
    video_path: Path,
    profile: str,
    *,
    used_keys: set[str] | None = None,
    segment_key_fn=None,
    sig: str = "",
    limit: int = 40,
) -> list[dict]:
    profile = normalize_profile(profile)
    used_keys = used_keys or set()
    ready, refuse_reason = require_inference_ready(profile)
    if not ready:
        log.error("highlight REFUSE %s: %s", video_path.name, refuse_reason)
        return []
    starts = stage1_candidates(video_path, profile)
    log.info("highlight stage1 %s: %s windows", video_path.name, len(starts))
    if profile == "mobile_legends":
        use_discover = os.environ.get("MLBB_VOD_BANNER_DISCOVER", "0") == "1"
        use_prefilter = os.environ.get("MLBB_VOD_BANNER_PREFILTER", "0") == "1"
        banner_pin: set[float] = set()
        banner_by_anchor: dict[float, object] = {}
        if use_discover or use_prefilter:
            from mlbb_kill_banner import discover_vod_kill_banners, filter_peaks_with_ocr_banner

            start_set = set(starts)
            banners: list = []
            if use_discover:
                banners = discover_vod_kill_banners(video_path, hint_peaks=starts)
            lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
            if banners:
                log.info(
                    "highlight banner discover %s: %s tier>=%s hits",
                    video_path.name,
                    len(banners),
                    os.environ.get("MLBB_KILL_BANNER_MIN_TIER", "double"),
                )
                anchor_starts = sorted({max(0.0, hit.sec - lead) for hit in banners})
                banner_pin = set(anchor_starts)
                for hit in banners:
                    anchor = max(0.0, hit.sec - lead)
                    prev = banner_by_anchor.get(anchor)
                    if prev is None or hit.tier > prev.tier:
                        banner_by_anchor[anchor] = hit
                for anchor in anchor_starts:
                    start_set.add(anchor)
            starts = sorted(start_set)
            if use_prefilter and starts:
                before = len(starts)
                filtered = filter_peaks_with_ocr_banner(video_path, starts, known_banners=banners)
                if banners:
                    anchor_starts = sorted({max(0.0, hit.sec - lead) for hit in banners})
                    near = [s for s in filtered if any(abs(s - a) <= 45 for a in anchor_starts)]
                    starts = sorted(set(near) | set(anchor_starts))
                else:
                    starts = filtered
                log.info(
                    "highlight banner prefilter %s: %s/%s windows",
                    video_path.name,
                    len(starts),
                    before,
                )
                if not starts and banners:
                    starts = sorted({max(0.0, hit.sec - lead) for hit in banners})
                    log.info(
                        "highlight banner prefilter %s: using %s banner anchors (peaks missed)",
                        video_path.name,
                        len(starts),
                    )
                if not starts and os.environ.get("MLBB_VOD_BANNER_SKIP_ON_MISS", "0") == "1":
                    log.warning(
                        "highlight %s: banner prefilter 0/%s — skip VOD",
                        video_path.name,
                        before,
                    )
                    return []
                if not starts:
                    if os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1":
                        log.warning(
                            "highlight %s: no OCR banner peaks — skip motion fallback",
                            video_path.name,
                        )
                        return []
                    cap = int(os.environ.get("HIGHLIGHT_MAX_STAGE1", "16"))
                    starts = sorted(start_set)[:cap]
                    log.info(
                        "highlight %s: banner prefilter 0 — keep %s motion peaks",
                        video_path.name,
                        len(starts),
                    )
    else:
        banner_pin = set()
        banner_by_anchor = {}
    starts = stage1_panns_prefilter(video_path, starts, profile, pinned=banner_pin)
    log.info("highlight panns prefilter %s: %s windows", video_path.name, len(starts))

    pending = [
        start
        for start in starts
        if not (segment_key_fn and sig and segment_key_fn(sig, start) in used_keys)
    ]
    workers = _parallel_workers()
    if workers > 1 and len(pending) > 1:
        log.info("highlight parallel score %s: %s windows x%d workers", video_path.name, len(pending), workers)

    verified: list[dict] = []

    def _consume(start: float, metrics: HighlightMetrics) -> bool:
        banner_hit = banner_by_anchor.get(start)
        if banner_hit is None and banner_by_anchor:
            for anchor, hit in banner_by_anchor.items():
                if abs(start - anchor) <= 6.0:
                    banner_hit = hit
                    break
        anchored = banner_hit is not None
        if (
            anchored
            and (not metrics.rule_pass or not metrics.visual_pass)
            and metrics.pass_reason == "owner_bad_window"
        ):
            rescored = _evaluate_highlight_start(
                video_path, start, profile, skip_owner_bad=True
            )
            if rescored is not None:
                start, metrics = rescored
        if not _accept_highlight_candidate(
            video_path, start, metrics, profile, banner_anchored=anchored
        ):
            return False
        row = {
            "source_path": str(video_path),
            "game_name": GAME_LABELS.get(profile, profile),
            "start": round(start, 3),
            "input_duration": WINDOW_SEC,
            "output_duration": WINDOW_SEC,
            "speed": 1.0,
            "score": metrics.viral_score or metrics.combined_score,
            "strict_score": metrics.viral_score or metrics.combined_score,
            "highlight_metrics": metrics.to_dict(),
            "gate_reason": metrics.pass_reason,
            "strict_metrics": metrics.to_dict(),
        }
        if banner_hit is not None:
            row.update(
                {
                    "kill_banner": getattr(banner_hit, "label", None) or getattr(banner_hit, "tier_name", ""),
                    "kill_banner_tier": int(getattr(banner_hit, "tier", 0) or 0),
                    "anchor": "kill_banner",
                    "banner_sec": float(getattr(banner_hit, "sec", start)),
                }
            )
        verified.append(row)
        return True

    if workers > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_evaluate_highlight_start, video_path, start, profile): start
                for start in pending
            }
            for fut in as_completed(futures):
                if len(verified) >= limit:
                    break
                try:
                    row = fut.result()
                except Exception as exc:
                    log.warning("highlight parallel score failed start=%s: %s", futures[fut], exc)
                    continue
                if row is None:
                    continue
                start, metrics = row
                if _consume(start, metrics) and len(verified) >= limit:
                    break
                if (
                    verified
                    and os.environ.get("MLBB_VOD_HIGHLIGHT_SEND_ONE", "0") == "1"
                    and os.environ.get("MLBB_VOD_ONLY", "0") == "1"
                ):
                    log.info("vod send_one: stop after first highlight pass start=%.1f", verified[-1]["start"])
                    break
    else:
        for start in pending:
            if len(verified) >= limit:
                break
            row = _evaluate_highlight_start(video_path, start, profile)
            if row is None:
                continue
            start, metrics = row
            if _consume(start, metrics) and len(verified) >= limit:
                break
            if (
                verified
                and os.environ.get("MLBB_VOD_HIGHLIGHT_SEND_ONE", "0") == "1"
                and os.environ.get("MLBB_VOD_ONLY", "0") == "1"
            ):
                log.info(
                    "vod send_one: stop after first highlight pass start=%.1f",
                    verified[-1]["start"],
                )
                break

    verified.sort(
        key=lambda c: (
            (c.get("highlight_metrics") or {}).get("viral_score", 0),
            c.get("score", 0),
        ),
        reverse=True,
    )
    log.info("highlight pool %s: %s passed", video_path.name, len(verified))
    return verified


def select_montage_segments(candidates: list[dict], used_keys: set[str], sig: str, segment_key_fn) -> list[dict]:
    pool = [
        c
        for c in candidates
        if segment_key_fn(sig, float(c["start"])) not in used_keys
    ]
    pool.sort(
        key=lambda c: (
            (c.get("highlight_metrics") or {}).get("viral_score", 0),
            c.get("score", 0),
        ),
        reverse=True,
    )
    chosen: list[dict] = []
    for cand in pool:
        start = float(cand["start"])
        if any(abs(start - float(c["start"])) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(cand)
        max_pick = int(os.environ.get("INTELLICLIP_MAX_CLIPS", str(TARGET_CLIPS)))
        if len(chosen) >= max_pick:
            break
    if len(chosen) < MIN_CLIPS:
        return []
    est = sum(float(c.get("output_duration", WINDOW_SEC)) for c in chosen)
    if est < 33.0:
        xfade = float(os.environ.get("TRANSITION_DURATION", "0.28"))
        target = 33.0 + xfade * max(0, len(chosen) - 1)
        per = target / len(chosen)
        for cand in chosen:
            cur = float(cand.get("output_duration") or cand.get("input_duration") or WINDOW_SEC)
            if cur < per:
                cand["input_duration"] = round(per, 3)
                cand["output_duration"] = round(per, 3)
        est = sum(float(c.get("output_duration", WINDOW_SEC)) for c in chosen)
        if est < 33.0:
            return []
    if est > 57.0 and len(chosen) > MIN_CLIPS:
        while len(chosen) > MIN_CLIPS and est > 57.0:
            chosen.pop()
            est = sum(float(c.get("output_duration", WINDOW_SEC)) for c in chosen)
    if chosen:
        vod = Path(str(chosen[0].get("source_path", "")))
        prof = normalize_profile((chosen[0].get("highlight_metrics") or {}).get("profile", "pubg"))
        if vod.exists():
            from preview_gate import rescore_clips

            rescored, ok, _reason = rescore_clips(vod, prof, chosen)
            if not ok:
                return []
            chosen = rescored
    return chosen
