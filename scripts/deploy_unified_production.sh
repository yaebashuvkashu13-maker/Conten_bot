#!/usr/bin/env bash
# ONLY supported production deploy path.
# Supersedes parallel branches/PRs: vod-quality-ops-suite, vod-ffprobe-hang-fix,
# pubg-fight-only-gates, vod-hang-autounload, pubg-owner-combat-gates, etc.
# Always deploy from: cursor/vod-unified-production-a016 (#94)
# Usage: CONTENT_BOT_REPO=/root/content_bot_ml ./scripts/deploy_unified_production.sh
set -euo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
BRANCH="${UNIFIED_BRANCH:-cursor/vod-unified-production-a016}"
UNIT="${VOD_FEED_SYSTEMD_UNIT:-content-bot-vod-feed.service}"
ENV_FILE="${VOD_BOT_ENV_FILE:-/root/.video_bot.env}"
FEED_SCRIPT="scripts/shooter_vod_segment_feed.py"

cd "$REPO"
git fetch origin "$BRANCH"
# Stay on unified branch; never checkout slim feed branches.
git checkout -B "$BRANCH" "origin/$BRANCH"

# --- Preflight: refuse slim/wrong feed overwrite ---
python3 - <<'PY'
from pathlib import Path
import sys
feed = Path("scripts/shooter_vod_segment_feed.py")
if not feed.is_file():
    print("FATAL: shooter_vod_segment_feed.py missing", file=sys.stderr)
    sys.exit(2)
text = feed.read_text(encoding="utf-8")
lines = text.count("\n")
if lines < 2500:
    print(f"FATAL: feed looks slim ({lines} lines < 2500) — refuse deploy", file=sys.stderr)
    sys.exit(2)
for needle in (
    "_ledger_record_decision",
    "_ledger_record_send",
    "record_heartbeat",
    'VOD_FORCE_PRESEND_BYPASS", "0"',
    "entry_is_hard_bad_without_peaks",
):
    if needle not in text:
        print(f"FATAL: feed missing safety marker: {needle}", file=sys.stderr)
        sys.exit(2)
print(f"preflight OK feed_lines={lines}")
PY

# Mirror hot scripts into /usr/local/bin for PYTHONPATH consumers.
for f in \
  clip_hook_gate.py dislike_reason_gates.py vod_cheap_cascade.py telegram_delivery.py \
  vod_media_cache.py vod_clip_quality_ledger.py vod_weekly_quality_report.py \
  vod_inbox_recover.py vod_owner_feedback_bridge.py vod_send_drought_watch.py \
  game_adaptive_thresholds.py vod_hang_detector.py vod_force_send.py \
  smart_video_editor.py shooter_vod_segment_feed.py daily_cycle_runner.py \
  vod_feed_owner_health.py vod_telegram_env.py; do
  [[ -f "scripts/$f" ]] && cp -f "scripts/$f" "/usr/local/bin/$f"
done
# Compat wrappers that must stay unified-only on the box.
for f in vps_apply_vod_only.sh install_mlbb_vod_only.sh mlbb_vod_health_watchdog.sh \
  mlbb_continuous_worker_watchdog.sh run_owner_then_feed.sh; do
  [[ -f "scripts/$f" ]] && install -m 0755 "scripts/$f" "/usr/local/bin/$f"
done

# Install systemd-owned supervisor (single owner).
install -m 0755 scripts/mlbb_vod_segment_feed.sh /usr/local/bin/mlbb_vod_segment_feed.sh
install -m 0644 scripts/content_bot_vod_feed.service "/etc/systemd/system/${UNIT}"
# Keep legacy unit name working if present.
if [[ -f /etc/systemd/system/content-bot-vod-feed.service && "$UNIT" != content-bot-vod-feed.service ]]; then
  cp -f "/etc/systemd/system/${UNIT}" /etc/systemd/system/content-bot-vod-feed.service
fi
systemctl daemon-reload

bash scripts/install_vod_weekly_quality_report.sh || { echo "WARN: install_vod_weekly_quality_report.sh failed" >&2; INSTALL_WARN=1; }
bash scripts/install_vod_send_drought_watch.sh || { echo "WARN: install_vod_send_drought_watch.sh failed" >&2; INSTALL_WARN=1; }
bash scripts/install_vod_daily_quality_digest.sh || { echo "WARN: install_vod_daily_quality_digest.sh failed" >&2; INSTALL_WARN=1; }
bash scripts/install_vod_feed_owner_health.sh || { echo "WARN: install_vod_feed_owner_health.sh failed" >&2; INSTALL_WARN=1; }

