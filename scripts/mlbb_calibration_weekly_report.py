#!/usr/bin/env python3
"""Weekly MLBB calibration report + eval gate when enough owner feedback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import ready_for_eval, stats
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
EVAL_REPORT = REPO / "data" / "training" / "mlbb_calibration_eval.json"


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def run_mlbb_vod_eval() -> dict:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_SOFT_ANCHOR", "1")
    os.environ.setdefault("CONTENT_BOT_REPO", str(REPO))
    from eval_owner_labels import eval_vod, load_videos

    videos = load_videos("mobile_legends")
    rows = videos.get("E4Dsp53yvv4", [])
    if not rows:
        return {"status": "no_labels"}
    return eval_vod("mobile_legends", "E4Dsp53yvv4", rows, good_tol=90.0, bad_tol=60.0)


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", "")
    s = stats()

    lines = [
        "📊 MLBB калибровка — недельный отчёт",
        f"👍 yes: {s['feedback_yes']} | 👎 no: {s['feedback_no']}",
        f"Согласие с моделью: {s['accuracy']:.0%} ({s['comparable']} оценок)",
        f"Exemplars: good={s['good_exemplars']} bad={s['bad_exemplars']}",
        f"Индекс Shorts: {s['index_total']}, ждут оценки: {s['pending']}",
    ]

    eval_row: dict = {"status": "skipped", "reason": "insufficient_feedback"}
    if ready_for_eval(min_yes=30, min_no=20):
        lines.append("")
        lines.append("🎯 Порог 30👍/20👎 достигнут — eval E4Dsp53yvv4:")
        eval_row = run_mlbb_vod_eval()
        if eval_row.get("status") == "ok":
            recall = float(eval_row.get("recall", 0))
            bad_hits = int(eval_row.get("bad_hits", 0))
            pass_gate = recall >= 0.70 and bad_hits == 0
            lines.append(
                f"recall@good={recall:.0%} ({eval_row.get('good_hit')}/{eval_row.get('good_total')})"
            )
            lines.append(f"bad overlap: {bad_hits}/{eval_row.get('bad_total')}")
            lines.append(f"PASS={'✅' if pass_gate else '❌'} (цель recall≥70%)")
            if eval_row.get("good_detail"):
                lines.append(f"good: {eval_row['good_detail']}")
            if bad_hits:
                lines.append(f"bad: {eval_row.get('bad_detail', '')}")
        else:
            lines.append(f"eval: {eval_row.get('status')}")
    else:
        need_yes = max(0, 30 - s["feedback_yes"])
        need_no = max(0, 20 - s["feedback_no"])
        lines.append(f"До eval: ещё 👍{need_yes} / 👎{need_no}")

    report = {"stats": s, "eval": eval_row}
    EVAL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    EVAL_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if token and chat_id:
        send_message(token, chat_id, "\n".join(lines))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
