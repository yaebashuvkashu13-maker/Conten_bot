#!/usr/bin/env bash
# Pre-deploy safety: clean checkout, no diverged history, deployment lock.
set -euo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
LOCK_FILE="${VOD_DEPLOY_LOCK_FILE:-/root/data/pubg/.deploy.lock}"
REMOTE="${VOD_DEPLOY_REMOTE:-origin}"
BRANCH="${VOD_DEPLOY_BRANCH:-cursor/vod-pipeline-p0-p1-a016}"
EXPECTED="${VOD_DEPLOY_COMMIT:-}"

cd "$REPO"

fail() {
  echo "deploy-check FAIL: $*" >&2
  exit 1
}

if [[ -f "$LOCK_FILE" ]]; then
  fail "deployment lock present at $LOCK_FILE"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "dirty files:" >&2
  git status --short >&2
  fail "working tree is dirty — move runtime labels to /root/data/pubg"
fi

git fetch "$REMOTE" "$BRANCH" >/dev/null 2>&1 || fail "cannot fetch $REMOTE/$BRANCH"

LOCAL="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse "$REMOTE/$BRANCH" 2>/dev/null || git rev-parse "origin/$BRANCH")"

if [[ -n "$EXPECTED" && "$LOCAL" != "$EXPECTED" ]]; then
  fail "HEAD $LOCAL != expected $EXPECTED"
fi

# On intentional branch switch deploy, local may differ from old remote tracking branch.
if [[ "$LOCAL" != "$REMOTE_HEAD" ]]; then
  if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE_HEAD" 2>/dev/null; then
    if ! git merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL" 2>/dev/null; then
      fail "history diverged local=$LOCAL remote=$REMOTE_HEAD"
    fi
  fi
fi

# Runtime labels must not live in repo checkout
for f in data/pubg_owner_labels.json data/pubg/pubg_owner_labels.json; do
  if [[ -f "$f" ]] && git status --porcelain "$f" | grep -q .; then
    fail "runtime label file dirty in checkout: $f"
  fi
done

echo "deploy-check OK repo=$REPO branch=$BRANCH head=$LOCAL remote=$REMOTE_HEAD"
