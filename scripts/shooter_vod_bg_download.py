#!/usr/bin/env python3
"""Background next-VOD download for shooter feed while current VOD scans."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("shooter_vod_bg_download")


class ShooterVodBgDownloader:
    """Discover + download the next shooter VOD on a background thread."""

    def __init__(
        self,
        game: str,
        env: dict[str, str],
        *,
        discover_fn: Callable[[str, dict[str, str], set[str]], list[dict]],
        download_fn: Callable[[str, dict, dict[str, str]], Path | None],
    ):
        self.game = game
        self.env = env
        self._discover_fn = discover_fn
        self._download_fn = download_fn
        self._thread: threading.Thread | None = None
        self._ready: Path | None = None
        self._ready_pick: dict | None = None
        self._error: str | None = None
        self._running = False
        self._lock = threading.Lock()
        self._done = threading.Event()

    def enabled(self) -> bool:
        return self.env.get("SHOOTER_VOD_BG_DOWNLOAD", "1") == "1"

    def busy(self) -> bool:
        with self._lock:
            return self._running or self._ready is not None

    def start_if_idle(self, used: set[str]) -> None:
        if not self.enabled():
            return
        with self._lock:
            if self._running or self._ready is not None:
                return
            self._running = True
            self._done.clear()
            used_snapshot = set(used)
            self._thread = threading.Thread(
                target=self._worker,
                args=(used_snapshot,),
                daemon=True,
                name=f"shooter-bg-dl-{self.game}",
            )
            self._thread.start()

    def _worker(self, used: set[str]) -> None:
        path: Path | None = None
        pick: dict | None = None
        err = ""
        try:
            candidates = self._discover_fn(self.game, self.env, used)
            if not candidates:
                err = "discovery_empty"
            else:
                pick = None
                if self.game in ("pubg", "standoff"):
                    from youtube_shooter_vod_prefs import pick_discovery_candidate

                    pick = pick_discovery_candidate(self.game, candidates)
                if pick is None:
                    pick = candidates[0]
                path = self._download_fn(self.game, pick, self.env)
                if path is None:
                    err = "download_failed"
                else:
                    log.info("bg download ready game=%s vod=%s", self.game, path.name)
        except Exception as exc:
            err = str(exc)
            log.exception("shooter bg download failed game=%s", self.game)
        with self._lock:
            self._ready = path
            self._ready_pick = pick if path is not None else None
            self._error = err or None
            self._running = False
        self._done.set()

    def pop_ready(self) -> tuple[Path | None, dict | None]:
        with self._lock:
            ready = self._ready
            pick = self._ready_pick
            self._ready = None
            self._ready_pick = None
            return ready, pick

    def wait_ready(self, timeout: float) -> tuple[Path | None, dict | None]:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            ready, pick = self.pop_ready()
            if ready:
                return ready, pick
            with self._lock:
                alive = self._running
            if not alive:
                return self.pop_ready()
            time.sleep(min(5.0, deadline - time.time()))
        return None, None
