"""Mock server-only modules so tests run without full prod deps."""

from __future__ import annotations

import sys
from types import ModuleType


def _ensure(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod


_ensure("montage_env", strict_peak_env=lambda profile: {})
_ensure("preview_gate", validate_clips_before_preview=lambda *a, **k: (True, "ok", None, [{}], [{}]))
_ensure("strict_montage_direct", discover_strict_candidates=lambda *a, **k: [], file_sha256=lambda p: "sig")
_ensure("youtube_download", load_env=lambda p=None: {})
_ensure(
    "mlbb_learning_first",
    can_send=lambda n: (True, "ok"),
    record_send=lambda n: None,
    daily_send_count=lambda: 0,
    max_daily_sends=lambda: 500,
    precision_7d=lambda: 0.5,
    enabled=lambda: False,
    sends_allowed=lambda: True,
    dislike_feedback_report=lambda *a, **k: "",
    eval_transition_gate=lambda: {"all_pass": True, "holdout": {}, "dry_run": {}},
)
