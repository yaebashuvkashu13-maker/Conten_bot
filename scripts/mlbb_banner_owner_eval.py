#!/usr/bin/env python3
"""Eval kill-banner matcher on owner Telegram labels (ground truth)."""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path


def main() -> int:
    # Prefer VPS runtime env so OWNER_MIN_SIM / LOGIT_THR match the live bot.
    env_path = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    os.environ.setdefault("CONTENT_BOT_REPO", str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault(
        "MLBB_BANNER_REF_ROOT",
        str(Path(os.environ["CONTENT_BOT_REPO"]) / "data" / "mlbb_kill_banners"),
    )
    labels_path = Path(
        os.environ.get(
            "MLBB_BANNER_CALIB_LABELS",
            "/root/data/mlbb/banner_calibration_labels.json",
        )
    )
    if not labels_path.exists():
        print(json.dumps({"ok": False, "error": f"missing {labels_path}"}))
        return 1

    import cv2

    from mlbb_banner_ref_match import (
        banner_structure_score,
        clear_banner_ref_cache,
        match_banner_reference,
        owner_logit_score,
        _load_logit_model,
    )
    from mlbb_kill_banner import _classify_frame

    clear_banner_ref_cache()
    labels = list(json.loads(labels_path.read_text(encoding="utf-8")).get("labels") or [])
    want = ("own_kill_good", "double_triple", "no_banner", "enemy_kill", "not_kill")
    tot: Counter[str] = Counter()
    hit: Counter[str] = Counter()
    clf: Counter[str] = Counter()
    logit_pos: Counter[str] = Counter()
    t0 = time.time()
    model = _load_logit_model()
    thr = float(os.environ.get("MLBB_BANNER_LOGIT_THR", str(model[1] if model else 0.45)))

    for row in labels:
        if not isinstance(row, dict):
            continue
        reason = str(row.get("reason") or "")
        if reason not in want:
            continue
        shot = Path(str(row.get("screenshot") or ""))
        if not shot.exists():
            continue
        img = cv2.imread(str(shot))
        if img is None:
            continue
        tot[reason] += 1
        m = match_banner_reference(img)
        if m is not None:
            hit[reason] += 1
        c = _classify_frame(0.0, img, deep=True)
        if c is not None and c.source == "ref":
            clf[reason] += 1
        p = owner_logit_score(img)
        if p is not None and p >= thr:
            logit_pos[reason] += 1

    goods = tot["own_kill_good"] + tot["double_triple"]
    goods_hit = hit["own_kill_good"] + hit["double_triple"]
    goods_clf = clf["own_kill_good"] + clf["double_triple"]
    neg_n = tot["no_banner"] + tot["enemy_kill"] + tot["not_kill"]
    neg_fp = hit["no_banner"] + hit["enemy_kill"] + hit["not_kill"]
    report = {
        "ok": True,
        "elapsed_sec": round(time.time() - t0, 1),
        "logit_thr": thr,
        "match": {r: f"{hit[r]}/{tot[r]}" for r in want if tot[r]},
        "classify_ref": {r: f"{clf[r]}/{tot[r]}" for r in want if tot[r]},
        "logit_ge_thr": {r: f"{logit_pos[r]}/{tot[r]}" for r in want if tot[r]},
        "goods_recall_match": round(goods_hit / max(1, goods), 3),
        "goods_recall_classify": round(goods_clf / max(1, goods), 3),
        "neg_fp_rate_match": round(neg_fp / max(1, neg_n), 3),
        "struct_sample": {},
    }
    # Quick structure sanity on first few of each.
    for reason in ("own_kill_good", "no_banner"):
        scores = []
        for row in labels:
            if str(row.get("reason") or "") != reason:
                continue
            p = Path(str(row.get("screenshot") or ""))
            if not p.exists():
                continue
            img = cv2.imread(str(p))
            if img is None:
                continue
            scores.append(banner_structure_score(img))
            if len(scores) >= 20:
                break
        if scores:
            report["struct_sample"][reason] = {
                "mean": round(sum(scores) / len(scores), 3),
                "n": len(scores),
            }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
