#!/bin/bash
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-content_bot_learning_20260913.tar.gz}
rm -f "$OUT" "$OUT.partial"
for p in "$DIR"/content_bot_learning_20260913.tar.gz.part*.b64.txt; do
  base64 -d "$p" >> "$OUT.partial"
done
mv "$OUT.partial" "$OUT"
echo "Wrote $OUT ($(wc -c < "$OUT") bytes)"
sha256sum "$OUT"
echo "Expected: 6cc768410cd7f2a5d1a7a4ad87aeab8d8a3ec96de0b923b47e1e145b6f464f04"
