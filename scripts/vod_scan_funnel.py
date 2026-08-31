#!/usr/bin/env python3
"""Per-VOD scan funnel counters and stage timings."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ScanFunnel:
    offsets_probed: int = 0
    dsp_pass: int = 0
    panns_pass: int = 0
    shortlist: int = 0
    snapped: int = 0
    picked: int = 0
    presend_pass: int = 0
    presend_fail: int = 0
    sent: int = 0
    panns_windows: int = 0
    cache_hits: int = 0
    feature_cache_hit: bool = False
    timings_ms: dict[str, float] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)

    _t0: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self._t0 <= 0:
            self._t0 = time.perf_counter()

    def mark(self, stage: str) -> None:
        self.timings_ms[stage] = round((time.perf_counter() - self._t0) * 1000.0, 1)

    def note_reject(self, reason: str) -> None:
        if reason and reason not in self.reject_reasons:
            self.reject_reasons.append(str(reason)[:120])

    def merge_timings(self, stats: dict[str, float | int]) -> None:
        for key in ("extract_ms", "dsp_ms", "panns_ms"):
            val = stats.get(key)
            if val is not None:
                self.timings_ms[key] = float(val)
        for key in ("offsets", "dsp_pass", "panns_windows", "cache_hits"):
            val = stats.get(key)
            if val is not None and key == "offsets":
                self.offsets_probed = int(val)
            elif val is not None and key == "dsp_pass":
                self.dsp_pass = int(val)
            elif val is not None and key == "panns_windows":
                self.panns_windows = int(val)
            elif val is not None and key == "cache_hits":
                self.cache_hits = int(val)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_t0", None)
        return d

    def summary(self) -> str:
        parts = [
            f"probe={self.offsets_probed}",
            f"dsp={self.dsp_pass}",
            f"panns={self.panns_pass or self.panns_windows}",
            f"pick={self.picked}",
            f"send={self.sent}",
        ]
        if self.feature_cache_hit:
            parts.append("feat_cache=1")
        if self.cache_hits:
            parts.append(f"pann_cache={self.cache_hits}")
        if self.presend_fail:
            parts.append(f"presend_fail={self.presend_fail}")
        return " ".join(parts)
