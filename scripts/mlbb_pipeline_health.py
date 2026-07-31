#!/usr/bin/env python3
"""Startup health checks for MLBB VOD pipeline — log blockers before wasting hours."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _ref_bank_root() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_REF_ROOT",
            "/root/content_bot_ml/data/mlbb_kill_banners",
        )
    )


def check_ref_bank() -> dict:
    root = _ref_bank_root()
    owner = root / "owner_cal" / "positive"
    neg = root / "owner_cal" / "negative"
    pos_n = len(list(owner.rglob("*.png"))) if owner.is_dir() else 0
    neg_n = len(list(neg.rglob("*.png"))) if neg.is_dir() else 0
    ok = pos_n >= 8
    return {
        "ok": ok,
        "root": str(root),
        "positive_refs": pos_n,
        "negative_refs": neg_n,
    }


def check_hero_icons() -> dict:
    root = Path(
        os.environ.get(
            "MLBB_HERO_ICON_ROOT",
            "/root/content_bot_ml/data/mlbb_hero_icons",
        )
    )
    icons = list(root.glob("*/icon.png")) if root.is_dir() else []
    return {"ok": len(icons) >= 40, "icons": len(icons), "root": str(root)}


def check_state_health(state: dict) -> dict:
    used = state.get("used_youtube_ids") or []
    vods = state.get("vods") or []
    alive = sum(
        1
        for r in vods
        if isinstance(r, dict) and Path(str(r.get("path") or "")).exists()
    )
    exhausted = sum(1 for r in vods if isinstance(r, dict) and r.get("exhausted"))
    return {
        "used_youtube_ids": len(used),
        "registry_rows": len(vods),
        "registry_files_alive": alive,
        "registry_exhausted": exhausted,
        "discovery_empty_streak": int(state.get("discovery_empty_streak") or 0),
        "zero_send_ids": len(state.get("zero_send_youtube_ids") or []),
    }


def log_pipeline_health(*, state: dict | None = None) -> dict:
    """Run checks and log a one-line summary. Returns full report dict."""
    report: dict = {
        "reliable": os.environ.get("MLBB_VOD_RELIABLE", "0") == "1",
        "discover_merge_tier": os.environ.get("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER", "?"),
        "discover_title_cap": os.environ.get("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", "?"),
        "min_peak_sec": os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "?"),
        "discover_max_sec": os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "?"),
    }
    report["ref_bank"] = check_ref_bank()
    report["hero_icons"] = check_hero_icons()
    if state is not None:
        report["state"] = check_state_health(state)

    ref = report["ref_bank"]
    icons = report["hero_icons"]
    parts = [
        f"ref={ref['positive_refs']}",
        f"hero_icons={icons['icons']}",
        f"discover_cap={report['discover_title_cap']}",
        f"merge_tier={report['discover_merge_tier']}",
    ]
    if state is not None:
        st = report["state"]
        parts.extend(
            [
                f"used_ids={st['used_youtube_ids']}",
                f"registry_alive={st['registry_files_alive']}",
                f"discover_miss={st['discovery_empty_streak']}",
            ]
        )
    if not ref["ok"]:
        log.warning("pipeline health: ref bank thin (%s) — banner discover may miss", ref)
    if not icons["ok"]:
        log.warning("pipeline health: hero icons thin (%s)", icons)
    log.info("pipeline health: %s", " ".join(parts))
    return report
