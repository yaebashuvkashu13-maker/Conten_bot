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


# Ops alert credentials must accept TG_* (prod) aliases.
for alert_script in ("vod_feed_owner_health.py", "vod_send_drought_watch.py", "vod_weekly_quality_report.py"):
    body = (SCRIPTS / alert_script).read_text(encoding="utf-8")
    if "vod_telegram_env" not in body and "TG_BOT_TOKEN" not in body:
        errors.append(f"{alert_script} must resolve TG_BOT_TOKEN / vod_telegram_env")

hang_body = (SCRIPTS / "vod_hang_detector.py").read_text(encoding="utf-8")
if "restart_supervisor(force=True)" in hang_body:
    errors.append("vod_hang_detector must not dual-start nohup via restart_supervisor")

recover_body = (SCRIPTS / "vod_feed_recover.py").read_text(encoding="utf-8")
if "VOD_FEED_ALLOW_NOHUP" not in recover_body:
    errors.append("vod_feed_recover must gate nohup behind VOD_FEED_ALLOW_NOHUP")

unit_body = (SCRIPTS / "content_bot_vod_feed.service").read_text(encoding="utf-8")
if "Restart=on-failure" not in unit_body:
    errors.append("content_bot_vod_feed.service must use Restart=on-failure")

deploy_body = (SCRIPTS / "deploy_unified_production.sh").read_text(encoding="utf-8")
if "mlbb_vod_health_watchdog" not in deploy_body and "continuous_worker_watchdog" not in deploy_body:
    errors.append("deploy_unified_production.sh must purge legacy watchdog crons")

if 'os.environ.get(env_key, "1") == "1"' in (SCRIPTS / "pubg_quality_score.py").read_text(encoding="utf-8"):
    errors.append("gun bypass missing-key default must not be 1")


# --- Unified-only deploy blast radius (legacy path must not regress) ---
install = (SCRIPTS / "install_mlbb_vod_only.sh").read_text(encoding="utf-8")
if "deploy_unified_production.sh" not in install:
    errors.append("install_mlbb_vod_only.sh must delegate to deploy_unified_production.sh")
if "Restart=always" in install:
    errors.append("install_mlbb_vod_only.sh must not ship Restart=always")
if re.search(r"flock.*\n.*exit 0", install) or "exit 0" in install and "DEPRECATED" not in install[:400]:
    # Deprecated wrapper may exit 2; refuse writing exit 0 flock handlers.
    if "flock" in install and "exit 0" in install and "DEPRECATED" not in install:
        errors.append("install_mlbb_vod_only.sh must not keep flock exit 0")
if "mlbb_continuous_worker_watchdog" in install and "DEPRECATED" not in install:
    errors.append("install_mlbb_vod_only.sh must not re-add continuous_worker_watchdog cron")

apply = (SCRIPTS / "vps_apply_vod_only.sh").read_text(encoding="utf-8")
if "deploy_unified_production.sh" not in apply:
    errors.append("vps_apply_vod_only.sh must call deploy_unified_production.sh")
if "install_mlbb_vod_only.sh" in apply and "deploy_unified_production.sh" not in apply:
    errors.append("vps_apply_vod_only.sh must not call legacy install")

health_wd = (SCRIPTS / "mlbb_vod_health_watchdog.sh").read_text(encoding="utf-8")
if "nohup" in health_wd and 'VOD_FEED_ALLOW_NOHUP' not in health_wd:
    # Allow gated telegram nohup only
    if re.search(r'nohup\s+"?\$BIN/mlbb_vod_segment_feed', health_wd) or "nohup \"$BIN/mlbb_vod_segment_feed" in health_wd:
        errors.append("mlbb_vod_health_watchdog.sh must not nohup the feed supervisor")
if "REFUSED nohup feed" not in health_wd and "NEVER nohup" not in health_wd:
    errors.append("mlbb_vod_health_watchdog.sh must refuse nohup feed restart")

cont_wd = (SCRIPTS / "mlbb_continuous_worker_watchdog.sh").read_text(encoding="utf-8")
if re.search(r'nohup\s+"\$VOD_WRAPPER"', cont_wd) or "nohup \"$VOD_WRAPPER\"" in cont_wd:
    errors.append("mlbb_continuous_worker_watchdog.sh must not nohup VOD_WRAPPER")

if re.search(r"vod_feed_owner_health\.py\s+vod_telegram_env\.py\s+--", deploy) or "vod_telegram_env.py --game" in deploy:
    errors.append("deploy must not pass vod_telegram_env.py as argv to health")

