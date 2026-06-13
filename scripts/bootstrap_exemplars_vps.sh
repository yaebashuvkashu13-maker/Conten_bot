#!/usr/bin/env bash
# Bootstrap highlight exemplars from VPS datasets (hero_datasets, telegram uploads).
set -Eeuo pipefail

ROOT="${HIGHLIGHT_EXEMPLAR_ROOT:-/root/content_bot_ml/data/highlight_exemplars}"

mkdir -p "$ROOT/pubg/good" "$ROOT/pubg/bad" "$ROOT/mobile_legends/good" "$ROOT/mobile_legends/bad"

# PUBG: owner-label cuts + telegram examples from owner
python3 /usr/local/bin/highlight_bootstrap_exemplars.py --game pubg --vod yt_n97cHIR9Qow.mp4 || true
python3 /usr/local/bin/highlight_bootstrap_exemplars.py --game pubg --vod yt_pJ-X6NdSU9k.mp4 || true
python3 /usr/local/bin/highlight_bootstrap_panns_peaks.py --game pubg --vod yt_FpMs48XOnq0.mp4 --min-panns 0.35 --top 8 || true

i=0
while IFS= read -r f; do
  cp -n "$f" "$ROOT/pubg/good/tg_$(basename "$f")"
  i=$((i + 1))
  [[ $i -ge 10 ]] && break
done < <(find /root/telegram_uploads -name '*.mp4' -size +2M 2>/dev/null | head -10)

# MLBB: hero_datasets (owner TikTok gameplay examples)
i=0
while IFS= read -r f; do
  cp -n "$f" "$ROOT/mobile_legends/good/hero_$(basename "$f")"
  i=$((i + 1))
  [[ $i -ge 12 ]] && break
done < <(find /root/hero_datasets -name '*.mp4' 2>/dev/null | head -12)

# MLBB bad: promo/quarantine (non-gameplay)
i=0
while IFS= read -r f; do
  cp -n "$f" "$ROOT/mobile_legends/bad/promo_$(basename "$f")"
  i=$((i + 1))
  [[ $i -ge 6 ]] && break
done < <(find /root/data/mlbb/quarantine/promo /root/data/mlbb/quarantine/ad -name '*.mp4' 2>/dev/null | head -6)

echo "exemplars pubg good=$(ls "$ROOT/pubg/good" | wc -l) bad=$(ls "$ROOT/pubg/bad" | wc -l)"
echo "exemplars mlbb good=$(ls "$ROOT/mobile_legends/good" | wc -l) bad=$(ls "$ROOT/mobile_legends/bad" | wc -l)"
