
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_kill_notification import _box_looks_like_kill_banner  # noqa: E402


def test_rejects_bottom_inventory_strip() -> None:
    # Wg9@1320 false onset: y0=0.807 tiny strip
    assert _box_looks_like_kill_banner([0.538, 0.807, 0.0595, 0.0127]) is False


def test_accepts_midframe_kill_banner() -> None:
    assert _box_looks_like_kill_banner([0.25, 0.42, 0.50, 0.08]) is True
