from __future__ import annotations

import os


def resolve_proxy_url(explicit: str | None = None) -> str | None:
    """Proxy from CLI/config, else PROXY_URL / HTTPS_PROXY / HTTP_PROXY."""
    if explicit:
        value = explicit.strip()
        return value or None
    for key in ("PROXY_URL", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None
