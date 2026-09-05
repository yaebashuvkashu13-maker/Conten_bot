#!/usr/bin/env python3
"""Fail CI if production safety invariants regress (bypass defaults, slim feed)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
errors: list[str] = []

feed = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
if feed.count("\n") < 2500:
    errors.append("shooter_vod_segment_feed.py looks slim (<2500 lines); refuse overwrite of production feed")
if 'setdefault("VOD_FORCE_PRESEND_BYPASS", "0")' not in feed and 'VOD_FORCE_PRESEND_BYPASS", "0"' not in feed:
    errors.append("feed default VOD_FORCE_PRESEND_BYPASS must be 0")
if re.search(r'return\s+True,\s*"keepalive_esc2_pass"', feed):
    errors.append("feed must not return keepalive_esc2_pass menu/loot bypass")
if "apply_to_environ" not in feed:
    errors.append("feed must apply game_adaptive_thresholds on startup")

hang = (SCRIPTS / "vod_hang_detector.py").read_text(encoding="utf-8")
if 'target["VOD_FORCE_PRESEND_BYPASS"] = "0"' not in hang:
    errors.append("vod_hang_detector must hard-set PRESEND_BYPASS=0")
if 'target["VOD_FORCE_SKIP_DISCOVERY"] = "0"' not in hang:
    errors.append("vod_hang_detector must hard-set SKIP_DISCOVERY=0")
if 'target["SHOOTER_VOD_SKIP_DISCOVERY"] = "0"' not in hang:
    errors.append("vod_hang_detector must hard-set SHOOTER_VOD_SKIP_DISCOVERY=0 (feed knob)")

force = (SCRIPTS / "vod_force_send.py").read_text(encoding="utf-8")
if 'env["VOD_FORCE_PRESEND_BYPASS"] = "0"' not in force:
    errors.append("vod_force_send must force PRESEND_BYPASS=0")
if 'VOD_FORCE_PRESEND_GATE", "0"' in force or "VOD_FORCE_PRESEND_GATE', '0'" in force:
    errors.append("vod_force_send must default VOD_FORCE_PRESEND_GATE to 1 (keep shooting gate)")
if 'VOD_FORCE_PRESEND_GATE", "1"' not in force and "VOD_FORCE_PRESEND_GATE', '1'" not in force:
    errors.append("vod_force_send must default VOD_FORCE_PRESEND_GATE=1")

score = (SCRIPTS / "pubg_quality_score.py").read_text(encoding="utf-8")
if 'PUBG_SINGLES_GUN_PAYOFF_BYPASS", "1"' not in score:
    errors.append("pubg_quality_score must default SINGLES_GUN_PAYOFF_BYPASS=1")
if 'PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0"' not in score:
    errors.append("pubg_quality_score must default EARLY_PAYOFF_REJECT_SINGLES=0")

deploy = (SCRIPTS / "deploy_unified_production.sh").read_text(encoding="utf-8")
for needle in (
    '"VOD_FORCE_PRESEND_BYPASS": "0"',
    '"VOD_FORCE_PRESEND_GATE": "1"',
    '"SHOOTER_VOD_SKIP_DISCOVERY": "0"',
):
    if needle not in deploy:
        errors.append(f"deploy_unified_production.sh missing pin {needle}")

bad = re.findall(r'os\.environ\.get\("VOD_FORCE_PRESEND_BYPASS",\s*"1"\)', hang + force + feed)
if bad:
    errors.append(f"found PRESEND_BYPASS default 1 ({len(bad)} times)")

if errors:
    print("PROD SAFETY CHECK FAILED:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("prod safety check OK")