# Copying vod_telegram_env.py into /usr/local/bin is required; that is not an argv bug.

hang2 = (SCRIPTS / "vod_hang_detector.py").read_text(encoding="utf-8")
if "vod_telegram_env" not in hang2:
    errors.append("vod_hang_detector must send alerts via vod_telegram_env")
if "subprocess.Popen" in hang2 and "VOD_FEED_ALLOW_NOHUP" not in hang2:
    errors.append("vod_hang_detector bot Popen must be gated by VOD_FEED_ALLOW_NOHUP")

if "feed_scanning" not in feed and 'reason="feed_scanning"' not in feed:
    errors.append("feed must record_heartbeat on scanning")

unit = (SCRIPTS / "content_bot_vod_feed.service").read_text(encoding="utf-8")
if "Restart=always" in unit:
    errors.append("content_bot_vod_feed.service must not use Restart=always")
if "Restart=on-failure" not in unit:
    errors.append("content_bot_vod_feed.service must use Restart=on-failure")

sup = (SCRIPTS / "mlbb_vod_segment_feed.sh").read_text(encoding="utf-8")
sup_code = "\n".join(ln for ln in sup.splitlines() if not ln.lstrip().startswith("#"))

if "exit 1" not in sup_code:
    errors.append("mlbb_vod_segment_feed.sh flock miss must exit 1")

wf = (ROOT / ".github/workflows/deploy-vps.yml").read_text(encoding="utf-8")
if "install_mlbb_vod_only.sh" in wf or "vod-pipeline-base" in wf:
    errors.append("deploy-vps.yml must use unified branch + deploy_unified_production.sh")
if "deploy_unified_production.sh" not in wf:
    errors.append("deploy-vps.yml must call deploy_unified_production.sh")

# run_owner_then_feed must not dual-start feed via nohup
owner_feed = (SCRIPTS / "run_owner_then_feed.sh").read_text(encoding="utf-8")
if re.search(r"nohup\s+.*mlbb_vod_segment_feed", owner_feed):
    errors.append("run_owner_then_feed.sh must not nohup the VOD supervisor")
if "systemctl" not in owner_feed or "REFUSED nohup feed" not in owner_feed:
    errors.append("run_owner_then_feed.sh must start feed via systemd only")

# Continuous Shorts watchdog must refuse when VOD unit present / ungated nohup telegram
if "vod systemd unit present" not in cont_wd and "refuse Shorts" not in cont_wd:
    errors.append("mlbb_continuous_worker_watchdog.sh must refuse Shorts path when VOD unit exists")
if re.search(r'nohup python3 "\$TELEGRAM_BOT"', cont_wd):
    # Must be gated by VOD_FEED_ALLOW_NOHUP nearby
    if "VOD_FEED_ALLOW_NOHUP" not in cont_wd:
        errors.append("mlbb_continuous_worker_watchdog.sh telegram nohup must be gated")
if "REFUSED nohup continuous_worker" not in cont_wd and "REFUSED nohup feed" not in cont_wd:
    errors.append("mlbb_continuous_worker_watchdog.sh must refuse ungated continuous_worker nohup")

# Emergency Shorts restore must refuse under VOD ownership
emerg = (SCRIPTS / "mlbb_emergency_restore.sh").read_text(encoding="utf-8")
if "REFUSED" not in emerg or "deploy_unified_production.sh" not in emerg:
    errors.append("mlbb_emergency_restore.sh must refuse under VOD / point to unified deploy")

cont_install = (SCRIPTS / "install_mlbb_continuous_worker.sh").read_text(encoding="utf-8")
if "REFUSED" not in cont_install or "deploy_unified_production.sh" not in cont_install:
    errors.append("install_mlbb_continuous_worker.sh must refuse under VOD ownership")

# Inventory / import tips must not teach legacy install as primary
inv = (ROOT / "docs" / "REPO_INVENTORY.md").read_text(encoding="utf-8")
active = inv.split("## Active")[1].split("## Dormant")[0] if "## Active" in inv and "## Dormant" in inv else inv
if "deploy_unified_production.sh" not in active:
    errors.append("REPO_INVENTORY must list deploy_unified_production.sh as active")

import_tip = (SCRIPTS / "import_vod_state_bundle.sh").read_text(encoding="utf-8")
if "install_mlbb_vod_only.sh" in import_tip and "deploy_unified_production.sh" not in import_tip:
    errors.append("import_vod_state_bundle.sh must recommend deploy_unified_production.sh")


if errors:
    print("PROD SAFETY CHECK FAILED:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("prod safety check OK")
