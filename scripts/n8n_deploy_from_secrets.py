#!/usr/bin/env python3
"""Deploy MLBB workflow to n8n via REST API (needs N8N_API_KEY, not login/password)."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

WORKFLOW_JSON = Path(__file__).resolve().parent.parent / "workflows" / "n8n_mlbb_nightly_pipeline.json"


def api_request(base: str, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-N8N-API-KEY": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def main() -> int:
    base = os.environ.get("N8N_BASE_URL", "").strip()
    api_key = os.environ.get("N8N_API_KEY", "").strip()
    if not base or not api_key:
        print(
            "Need N8N_BASE_URL and N8N_API_KEY in secrets.\n"
            "Login/password do not work for n8n REST API (only X-N8N-API-KEY).\n"
            "n8n Cloud: API after paid plan → Settings → n8n API.\n"
            "Without API: import workflows/n8n_mlbb_nightly_pipeline.json manually.",
            file=sys.stderr,
        )
        return 1
    if not WORKFLOW_JSON.exists():
        print(f"missing {WORKFLOW_JSON}", file=sys.stderr)
        return 1
    workflow = json.loads(WORKFLOW_JSON.read_text(encoding="utf-8"))
    workflow["name"] = workflow.get("name", "MLBB Night Pipeline")
    workflow.pop("id", None)
    workflow.pop("versionId", None)
    try:
        created = api_request(base, api_key, "POST", "/api/v1/workflows", workflow)
        wf_id = created.get("id")
        if wf_id:
            api_request(base, api_key, "POST", f"/api/v1/workflows/{wf_id}/activate")
            print(f"ok workflow id={wf_id} activated")
        else:
            print(json.dumps(created, indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
