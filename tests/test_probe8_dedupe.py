"""Near-dup collapse for overlapping OK parents in 8s probe batch."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pubg_owner_probe8_batch.py"
    spec = importlib.util.spec_from_file_location("probe8_batch", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_dedupe_drops_1_2s_offsets_same_vod():
    mod = _load()
    raw = [
        {"vid": "abc", "abs_start": 562.0, "sid": "abc_562_p8", "parent": "a"},
        {"vid": "abc", "abs_start": 564.0, "sid": "abc_564_p8", "parent": "b"},
        {"vid": "abc", "abs_start": 570.0, "sid": "abc_570_p8", "parent": "a"},
        {"vid": "xyz", "abs_start": 100.0, "sid": "xyz_100_p8", "parent": "c"},
        {"vid": "xyz", "abs_start": 101.0, "sid": "xyz_101_p8", "parent": "d"},
    ]
    kept = mod.dedupe_probes(raw, set())
    sids = [p["sid"] for p in kept]
    assert sids == ["abc_562_p8", "abc_570_p8", "xyz_100_p8"]


def test_dedupe_respects_already_sent_near_dup():
    mod = _load()
    raw = [
        {"vid": "abc", "abs_start": 564.0, "sid": "abc_564_p8", "parent": "b"},
        {"vid": "abc", "abs_start": 572.0, "sid": "abc_572_p8", "parent": "a"},
    ]
    kept = mod.dedupe_probes(raw, {"abc_562_p8"})
    sids = [p["sid"] for p in kept]
    assert sids == ["abc_572_p8"]


def test_parse_probe_start():
    mod = _load()
    assert mod.parse_probe_start("1tGY_WQoj9c_562_p8") == ("1tGY_WQoj9c", 562.0)
    assert mod.parse_probe_start("nope") is None
