#!/bin/bash
# Reassemble learning tarball from parts
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-content_bot_learning_20260913.tar.gz}
rm -f "$OUT"
for p in "$DIR"/content_bot_learning_20260913.tar.gz.part*; do
  case "$p" in *.b64.txt) continue;; esac
  cat "$p" >> "$OUT"
done
echo "Wrote $OUT ($(wc -c < "$OUT") bytes)"
sha256sum "$OUT"
