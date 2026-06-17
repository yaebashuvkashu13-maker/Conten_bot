"""Late-action title patterns."""

from __future__ import annotations

import re

NEGATIVE_TITLE = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|official\s+event|allstar|collab|cctv|"
    r"tutorial|guide|tips|funny|meme|intro|reaction|dance|tiktok|"
    r"rank\s+push\s+only|lobby|menu|event|login|diamond|free\s+skin|"
    r"sound\s*effect|sfx\b|notification\s*sound|ringtone|audio\s*only|"
    r"kill\s*sound|voice\s*line|ost\b|music\s*only|wallpaper|thumbnail|"
    r"hero\s*(reveal|showcase|preview|intro|spawn|appearance)|skin\s*reveal|"
    r"review\s+skin|skin\s+review|kualitas.*skin|skin\s+epic\s+terbaru|"
    r"siapa\s+yang\s+menang|who\s+wins|quien\s+gana|menang\s*\?|"
    r"which\s+hero|hero\s+vs|vs\s+.*\?|poll|vote\s+for|"
    r"character\s*preview|cinematic|new\s*hero|spawn\s*preview|hero\s*spawn)",
    re.I,
)


def test_poll_title_blocked() -> None:
    title = "Siapa yang menang❓️CHANG'E❓️ #mobilelegends #mlbb"
    assert NEGATIVE_TITLE.search(title)


def test_normal_gameplay_title_passes() -> None:
    title = "MLBB savage maniac ranked mythic teamfight"
    assert not NEGATIVE_TITLE.search(title)
