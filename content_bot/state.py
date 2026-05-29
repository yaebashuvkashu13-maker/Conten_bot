from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"published_ids": []}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"published_ids": []}

    @property
    def published_ids(self) -> set[str]:
        return set(self.data.get("published_ids", []))

    def mark_published(self, post_id: str) -> None:
        published = self.published_ids
        published.add(post_id)
        self.data["published_ids"] = sorted(published)
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

