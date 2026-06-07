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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("highlight_scorer")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXEMPLAR_ROOT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO_ROOT / "data" / "highlight_exemplars")))
CLASSIFIER_PATH = Path(
    os.environ.get(
        "HIGHLIGHT_CLASSIFIER_PATH",
        str(REPO_ROOT / "data" / "mlbb" / "highlight_classifier.joblib"),
    )
)
OWNER_LABELS = REPO_ROOT / "data" / "pubg_owner_labels.json"

WINDOW_SEC = float(os.environ.get("HIGHLIGHT_WINDOW_SEC", "10"))
STEP_SEC = float(os.environ.get("HIGHLIGHT_STEP_SEC", "2"))
MIN_GAP_SEC = float(os.environ.get("HIGHLIGHT_MIN_GAP_SEC", "90"))
MIN_CLIPS = int(os.environ.get("HIGHLIGHT_MIN_CLIPS", "3"))
TARGET_CLIPS = int(os.environ.get("HIGHLIGHT_TARGET_CLIPS", "4"))

PANN_GUN_MIN = float(os.environ.get("HIGHLIGHT_PANN_GUN_MIN", "0.25"))
CLIP_MIN_SHOOTER = float(os.environ.get("HIGHLIGHT_CLIP_MIN_SHOOTER", "0.05"))
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
    clip_score: float = 0.0
    ocr_text: str = ""
    ocr_hits: int = 0
    center_motion: float = 0.0
    boss_bar: float = 0.0
    minimap_delta: float = 0.0
    skill_delta: float = 0.0
    classifier_prob: float = 1.0
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
            "clip_score": round(self.clip_score, 4),
            "ocr_text": self.ocr_text,
            "ocr_hits": self.ocr_hits,
            "center_motion": round(self.center_motion, 4),
            "boss_bar": round(self.boss_bar, 4),
            "minimap_delta": round(self.minimap_delta, 4),
            "skill_delta": round(self.skill_delta, 4),
            "classifier_prob": round(self.classifier_prob, 4),
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
    pretrained = os.environ.get("HIGHLIGHT_CLIP_PRETRAINED", "openai")
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
    embs: list[np.ndarray] = []
    from gameplay_gate import _read_frame_at

    for path in paths[:24]:
        frame = None
        if path.suffix.lower() in (".jpg", ".png"):
            import cv2

            frame = cv2.imread(str(path))
        else:
            frame = _read_frame_at(path, 1.0)
        if frame is None:
            continue
        tensor = preprocess(_frame_to_pil(frame)).unsqueeze(0).to(device)
        with torch.no_grad():
            emb = model.encode_image(tensor)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        embs.append(emb.cpu().numpy()[0])
    return tuple(embs)


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
    import open_clip
    import torch
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    profile = normalize_profile(profile)
    game = profile
    model, preprocess, _, device = _clip_bundle()
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
        clip_s = sim_good - sim_bad
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

    return (float(np.mean(scores)) if scores else 0.0, frame_rows)


def score_killfeed_ocr(video_path: Path, start_sec: float, duration_sec: float) -> tuple[str, int]:
    try:
        import cv2
        import pytesseract
        from gameplay_gate import _read_frame_at, detect_game_viewport_crop
    except ImportError:
        return "", 0

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    t = start_sec + duration_sec * 0.5
    frame = _read_frame_at(video_path, t)
    if frame is None:
        return "", 0
    if crop is not None:
        x, y, w, h = crop
        frame = frame[y : y + h, x : x + w]
    small = cv2.resize(frame, (320, 180))
    h, w = small.shape[:2]
    zone = small[int(h * 0.02) : int(h * 0.22), int(w * 0.62) : int(w * 0.98)]
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(gray, config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+- ")
    text = " ".join(text.split())
    hits = sum(1 for kw in ("kill", "knock", "eliminated", "headshot", "убил", "убийство") if kw.lower() in text.lower())
    return text[:120], hits


def _load_classifier():
    if not CLASSIFIER_PATH.exists():
        return None
    try:
        import joblib

        return joblib.load(CLASSIFIER_PATH)
    except Exception as exc:
        log.warning("classifier load failed: %s", exc)
        return None


def classifier_probability(metrics: HighlightMetrics) -> float:
    clf = _load_classifier()
    if clf is None:
        return 1.0
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
        return 1.0


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


def audio_passes_shooter(panns: dict[str, float]) -> tuple[bool, str]:
    gun_max = panns["panns_gun_max"]
    if gun_max < PANN_GUN_MIN:
        return False, f"panns_gun_low={gun_max:.3f}"
    if panns["panns_speech"] > 0.45 and gun_max < PANN_GUN_MIN * 1.2:
        return False, f"speech_dominant={panns['panns_speech']:.3f}"
    if panns["panns_music"] > 0.40 and gun_max < PANN_GUN_MIN * 1.15:
        return False, f"music_dominant={panns['panns_music']:.3f}"
    return True, "panns_gun_ok"


def rule_gate(profile: str, metrics: HighlightMetrics) -> tuple[bool, str]:
    profile = normalize_profile(profile)
    if profile in SHOOTER_PROFILES:
        if not metrics.audio_pass:
            return False, metrics.pass_reason or "audio_fail"
        if metrics.clip_score <= CLIP_MIN_SHOOTER:
            return False, f"clip_low={metrics.clip_score:.3f}"
        return True, "shooter_ab_ok"

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
) -> HighlightMetrics:
    profile = normalize_profile(profile)
    os.environ["_HIGHLIGHT_PROFILE"] = profile

    panns = score_panns_audio(video_path, start_sec, duration_sec)
    clip_score, frames = score_clip_exemplar(video_path, start_sec, duration_sec, profile)
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

    if profile in SHOOTER_PROFILES:
        m.audio_pass, audio_reason = audio_passes_shooter(panns)
        if not m.audio_pass:
            m.pass_reason = audio_reason
    else:
        m.audio_pass = True

    m.visual_pass = clip_score > (CLIP_MIN_SHOOTER if profile in SHOOTER_PROFILES else 0.03)
    m.classifier_prob = classifier_probability(m)
    m.rule_pass, rule_reason = rule_gate(profile, m)
    m.pass_reason = rule_reason if m.rule_pass else (m.pass_reason or rule_reason)

    if m.rule_pass and m.classifier_prob >= CLASSIFIER_MIN:
        m.combined_score = (
            m.panns_gun_max * 0.45
            + max(m.clip_score, 0) * 0.35
            + m.classifier_prob * 0.15
            + m.center_motion * 0.05
            + min(m.ocr_hits, 3) * 0.02
        )
    else:
        m.combined_score = 0.0
        m.rule_pass = False
        if m.classifier_prob < CLASSIFIER_MIN:
            m.pass_reason = f"classifier_low={m.classifier_prob:.3f}"

    return m


