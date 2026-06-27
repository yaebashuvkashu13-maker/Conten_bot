from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from yt_dlp import YoutubeDL


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


def collect_profile(
    profile_url: str,
    output_dir: str | Path,
    proxy_url: str | None,
    max_entries: int,
    source_label: str,
    download_media: bool,
) -> list[TikTokVideoRecord]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    options = {
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_entries,
        "proxy": proxy_url,
        "skip_download": not download_media,
        "outtmpl": str(output_path / "%(uploader)s" / "%(id)s.%(ext)s"),
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

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

    manifest_path = output_path / f"{source_label}_manifest.jsonl"
    seen_ids: set[str] = set()
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                vid = str(row.get("video_id") or "")
                if vid:
                    seen_ids.add(vid)
            except json.JSONDecodeError:
                continue

    new_records = [r for r in records if r.video_id not in seen_ids]
    if new_records:
        with manifest_path.open("a", encoding="utf-8") as handle:
            for record in new_records:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect TikTok profile data into a dataset.")
    parser.add_argument("--profile-url", required=True)
    parser.add_argument("--output-dir", default="datasets/tiktok")
    parser.add_argument("--proxy-url")
    parser.add_argument("--max-entries", type=int, default=20)
    parser.add_argument("--label", default="tiktok-source")
    parser.add_argument("--download-media", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    records = collect_profile(
        profile_url=args.profile_url,
        output_dir=args.output_dir,
        proxy_url=args.proxy_url,
        max_entries=args.max_entries,
        source_label=args.label,
        download_media=args.download_media,
    )
    print(f"Collected {len(records)} videos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

