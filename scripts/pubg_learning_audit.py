#!/usr/bin/env python3
"""
Audit PUBG VOD feed: find owner-labeled kill moments the bot missed in logs.

Usage:
  python3 pubg_learning_audit.py
  python3 pubg_learning_audit.py --vod VIDEO_ID --good-tol 15
  python3 pubg_learning_audit.py --train-missed  # append missed goods to owner labels
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_PUBG = Path(os.environ.get("SHOOTER_PUBG_DATA_ROOT", "/root/data/pubg"))
LOG = Path(os.environ.get("MLBB_VOD_FEED_LOG", "/root/data/mlbb/mlbb_vod_segment_feed.log"))
OWNER_PATH = Path(
    os.environ.get("PUBG_OWNER_LABELS_PATH", str(REPO / "data" / "pubg_owner_labels.json"))
)
VSEG_LABELS = Path(os.environ.get("SHOOTER_PUBG_VSEG_LABELS", str(DATA_PUBG / "vod_segment_labels.json")))
INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))

PRESEND_REJECT_RE = re.compile(
    r"presend REJECT (?P<seg>[^:]+): (?P<reason>.+)$|"
    r"\[FAIL\] highlight start=(?P<start>[0-9.]+).*reason=(?P<hreason>.+)$"
)
METRO_REJECT_RE = re.compile(r"metro reject vod=(?P<vod>\S+).*reason=(?P<reason>.+)$")


def _read_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def load_owner_good_anchors(*, skip_shorts_fullclip: bool = True) -> dict[str, list[float]]:
    data = _read_json(OWNER_PATH, {"videos": {}})
    out: dict[str, list[float]] = {}
    for vid, rows in (data.get("videos") or {}).items():
        times: list[float] = []
        for row in rows:
            if row.get("label") != "good":
                continue
            if skip_shorts_fullclip and row.get("source") == "youtube_shorts" and float(row.get("time_sec", 0)) == 0.0:
                continue
            times.append(float(row.get("time_sec", 0)))
        if times:
            out[str(vid)] = sorted(times)
    return out


def parse_log_combat_passes(log_path: Path) -> list[dict]:
    """[PASS] combat windows from highlight scorer — for unsent-moment audit."""
    if not log_path.exists():
        return []
    import re

    rows: list[dict] = []
    current_vod = ""
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"highlight (?:stage1|pool) yt_([A-Za-z0-9_-]{11})", line)
        if m:
            current_vod = m.group(1)
        m = re.search(
            r"\[PASS\] highlight start=([0-9.]+).*reason=(combat_ok[^,\s]*)",
            line,
        )
        if m and current_vod:
            rows.append(
                {
                    "vod": current_vod,
                    "time_sec": float(m.group(1)),
                    "reason": m.group(2),
                }
            )
    return rows


def audit_unsent_combat_passes(
    passes: list[dict],
    sent_ids: set[str],
    *,
    lead: float = 4.0,
) -> list[dict]:
    """Combat PASS in logs but no Telegram segment id near that time."""
    missed: list[dict] = []
    for row in passes:
        vid = row["vod"]
        t = float(row["time_sec"])
        start = max(0.0, t - lead)
        sid = f"{vid}_{int(start)}"
        if sid not in sent_ids:
            missed.append({**row, "expected_segment_id": sid})
    return missed


def load_vseg_good_anchors() -> dict[str, list[float]]:
    data = _read_json(VSEG_LABELS, {"good": []})
    out: dict[str, list[float]] = {}
    for row in data.get("good", []):
        vod = str(row.get("vod", "")).strip()
        vid = vod
        if vid.startswith("yt_"):
            vid = vid[3:]
        if vid.endswith(".mp4"):
            vid = Path(vid).stem.replace("yt_", "", 1)
        start = float(row.get("start", 0) or 0)
        if vid and start > 0:
            out.setdefault(vid, []).append(start)
    for vid in out:
        out[vid] = sorted(set(out[vid]))
    return out


def parse_log_rejects(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    rows: list[dict] = []
    current_vod = ""
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "candidate " in line and " id=" in line:
            m = re.search(r"\bid=([A-Za-z0-9_-]{6,})\b", line)
            if m:
                current_vod = m.group(1)
        m = METRO_REJECT_RE.search(line)
        if m:
            rows.append(
                {
                    "kind": "metro_reject",
                    "vod": m.group("vod"),
                    "time_sec": None,
                    "reason": m.group("reason").strip(),
                }
            )
        m = PRESEND_REJECT_RE.search(line)
        if m:
            if m.group("seg"):
                seg = m.group("seg").strip()
                vid = seg.rsplit("_", 1)[0] if "_" in seg else current_vod
                try:
                    t = float(seg.rsplit("_", 1)[-1])
                except ValueError:
                    t = None
                rows.append(
                    {
                        "kind": "presend_reject",
                        "vod": vid,
                        "time_sec": t,
                        "reason": m.group("reason").strip(),
                    }
                )
            elif m.group("start"):
                rows.append(
                    {
                        "kind": "highlight_fail",
                        "vod": current_vod,
                        "time_sec": float(m.group("start")),
                        "reason": (m.group("hreason") or "").strip(),
                    }
                )
    return rows


def nearest_reject(rejects: list[dict], vid: str, time_sec: float, tol: float) -> dict | None:
    best: dict | None = None
    best_dist = tol + 1.0
    for row in rejects:
        if row.get("vod") != vid:
            continue
        t = row.get("time_sec")
        if t is None:
            continue
        dist = abs(float(t) - time_sec)
        if dist <= tol and dist < best_dist:
            best_dist = dist
            best = row
    return best


def audit_vod(
    video_id: str,
    good_times: list[float],
    rejects: list[dict],
    *,
    good_tol: float,
) -> dict:
    missed: list[dict] = []
    explained: list[dict] = []
    for t in good_times:
        hit = nearest_reject(rejects, video_id, t, good_tol)
        if hit:
            explained.append({"time_sec": t, **hit})
        else:
            missed.append({"time_sec": t, "vod": video_id})
    return {
        "video_id": video_id,
        "good_anchors": len(good_times),
        "explained_misses": len(explained),
        "unexplained_misses": len(missed),
        "missed": missed,
        "explained": explained[:20],
    }


def merge_anchors(*maps: dict[str, list[float]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for m in maps:
        for vid, times in m.items():
            out.setdefault(vid, []).extend(times)
    for vid in out:
        out[vid] = sorted(set(out[vid]))
    return out


def train_missed_goods(report: list[dict]) -> int:
    from pubg_owner_learning import append_owner_time_label

    added = 0
    for row in report:
        for miss in row.get("missed", []):
            if append_owner_time_label(
                row["video_id"],
                float(miss["time_sec"]),
                "good",
                note="audit_missed",
                source="audit",
            ):
                added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PUBG missed kill moments from logs")
    parser.add_argument("--vod", default="", help="Single video id")
    parser.add_argument("--good-tol", type=float, default=15.0)
    parser.add_argument("--log", default=str(LOG))
    parser.add_argument("--train-missed", action="store_true")
    args = parser.parse_args()

    anchors = merge_anchors(load_owner_good_anchors(), load_vseg_good_anchors())
    if args.vod:
        anchors = {args.vod: anchors.get(args.vod, [])}
    rejects = parse_log_rejects(Path(args.log))
    sent_raw = _read_json(DATA_PUBG / "vod_segment_feed_sent.json", {"sent": []})
    sent_ids = set(sent_raw.get("sent", []) if isinstance(sent_raw, dict) else sent_raw)
    combat_passes = parse_log_combat_passes(Path(args.log))
    unsent = audit_unsent_combat_passes(combat_passes, sent_ids)
    if unsent:
        print(f"\n=== combat PASS but not sent ({len(unsent)} recent) ===")
        seen: set[tuple[str, int]] = set()
        for row in unsent[-30:]:
            key = (row["vod"], int(row["time_sec"]))
            if key in seen:
                continue
            seen.add(key)
            print(
                f"  {row['vod']} @{row['time_sec']:.0f}s "
                f"seg={row.get('expected_segment_id')} ({row.get('reason','')[:40]})"
            )

    report: list[dict] = []
    total_missed = 0
    for vid, times in sorted(anchors.items()):
        if not times:
            continue
        row = audit_vod(vid, times, rejects, good_tol=args.good_tol)
        report.append(row)
        total_missed += row["unexplained_misses"]
        print(
            f"{vid}: good={row['good_anchors']} explained={row['explained_misses']} "
            f"missed={row['unexplained_misses']}"
        )
        for miss in row["missed"][:5]:
            print(f"  ? missed @{miss['time_sec']:.1f}s")
        for ex in row["explained"][:3]:
            print(f"  x reject @{ex.get('time_sec')} {ex.get('reason','')[:80]}")

    print(f"\nTOTAL unexplained missed anchors: {total_missed}")
    if args.train_missed and total_missed:
        added = train_missed_goods(report)
        print(f"Appended {added} audit anchors to {OWNER_PATH}")

    out_path = DATA_PUBG / "learning_audit_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"report": report, "total_missed": total_missed, "unsent_combat_passes": unsent[-50:]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
