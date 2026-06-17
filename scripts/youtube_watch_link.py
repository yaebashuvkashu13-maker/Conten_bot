"""YouTube deep links that open at a specific second (youtu.be/ID?t=N)."""


def youtube_watch_url(video_id: str, start_sec: float = 0.0) -> str:
    vid = str(video_id or "").strip()
    if not vid:
        return ""
    if start_sec >= 1:
        return f"https://youtu.be/{vid}?t={int(start_sec)}"
    return f"https://youtu.be/{vid}"


def youtube_watch_url_from_row(row: dict) -> str:
    """Build watch link from calibration row fields (Shorts / rescued clips)."""
    vid = str(row.get("video_id") or row.get("id") or "").strip()
    start = 0.0
    for key in ("trim_start_sec", "clip_start_sec", "start"):
        val = row.get(key)
        if val is None:
            continue
        try:
            s = float(val)
        except (TypeError, ValueError):
            continue
        if s >= 0.5:
            start = s
            break
    return youtube_watch_url(vid, start)