# Pin safe defaults in env
python3 - <<PY
from pathlib import Path
p = Path("${ENV_FILE}")
wanted = {
    # PUBG-only production: ignore other game streams / finite quotas.
    "VOD_PUBG_ONLY": "1",
    "EU_PUBG_ONLY": "1",
    "DAILY_PUBG_QUOTA": "-1",
    "DAILY_MLBB_QUOTA": "0",
    "DAILY_STANDOFF_QUOTA": "0",
    "DAILY_GENSHIN_QUOTA": "0",
    "DAILY_WOT_QUOTA": "0",
    "DAILY_GAME_CYCLE_ENABLED": "0",
    "VOD_FORCE_SOFTEN": "0",
    "VOD_ABSOLUTE_SILENCE_SEC": "5400",
    "VOD_SILENCE_WARN_SEC": "3600",
    "VOD_FORCE_PRESEND_BYPASS": "0",
    "VOD_FORCE_SKIP_DISCOVERY": "0",
    "SHOOTER_VOD_SKIP_DISCOVERY": "0",
    "SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD": "0",
    "VOD_FORCE_PRESEND_GATE": "1",
    "VOD_FORCE_REJECT_LOOT": "1",
    "VOD_FORCE_RELAX_OWNER": "1",
    "PUBG_PRESEND_SHOOTING_GATE": "1",
    "CLIP_HOOK_GATE": "1",
    "DISLIKE_REASON_GATES": "1",
    "VOD_CHEAP_CASCADE": "1",
    "VOD_AUDIO_PREFLIGHT": "1",
    "TELEGRAM_ENCODE": "1",
    "VOD_QUALITY_LEDGER": "1",
    "VOD_ADAPTIVE_THRESHOLDS": "1",
    "VOD_DROUGHT_AUTO_RECOVER": "1",
    "VOD_DROUGHT_HOURS": "2",
    "VOD_LEDGER_SILENCE_HOURS": "3",
    # Permanent singles floors (anti-garbage). Drought soften may lower temporarily.
    "PUBG_EARLY_PAYOFF_REJECT_SINGLES": "0",
    "PUBG_SINGLES_GUN_PAYOFF_BYPASS": "0",
    "PUBG_SINGLES_GUN_QUALITY_BYPASS": "0",
    "PUBG_FAST_PAYOFF_MIN": "0.12",
    "PUBG_PAYOFF_SCORE_MIN": "0.28",
    "PUBG_PAYOFF_SCORE_MIN_SINGLES": "0.16",
    "PUBG_FIGHT_SCORE_MIN": "0.38",
    "PUBG_QUALITY_SCORE_MIN_SINGLES": "0.32",
}
# Stale drought pins that previously inverted soften (stricter than steady).
# Recover hard-assigns these; they must not linger in the env file.
drop_keys = {
    "VOD_FORCE_QUALITY_MIN",
    "VOD_FORCE_PAYOFF_MIN",
    "VOD_FORCE_GUN_DENSITY",
    "VOD_FORCE_ESCALATION",
}
text = p.read_text() if p.exists() else ""
lines = text.splitlines(); keys=set(); out=[]
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        out.append(line); continue
    k = line.split("=", 1)[0].strip()
    if k in drop_keys:
        continue
    if k in wanted:
        out.append(f"{k}={wanted[k]}"); keys.add(k)
    else:
        out.append(line)
for k,v in wanted.items():
    if k not in keys:
        out.append(f"{k}={v}")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("\\n".join(out) + "\\n")
print("env pinned", sorted(wanted))
print("env dropped", sorted(drop_keys))
PY


# --- Purge legacy nohup watchdogs that fight systemd ownership ---
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -Ev 'continuous_worker_watchdog|mlbb_vod_health_watchdog|mlbb_vod_only_watchdog|vps_apply_vod_only|vps_auto_update|install_mlbb_vod_only' >"$tmp_cron" || true
  crontab "$tmp_cron" || true
  rm -f "$tmp_cron"
  echo "purged legacy nohup watchdog cron lines (if any)"
fi
# Also stop/disable any leftover timer units if present.
systemctl disable --now mlbb-vod-health-watchdog.timer 2>/dev/null || true
systemctl disable --now mlbb-continuous-worker-watchdog.timer 2>/dev/null || true

# Reinstall systemd-only hang healer (replaces purged mlbb_vod_health_watchdog cron).
CONTENT_BOT_REPO="$REPO" VOD_BOT_ENV_FILE="$ENV_FILE" \
  bash scripts/install_vod_hang_watch.sh || { echo "WARN: install_vod_hang_watch.sh failed" >&2; INSTALL_WARN=1; }

# --- Single owner restart: stop unit, kill orphans, clear stale lock, start unit ---
systemctl stop "$UNIT" 2>/dev/null || true
systemctl stop content-bot-vod-feed.service 2>/dev/null || true
# Kill any leftover supervisors/feeds not under systemd.
pkill -f 'mlbb_vod_segment_feed\\.sh' 2>/dev/null || true
pkill -f 'shooter_vod_segment_feed\\.py' 2>/dev/null || true
pkill -f 'mlbb_vod_segment_feed\\.py' 2>/dev/null || true
sleep 2
rm -f /tmp/mlbb_vod_supervisor.lock /tmp/mlbb_vod_segment_feed.lock /tmp/pubg_vod_segment_feed.lock
systemctl reset-failed "$UNIT" 2>/dev/null || true
systemctl enable --now "$UNIT"
sleep 5
systemctl is-active "$UNIT"
pgrep -af 'mlbb_vod_segment_feed.sh|shooter_vod_segment_feed.py' || echo "WARN: feed process not visible yet"
if ! python3 /usr/local/bin/vod_feed_owner_health.py --game pubg; then
  echo "WARN: vod_feed_owner_health reported problems (see JSON above)" >&2
fi
echo "deployed $BRANCH @ $(git rev-parse --short HEAD) unit=$UNIT"
