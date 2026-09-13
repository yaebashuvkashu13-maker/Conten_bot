#!/bin/bash
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
python3 - <<'PY'
import json, base64, hashlib
from pathlib import Path
dir=Path('.')
manifest=json.loads((dir/'PARTS_MANIFEST.json').read_text())
for item in manifest:
  b64=''.join((dir/p).read_text().strip() for p in item['parts'])
  data=base64.b64decode(b64)
  assert hashlib.sha256(data).hexdigest()==item['sha256'], item['rel']
  dest=Path('restored')/item['rel']
  dest.parent.mkdir(parents=True, exist_ok=True)
  dest.write_bytes(data)
  print('ok', item['rel'], len(data))
print('All parts restored under ./restored/')
PY
