#!/usr/bin/env bash
# Free CPU for overnight_msk: no parallel montage / MLBB 4h jobs.
set -Eeuo pipefail

echo "[$(date -Is)] stop_competing_workers"

pkill -f youtube_triple_montage.py 2>/dev/null || true
pkill -f hourly_new_sources_montage.py 2>/dev/null || true
pkill -f mlbb_hourly_cycle.sh 2>/dev/null || true
pkill -f overnight_orchestrator.sh 2>/dev/null || true
pkill -f run_parallel_stack.sh 2>/dev/null || true
pkill -f mass_download 2>/dev/null || true

# Abandon long proactive MLBB AV1 proxy / triple montage chain
tmux kill-session -t yt-h264-proxy 2>/dev/null || true
tmux kill-session -t yt-triple-wait 2>/dev/null || true
pkill -f 'ffmpeg.*iVMOD8v2MRk' 2>/dev/null || true

# Do not kill overnight_msk / overnight_youtube_batch
pgrep -af 'overnight_msk|overnight_youtube_batch' || true
pgrep -af 'smart_video_editor' || true

echo "[$(date -Is)] done"
