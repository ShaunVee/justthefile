"""Access policy.

One gate function, driven by ACCESS_MODE. Handlers call `is_allowed()` and never
branch on the mode themselves, so opening the bot to everyone later is an .env
edit rather than a code change.
"""

from __future__ import annotations

from .config import Config


def is_allowed(cfg: Config, user_id: int | None) -> bool:
    if user_id is None:  # channel posts and similar have no user
        return False
    if user_id in cfg.blocked_user_ids:
        return False
    if cfg.access_mode == "public":
        return True
    return user_id in cfg.allowed_user_ids


def chat_permitted(cfg: Config, chat_id: int | None) -> bool:
    """Whether a group chat may use the bot, whoever in it is posting.

    Orthogonal to is_allowed: a chat in ALLOWED_CHAT_IDS is open to everyone in
    it, which is the whole point of dropping the bot into a group of friends. A
    blocked user stays blocked there too, but that check needs both the chat and
    the user and so stays with the caller. Kept a separate function so the
    per-user gate above, and its tests, are untouched.
    """
    return chat_id is not None and chat_id in cfg.allowed_chat_ids