def _owner_anchor_starts(video_path: Path, profile: str) -> list[float]:
    profile = normalize_profile(profile)
    if profile != "pubg" or not OWNER_LABELS.exists():
        return []
    try:
        data = json.loads(OWNER_LABELS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vid = video_path.stem[3:] if video_path.stem.startswith("yt_") else video_path.stem
    rows = data.get("videos", {}).get(vid, [])
    pad = float(os.environ.get("HIGHLIGHT_OWNER_PAD_SEC", "120"))
    return [float(r["time_sec"]) for r in rows if r.get("label") == "good" and "time_sec" in r]


def stage1_candidates(video_path: Path, profile: str) -> list[float]:
    """Sliding 10s / step 2s with cheap motion prefilter + owner anchors."""
    from smart_video_editor import analyze_video

    profile = normalize_profile(profile)
    analysis = analyze_video(video_path)
    win = float(analysis.get("window_seconds", 2.0))
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    duration = float(analysis.get("duration") or (len(motion) * win))

    p90 = float(np.percentile(motion, 90)) if motion.size else 0.02
    motion_thr = max(0.018, p90 * 0.55)

    starts: set[float] = set()
    t = 90.0
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

    for anchor in _owner_anchor_starts(video_path, profile):
        for off in (-60, -30, 0, 30, 60):
            s = anchor + off - WINDOW_SEC * 0.5
            if s >= 60:
                starts.add(round(s, 1))

    return sorted(starts)


def stage1_panns_prefilter(video_path: Path, starts: list[float], profile: str) -> list[float]:
    """Keep windows where PANNs gun max is promising (cheap batch on sparse set)."""
    profile = normalize_profile(profile)
    if profile not in SHOOTER_PROFILES:
        return starts
    kept: list[float] = []
    pre_min = float(os.environ.get("HIGHLIGHT_PANN_PREFILTER_MIN", "0.12"))
    for start in starts:
        panns = score_panns_audio(video_path, start, WINDOW_SEC)
        if panns["panns_gun_max"] >= pre_min:
            kept.append(start)
    return kept or starts[: min(40, len(starts))]


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
    starts = stage1_candidates(video_path, profile)
    log.info("highlight stage1 %s: %s windows", video_path.name, len(starts))
    starts = stage1_panns_prefilter(video_path, starts, profile)
    log.info("highlight panns prefilter %s: %s windows", video_path.name, len(starts))

    verified: list[dict] = []
    for start in starts:
        if segment_key_fn and sig and segment_key_fn(sig, start) in used_keys:
            continue
        metrics = score_candidate_window(video_path, start, WINDOW_SEC, profile)
        status = "PASS" if metrics.rule_pass else "FAIL"
        log.info(
            "[%s] highlight start=%.1f panns_gun=%.3f clip=%.3f reason=%s",
            status,
            start,
            metrics.panns_gun_max,
            metrics.clip_score,
            metrics.pass_reason,
        )
        if not metrics.rule_pass:
            continue
        verified.append(
            {
                "source_path": str(video_path),
                "game_name": GAME_LABELS.get(profile, profile),
                "start": round(start, 3),
                "input_duration": WINDOW_SEC,
                "output_duration": WINDOW_SEC,
                "speed": 1.0,
                "score": metrics.combined_score,
                "strict_score": metrics.combined_score,
                "highlight_metrics": metrics.to_dict(),
                "gate_reason": metrics.pass_reason,
                "strict_metrics": metrics.to_dict(),
            }
        )
        if len(verified) >= limit:
            break

    verified.sort(key=lambda c: c.get("score", 0), reverse=True)
    log.info("highlight pool %s: %s passed", video_path.name, len(verified))
    return verified


def select_montage_segments(candidates: list[dict], used_keys: set[str], sig: str, segment_key_fn) -> list[dict]:
    chosen: list[dict] = []
    for cand in candidates:
        start = float(cand["start"])
        if segment_key_fn(sig, start) in used_keys:
            continue
        if any(abs(start - float(c["start"])) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(cand)
        if len(chosen) >= TARGET_CLIPS:
            break
    if len(chosen) < MIN_CLIPS:
        return []
    est = sum(float(c.get("output_duration", WINDOW_SEC)) for c in chosen)
    if est < 33.0:
        return []
    if est > 57.0 and len(chosen) > MIN_CLIPS:
        while len(chosen) > MIN_CLIPS and est > 57.0:
            chosen.pop()
            est = sum(float(c.get("output_duration", WINDOW_SEC)) for c in chosen)
    return chosen
