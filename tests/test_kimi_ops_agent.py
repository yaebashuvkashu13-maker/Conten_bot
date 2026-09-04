#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_kimi_brief_mentions_fight_only():
    from kimi_ops_agent import PUBG_SYSTEM_BRIEF

    assert "перестрел" in PUBG_SYSTEM_BRIEF.lower() or "перестрелки" in PUBG_SYSTEM_BRIEF
    assert "loot" in PUBG_SYSTEM_BRIEF.lower() or "лут" in PUBG_SYSTEM_BRIEF.lower()
    assert "REQUIRE_KILL_NOTIFICATION" in PUBG_SYSTEM_BRIEF


def test_kimi_missing_key_message(monkeypatch):
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    from kimi_ops_agent import chat

    # Empty env dict — do not load VPS secrets in unit test.
    msg = chat("статус", env={}, with_context=False)
    assert "MOONSHOT_API_KEY" in msg or "KIMI_API_KEY" in msg
