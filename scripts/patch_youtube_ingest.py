from pathlib import Path

p = Path("/root/content_bot_ml/scripts/mlbb_youtube_shorts_ingest.py")
text = p.read_text(encoding="utf-8")

old_queries = """SEARCH_QUERIES = (
    "mlbb teamfight shorts",
    "mlbb savage shorts",
    "mobile legends highlights shorts",
)

NEGATIVE_TITLE = re.compile(
    r"(#ad\\b|sponsored|giveaway|promo\\b|free\\s+diamond|skin\\s+gratis|"
    r"log\\s*in\\s+mlbb|mailbox|official\\s+event|tutorial|guide|tips|"
    r"funny|meme|intro|reaction|rank\\s+push\\s+only|lobby|menu)",
    re.I,
)
"""

new_block = """SEARCH_QUERIES = (
    "mlbb ranked gameplay savage",
    "mobile legends streamer ranked teamfight",
    "mlbb solo rank maniac gameplay",
    "mlbb live gameplay highlights",
)

STREAMER_SHORTS_FEEDS = (
    "https://www.youtube.com/@Betosky/shorts",
    "https://www.youtube.com/@JessNoLimit/shorts",
    "https://www.youtube.com/@Insectos/shorts",
    "https://www.youtube.com/@akosidogie/shorts",
    "https://www.youtube.com/@Elginnn/shorts",
    "https://www.youtube.com/@Wise_/shorts",
    "https://www.youtube.com/@OhMyV33nus/shorts",
    "https://www.youtube.com/@Kairi/shorts",
)

NEGATIVE_TITLE = re.compile(
    r"(#ad\\b|sponsored|giveaway|promo\\b|free\\s+diamond|skin\\s+gratis|"
    r"log\\s*in\\s+mlbb|mailbox|official\\s+event|allstar|collab|cctv|"
    r"tutorial|guide|tips|funny|meme|intro|reaction|dance|tiktok|"
    r"rank\\s+push\\s+only|lobby|menu|event|login|diamond|free\\s+skin)",
    re.I,
)
"""

if "STREAMER_SHORTS_FEEDS" not in text:
    if old_queries not in text:
        raise SystemExit("query block not found")
    text = text.replace(old_queries, new_block, 1)
    print("queries/feeds updated")

channel_fn = '''

def fetch_streamer_shorts(channel_url: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        channel_url,
        "--flat-playlist",
        "--playlistend",
        str(max(limit * 3, 40)),
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--print",
        "%(id)s\\t%(title)s\\t%(view_count)s\\t%(duration)s\\t%(upload_date)s\\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=240, env=subprocess_env_no_proxy(env)
    )
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\\t")
        if len(parts) < 6:
            continue
        vid, title, views, dur, upload_date, url = parts[:6]
        if not vid or len(vid) != 11:
            continue
        if upload_date and upload_date not in ("NA", "N/A") and upload_date.isdigit() and upload_date < cutoff:
            continue
        try:
            duration = float(dur or 0)
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            continue
        if duration <= 3 or duration > 60:
            continue
        if NEGATIVE_TITLE.search(title):
            continue
        entries.append(
            {
                "video_id": vid,
                "title": title[:240],
                "view_count": view_count,
                "duration": duration,
                "upload_date": upload_date,
                "url": url or f"https://www.youtube.com/shorts/{vid}",
                "search_query": channel_url,
                "source_type": "streamer_channel",
            }
        )
        if len(entries) >= limit:
            break
    return entries


'''

marker = "def search_shorts(query: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:"
if "fetch_streamer_shorts" not in text:
    text = text.replace(marker, channel_fn + marker, 1)
    print("fetch_streamer_shorts added")

old_pool = """    seen: set[str] = set()
    pool: list[dict] = []
    for query in queries:
        for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0 and len(queries) > 1:
            time.sleep(args.search_delay)
"""

new_pool = """    seen: set[str] = set()
    pool: list[dict] = []
    channel_feeds = list(STREAMER_SHORTS_FEEDS)
    if args.incremental and channel_feeds:
        slot = int(time.time() // 7200) % len(channel_feeds)
        channel_feeds = [channel_feeds[slot]]
        print(f"incremental channel={channel_feeds[0]}")
    for channel_url in channel_feeds:
        for row in fetch_streamer_shorts(
            channel_url, limit=args.max_per_query, env=env, days=args.days
        ):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0:
            time.sleep(args.search_delay)
    for query in queries:
        for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0 and len(queries) > 1:
            time.sleep(args.search_delay)
"""

if "incremental channel=" not in text:
    text = text.replace(old_pool, new_pool, 1)
    print("pool collection updated")

old_score = """    gate = bool(m.rule_pass and m.visual_pass)
    combined = combat + (0.15 if gate else 0.0)
    return {
        "score": round(combined, 4),
"""

new_score = """    kill_score = 0.0
    kill_pass = 0
    kill_reason = ""
    try:
        from mlbb_kill_ui import score_mlbb_kill_ui

        kill = score_mlbb_kill_ui(path, 0.15, window, sample_frames=6)
        kill_score = float(kill.score)
        kill_pass = int(kill.has_kill_notification)
        kill_reason = kill.reason
    except ImportError:
        pass
    gate = bool(m.rule_pass and m.visual_pass)
    combined = combat + (0.15 if gate else 0.0) + kill_score * 0.35
    return {
        "score": round(combined, 4),
        "kill_ui_score": round(kill_score, 4),
        "kill_ui_pass": kill_pass,
        "kill_ui_reason": kill_reason,
"""

if "kill_ui_score" not in text:
    text = text.replace(old_score, new_score, 1)
    print("score_clip updated")

p.write_text(text, encoding="utf-8")
print("done")
