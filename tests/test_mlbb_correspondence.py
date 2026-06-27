"""MLBB search correspondence — title must match MLBB query intent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_correspondence import corresponds_to_mlbb_search


def test_football_conflicts_mlbb_query() -> None:
    ok, reason = corresponds_to_mlbb_search(
        title="Penn State Football hype video",
        search_query="mlbb 2026 savage shorts",
    )
    assert not ok
    assert reason == "domain_conflict"


def test_hmmm_no_correspondence_to_mlbb_query() -> None:
    ok, reason = corresponds_to_mlbb_search(
        title="Hmmm!! #shorts #shortlive",
        search_query="mlbb 2026 savage shorts",
    )
    assert not ok
    assert reason == "no_correspondence"


def test_chou_matches_mlbb_query() -> None:
    ok, reason = corresponds_to_mlbb_search(
        title="Chou savage wipe #mlbb",
        search_query="mlbb chou savage shorts",
    )
    assert ok
    assert reason in ("mlbb_domain", "query_overlap:chou", "query_overlap:mlbb", "query_overlap:savage")


def test_mlbb_in_title_without_query() -> None:
    ok, reason = corresponds_to_mlbb_search(title="Insane MLBB teamfight", search_query="")
    assert ok
    assert reason == "mlbb_domain"
