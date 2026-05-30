from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .collect_config import CollectConfig, load_collect_config
from .config import load_config
from .instagram_ingest import fetch_source_posts_with_options
from .proxy_config import resolve_proxy_url
from .tiktok_dataset import collect_profile


def _existing_video_ids(manifest_path: Path) -> set[str]:
    if not manifest_path.exists():
        return set()
    ids: set[str] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = record.get("video_id")
        if video_id:
            ids.add(str(video_id))
    return ids


def run_tiktok_bulk(config: CollectConfig, *, skip_existing: bool) -> int:
    if not config.proxy_url:
        print("WARNING: proxy_url is not set. TikTok/Instagram may block requests.")

    total_new = 0
    for index, profile in enumerate(config.tiktok_profiles, start=1):
        manifest_path = config.output_dir / f"{profile.label}_manifest.jsonl"
        before_ids = _existing_video_ids(manifest_path) if skip_existing else set()

        print(f"[{index}/{len(config.tiktok_profiles)}] {profile.label} ({profile.url})")
        records = collect_profile(
            profile_url=profile.url,
            output_dir=config.output_dir,
            proxy_url=config.proxy_url,
            max_entries=profile.max_entries,
            source_label=profile.label,
            download_media=config.download_media,
        )

        if skip_existing:
            new_records = [r for r in records if r.video_id not in before_ids]
        else:
            new_records = records
        total_new += len(new_records)
        print(f"  collected={len(records)}, new={len(new_records)}")

        if index < len(config.tiktok_profiles) and config.delay_between_profiles_seconds > 0:
            time.sleep(config.delay_between_profiles_seconds)

    return total_new


def run_instagram_snapshot(config: CollectConfig) -> int:
    if not config.instagram_config_path or not config.instagram_snapshot_dir:
        return 0

    app_config = load_config(config.instagram_config_path)
    proxy_url = config.proxy_url or app_config.proxy_url
    snapshot_dir = config.instagram_snapshot_dir
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for source in app_config.instagram_sources:
        out_path = snapshot_dir / f"{source.name}.jsonl"
        try:
            posts = fetch_source_posts_with_options(
                source,
                cookiefile=str(app_config.instagram_cookies_path)
                if app_config.instagram_cookies_path
                else None,
                proxy_url=proxy_url,
            )
        except Exception as exc:
            print(f"[instagram:{source.name}] fetch failed: {exc}")
            continue

        with out_path.open("w", encoding="utf-8") as handle:
            for post in posts:
                row = {
                    "post_id": post.post_id,
                    "source_name": post.source_name,
                    "permalink": post.permalink,
                    "caption": post.caption,
                    "media_url": post.media_url,
                    "thumbnail_url": post.thumbnail_url,
                    "view_count": post.view_count,
                    "like_count": post.like_count,
                    "uploader": post.uploader,
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        saved += len(posts)
        print(f"[instagram:{source.name}] saved {len(posts)} posts -> {out_path}")

    return saved


def run(config_path: str, *, tiktok: bool, instagram: bool, skip_existing: bool) -> int:
    config = load_collect_config(config_path)
    if tiktok:
        new_videos = run_tiktok_bulk(config, skip_existing=skip_existing)
        print(f"TikTok bulk done. new_records={new_videos}")
    if instagram:
        saved_posts = run_instagram_snapshot(config)
        print(f"Instagram snapshot done. saved_posts={saved_posts}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bulk-collect TikTok videos and/or snapshot Instagram via proxy."
    )
    parser.add_argument("--config", default="config.collect.yaml")
    parser.add_argument("--tiktok", action="store_true", help="Run TikTok profile bulk download.")
    parser.add_argument("--instagram", action="store_true", help="Snapshot Instagram metadata to JSONL.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run both TikTok and Instagram sections from config.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Report only newly seen TikTok video IDs (manifest still appended by collector).",
    )
    parser.add_argument("--proxy-url", help="Override proxy (else config / PROXY_URL env).")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.proxy_url:
        import os

        os.environ["PROXY_URL"] = args.proxy_url

    tiktok = args.tiktok or args.all
    instagram = args.instagram or args.all
    if not tiktok and not instagram:
        tiktok = True
        instagram = True

    if tiktok or instagram:
        proxy = resolve_proxy_url(args.proxy_url)
        if not proxy:
            print("WARNING: no proxy configured. Set proxy_url in config or PROXY_URL env.")

    return run(args.config, tiktok=tiktok, instagram=instagram, skip_existing=args.skip_existing)


if __name__ == "__main__":
    raise SystemExit(main())
