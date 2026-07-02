"""VK callback webhook security."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_health_hides_group_id() -> None:
    from vk_callback_webhook import VkCallbackHandler

    class FakeHandler(VkCallbackHandler):
        path = "/health"

        def __init__(self):
            self.path = "/health"

        def _write(self, code, body, content_type="text/plain"):  # type: ignore[no-untyped-def]
            FakeHandler.last = (code, body)

    env = {
        "VK_MLBB_GROUP_ID": "12345",
        "VK_MLBB_CONFIRMATION": "abc",
        "VK_CALLBACK_SECRET": "sec",
        "VK_MLBB_ACCESS_TOKEN": "tok",
    }
    h = FakeHandler()
    with patch("vk_callback_webhook.load_env", return_value=env):
        h.do_GET()
    code, body = FakeHandler.last
    assert code == 200
    data = json.loads(body)
    assert "group_id" not in data


def test_cfg_requires_secret() -> None:
    from vk_callback_webhook import _cfg

    with pytest.raises(SystemExit):
        _cfg({"VK_MLBB_GROUP_ID": "1", "VK_MLBB_CONFIRMATION": "c"})
