from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class StateStore:
    """Published post IDs with atomic writes and corrupt-json recovery."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.recovery_path = self.path.with_suffix(self.path.suffix + ".recovery.jsonl")
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.data = self._load()
        self._merge_recovery_journal()

    def _empty(self) -> dict:
        return {"published_ids": []}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data.get("published_ids"), list):
                raise json.JSONDecodeError("invalid published_ids", "", 0)
            return data
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.error("state corrupt %s: %s", self.path, exc)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            corrupt = self.path.with_suffix(f"{self.path.suffix}.corrupt.{stamp}")
            try:
                self.path.rename(corrupt)
                logger.warning("moved corrupt state to %s", corrupt)
            except OSError:
                pass
            if self.backup_path.exists():
                try:
                    data = json.loads(self.backup_path.read_text(encoding="utf-8"))
                    logger.warning("restored state from backup %s", self.backup_path)
                    return data
                except (json.JSONDecodeError, OSError):
                    pass
            return self._empty()

    def _merge_recovery_journal(self) -> None:
        if not self.recovery_path.exists():
            return
        merged = 0
        for line in self.recovery_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                post_id = str(row.get("post_id") or "")
            except json.JSONDecodeError:
                continue
            if post_id and post_id not in self.published_ids:
                self.data.setdefault("published_ids", []).append(post_id)
                merged += 1
        if merged:
            self.data["published_ids"] = sorted(set(self.data["published_ids"]))
            self._save()
            logger.warning("merged %s ids from recovery journal", merged)

    @property
    def published_ids(self) -> set[str]:
        return set(self.data.get("published_ids", []))

    def mark_published(self, post_id: str) -> None:
        published = self.published_ids
        published.add(post_id)
        self.data["published_ids"] = sorted(published)
        self._save()

    def record_recovery(self, post_id: str, *, reason: str = "save_failed") -> None:
        """Post was sent to Telegram but durable state write failed — avoid re-send."""
        row = {"post_id": post_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
        self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
        with self.recovery_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.error("recovery journal: post_id=%s reason=%s", post_id, reason)

    def _save(self, retries: int = 3) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self.path.replace(self.backup_path)
            except OSError:
                pass
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp.{os.getpid()}")
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                last_exc = exc
                logger.warning("state save attempt %s/%s failed: %s", attempt, retries, exc)
                time.sleep(0.2 * attempt)
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"state save failed after {retries} tries: {last_exc}")
