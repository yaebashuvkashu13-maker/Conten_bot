# DATA_STATUS — where learning bytes actually are (2026-09-13)

## On GitHub `main` now

- Full handoff docs: `archive/2026-09-13/*.md` (PROJECT_HANDOFF, RESTORE, ARCHITECTURE, LEARNING_DATA, OWNER_PREFERENCES, INCIDENT_LOG, CODE_SYNC)
- Adaptive thresholds: `data/vod_adaptive_thresholds/*`
- `learning_parts/PARTS_MANIFEST.json` + `REASSEMBLE_PARTS.sh` + README
- `PAUSE_HANDOFF.md` at repo root
- `CODE_HEAD_SHA.txt` / `TARBALL.sha256`

## Complete learning + code archive (MUST copy off VPS before expiry)

Server paths (root@95.182.99.85):

| Artifact | Path | Notes |
|----------|------|-------|
| Learning tar.gz | `/root/backups/content_bot_learning_20260913.tar.gz` | sha256 `6cc768410cd7f2a5d1a7a4ad87aeab8d8a3ec96de0b923b47e1e145b6f464f04` (~1.7M) |
| Expanded archive | `/root/backups/content_bot_archive_20260913/` | docs+data+systemd+code_snapshot |
| Git commit with full tree | `f85dfd3b17b99bfa86d0adf1bd8e58c90896177d` on branch `cursor/pubg-cut-quality-probe-cap-a016` | **not pushed** (no server git creds) |
| Git bundle (8 commits tip) | `/root/backups/content_bot_pause_20260913.bundle` (~11M) | `git clone`/`git pull` from bundle |
| Handoff extra | `/root/backups/handoff_extra.tgz` | docs+parts |

## Why not all binaries on GitHub yet

- Server `git push` fails: no GitHub username/password/PAT on VPS
- Box `gh` not logged in; no local PAT file
- GitHub MCP `push_files` works for text docs but 100+ base64 parts (~3.5M) need many sequential API commits; parts are prepared under `learning_parts/` on the VPS for continued upload

## Owner action BEFORE VPS dies

```bash
# from your laptop
scp root@95.182.99.85:/root/backups/content_bot_learning_20260913.tar.gz .
scp root@95.182.99.85:/root/backups/content_bot_pause_20260913.bundle .
scp -r root@95.182.99.85:/root/backups/content_bot_archive_20260913 .
# optional: upload tar.gz as GitHub Release asset after `gh auth login`
gh release create pause-2026-09-13 content_bot_learning_20260913.tar.gz --repo yaebashuvkashu13-maker/Conten_bot
```

Then continue MCP/part uploads or `git push` once a PAT is available.
