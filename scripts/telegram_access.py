#!/usr/bin/env python3
"""Telegram chat access control (deny-by-default)."""

from __future__ import annotations


def is_owner(chat_id: str, owner_chat_id: str, extra_owner_ids: str = "") -> bool:
    cid = str(chat_id)
    owners = {str(owner_chat_id)} if owner_chat_id else set()
    for item in extra_owner_ids.split(","):
        item = item.strip()
        if item:
            owners.add(item)
    return cid in owners


def chat_is_allowed(
    chat_id: str,
    *,
    owner_chat_id: str,
    allowed_chat_ids: set[str],
    extra_owner_ids: str = "",
) -> bool:
    """Owner always allowed; others only if listed in allowed_chat_ids."""
    cid = str(chat_id)
    if is_owner(cid, owner_chat_id, extra_owner_ids):
        return True
    if not allowed_chat_ids:
        return False
    return cid in allowed_chat_ids
