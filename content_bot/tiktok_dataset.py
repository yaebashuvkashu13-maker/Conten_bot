from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class TikTokVideoRecord:
    video_id: str
    webpage_url: str
    uploader: str | None
    description: str
    duration: float | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    repost_count: int | None
    upload_date: str | None
    extractor: str | None
    source_label: str


@dataclass(slots=True)
class TikTokSourceConfig:
    label: str
    profile_url: str
    max_entries: int


@dataclass(slots=True)
class TikTokDatasetConfig:
    output_dir: Path
    sources: list[TikTokSourceConfig]
    proxy_url: str | None
    proxy_url_env: str | None
    cookies_path: Path | None
    download_media: bool
    skip_existing_records: bool


def _load_existing_video_ids(output_path: Path) -> set[str]:
    video_ids: set[str] = set()
    for manifest_path in output_path.glob("*_manifest.jsonl"):
        with manifest_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                video_id = record.get("video_id")
                if video_id:
                    video_ids.add(str(video_id))
    return video_ids


def _resolve_proxy_url(config: TikTokDatasetConfig) -> str | None:
    if config.proxy_url:
        return config.proxy_url
    if config.proxy_url_env:
        return os.environ.get(config.proxy_url_env)
    return None


def load_dataset_config(path: str | Path) -> TikTokDatasetConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text()) or {}
    dataset_raw = raw.get("tiktok_dataset", raw)
    sources_raw = dataset_raw.get("sources")
    if not sources_raw:
        raise ValueError("TikTok dataset config must include at least one source.")

    default_max_entries = int(dataset_raw.get("max_entries_per_source", 50))
    sources = [
        TikTokSourceConfig(
            label=str(source.get("label") or source.get("name") or source["profile_url"]),
            profile_url=str(source["profile_url"]),
            max_entries=int(source.get("max_entries", default_max_entries)),
        )
        for source in sources_raw
    ]

    cookies_raw = dataset_raw.get("cookies_path")
    return TikTokDatasetConfig(
        output_dir=Path(dataset_raw.get("output_dir", "datasets/tiktok")),
        sources=sources,
        proxy_url=str(dataset_raw["proxy_url"]) if dataset_raw.get("proxy_url") else None,
        proxy_url_env=str(dataset_raw["proxy_url_env"]) if dataset_raw.get("proxy_url_env") else None,
        cookies_path=Path(cookies_raw) if cookies_raw else None,
        download_media=bool(dataset_raw.get("download_media", True)),
        skip_existing_records=bool(dataset_raw.get("skip_existing_records", True)),
    )


def _write_manifest_records(
    output_path: Path,
    source_label: str,
    records: list[TikTokVideoRecord],
    *,
    skip_existing_records: bool,
) -> list[TikTokVideoRecord]:
    manifest_path = output_path / f"{source_label}_manifest.jsonl"
    existing_ids = _load_existing_video_ids(output_path) if skip_existing_records else set()
    records_to_write = [record for record in records if record.video_id not in existing_ids]

    if records_to_write:
        with manifest_path.open("a", encoding="utf-8") as handle:
            for record in records_to_write:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    return records_to_write


def collect_profile(
    profile_url: str,
    output_dir: str | Path,
    proxy_url: str | None,
    max_entries: int,
    source_label: str,
    download_media: bool,
    cookiefile: str | None = None,
    skip_existing_records: bool = True,
) -> list[TikTokVideoRecord]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_entries,
        "proxy": proxy_url,
        "skip_download": not download_media,
        "outtmpl": str(output_path / "%(uploader)s" / "%(id)s.%(ext)s"),
        "continuedl": True,
        "fragment_retries": 10,
        "ignoreerrors": True,
        "overwrites": False,
        "retries": 10,
        "socket_timeout": 30,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    if cookiefile:
        options["cookiefile"] = cookiefile

    from yt_dlp import YoutubeDL

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(profile_url, download=download_media)

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        entries = [info]

    records: list[TikTokVideoRecord] = []
    for entry in entries:
        if not entry:
            continue
        record = TikTokVideoRecord(
            video_id=str(entry.get("id")),
            webpage_url=str(entry.get("webpage_url") or profile_url),
            uploader=entry.get("uploader"),
            description=str(entry.get("description") or entry.get("title") or "").strip(),
            duration=entry.get("duration"),
            view_count=entry.get("view_count"),
            like_count=entry.get("like_count"),
            comment_count=entry.get("comment_count"),
            repost_count=entry.get("repost_count"),
            upload_date=entry.get("upload_date"),
            extractor=entry.get("extractor_key") or entry.get("extractor"),
            source_label=source_label,
        )
        records.append(record)

    _write_manifest_records(
        output_path,
        source_label,
        records,
        skip_existing_records=skip_existing_records,
    )

    return records


def collect_from_config(config_path: str | Path) -> list[TikTokVideoRecord]:
    config = load_dataset_config(config_path)
    proxy_url = _resolve_proxy_url(config)
    all_records: list[TikTokVideoRecord] = []

    for source in config.sources:
        records = collect_profile(
            profile_url=source.profile_url,
            output_dir=config.output_dir,
            proxy_url=proxy_url,
            max_entries=source.max_entries,
            source_label=source.label,
            download_media=config.download_media,
            cookiefile=str(config.cookies_path) if config.cookies_path else None,
            skip_existing_records=config.skip_existing_records,
        )
        all_records.extend(records)

    return all_records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect TikTok profile data into a dataset.")
    parser.add_argument("--config", help="YAML config for collecting multiple TikTok sources.")
    parser.add_argument("--profile-url")
    parser.add_argument("--output-dir", default="datasets/tiktok")
    parser.add_argument("--proxy-url")
    parser.add_argument("--proxy-url-env", help="Environment variable that contains the proxy URL.")
    parser.add_argument("--cookies-path")
    parser.add_argument("--max-entries", type=int, default=20)
    parser.add_argument("--label", default="tiktok-source")
    parser.add_argument("--download-media", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.config:
        records = collect_from_config(args.config)
        print(f"Collected {len(records)} videos from config.")
        return 0

    if not args.profile_url:
        raise SystemExit("--profile-url is required unless --config is provided.")

    proxy_url = args.proxy_url or (os.environ.get(args.proxy_url_env) if args.proxy_url_env else None)
    records = collect_profile(
        profile_url=args.profile_url,
        output_dir=args.output_dir,
        proxy_url=proxy_url,
        max_entries=args.max_entries,
        source_label=args.label,
        download_media=args.download_media,
        cookiefile=args.cookies_path,
    )
    print(f"Collected {len(records)} videos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

