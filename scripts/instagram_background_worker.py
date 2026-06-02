#!/usr/bin/env python3
"""Legacy hook: run real Instagram digest (replaces no-op stub)."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    script = "/usr/local/bin/instagram_digest_run.sh"
    return subprocess.run(["bash", script], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
