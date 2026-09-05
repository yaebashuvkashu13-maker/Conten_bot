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
if "_ledger_record_decision" not in feed or "_ledger_record_send" not in feed:
    errors.append("feed must write quality ledger on reject/send")
if "record_heartbeat" not in feed:
    errors.append("feed must write quality ledger heartbeat on startup")
if 'SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD", "0"' not in feed:
    errors.append("feed default SKIP_DISCOVERY_WHEN_INBOX_DEAD must be 0")
if "entry_is_hard_bad_without_peaks" not in feed:
    errors.append("feed recycle must skip hard-bad VODs without peaks")

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
if 'VOD_FORCE_RELAX_OWNER", "1"' not in force and "VOD_FORCE_RELAX_OWNER', '1'" not in force:
    errors.append("vod_force_send must default VOD_FORCE_RELAX_OWNER to 1 on esc2")
if 'VOD_FORCE_REJECT_LOOT", "1"' not in force and "VOD_FORCE_REJECT_LOOT', '1'" not in force:
    errors.append("vod_force_send must default loot reject ON for esc0/1 drought")
if "apply_drought_pubg_env" not in force:
    errors.append("vod_force_send must use apply_drought_pubg_env helper")

score = (SCRIPTS / "pubg_quality_score.py").read_text(encoding="utf-8")
if "_singles_gun_bypass_enabled" not in score:
    errors.append("pubg_quality_score must gate gun bypass via _singles_gun_bypass_enabled")
if 'PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0"' not in score:
    errors.append("pubg_quality_score must default EARLY_PAYOFF_REJECT_SINGLES=0")

deploy = (SCRIPTS / "deploy_unified_production.sh").read_text(encoding="utf-8")
for needle in (
    '"VOD_FORCE_PRESEND_BYPASS": "0"',
    '"VOD_FORCE_PRESEND_GATE": "1"',
    '"SHOOTER_VOD_SKIP_DISCOVERY": "0"',
    '"PUBG_SINGLES_GUN_PAYOFF_BYPASS": "0"',
    '"SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD": "0"',
    "mlbb_vod_segment_feed.sh",
    "content_bot_vod_feed.service",
    "vod_feed_owner_health",
    "feed looks slim",
):
    if needle not in deploy:
        errors.append(f"deploy_unified_production.sh missing {needle}")

if not (SCRIPTS / "mlbb_vod_segment_feed.sh").is_file():
    errors.append("mlbb_vod_segment_feed.sh supervisor must live in repo")
if not (SCRIPTS / "content_bot_vod_feed.service").is_file():
    errors.append("content_bot_vod_feed.service unit must live in repo")
if not (SCRIPTS / "vod_feed_owner_health.py").is_file():
    errors.append("vod_feed_owner_health.py must exist")
if not (SCRIPTS / "install_vod_daily_quality_digest.sh").is_file():
    errors.append("install_vod_daily_quality_digest.sh must exist")
if not (SCRIPTS / "install_vod_feed_owner_health.sh").is_file():
    errors.append("install_vod_feed_owner_health.sh must exist")

cal = (SCRIPTS / "pubg_owner_calibration.py").read_text(encoding="utf-8")
if 'PUBG_SINGLES_GUN_PAYOFF_BYPASS", "0"' not in cal:
    errors.append("pubg_owner_calibration must default SINGLES_GUN_PAYOFF_BYPASS=0 (drought-only)")

adaptive = (SCRIPTS / "shooter_vod_adaptive_gate.py").read_text(encoding="utf-8")
if "SMART_PUBG_MIN_GUNFIRE_DENSITY" in adaptive:
    errors.append("shooter_vod_adaptive_gate must not own SMART_PUBG_MIN_GUNFIRE_DENSITY floors")
if "apply_to_environ" not in adaptive:
    errors.append("shooter_vod_adaptive_gate must re-apply game_adaptive_thresholds")
for level_name in ("SHOOTER_SOFTEN_L3", "SHOOTER_SOFTEN_L4"):
    block = re.search(
        rf"{level_name}: dict\[str, str\] = \{{(.*?)\n\}}",
        adaptive,
        re.S,
    )
    if block and "PUBG_RELAX_OWNER_HEURISTICS" in block.group(1):
        errors.append(f"{level_name} must not declare PUBG_RELAX_OWNER_HEURISTICS")

ledger = (SCRIPTS / "vod_clip_quality_ledger.py").read_text(encoding="utf-8")
if "def record_heartbeat" not in ledger:
    errors.append("vod_clip_quality_ledger must expose record_heartbeat")
if "def latest_gate_event_age_sec" not in ledger:
    errors.append("vod_clip_quality_ledger must expose latest_gate_event_age_sec")

drought = (SCRIPTS / "vod_send_drought_watch.py").read_text(encoding="utf-8")
if "latest_gate_event_age_sec" not in drought or "ledger_silent" not in drought:
    errors.append("vod_send_drought_watch must alert on silent ledger")

bad = re.findall(r'os\.environ\.get\("VOD_FORCE_PRESEND_BYPASS",\s*"1"\)', hang + force + feed)
if bad:
    errors.append(f"found PRESEND_BYPASS default 1 ({len(bad)} times)")

if "_drought_floor_cap" not in (SCRIPTS / "game_adaptive_thresholds.py").read_text(encoding="utf-8"):
    errors.append("game_adaptive_thresholds must cap floors under drought soften")

if errors:
    print("PROD SAFETY CHECK FAILED:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("prod safety check OK")
