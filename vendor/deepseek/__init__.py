"""Unofficial client for chat.deepseek.com (vendored, MIT).

Local patch: exports are resolved lazily so that importing the package does not
pull in Playwright. See vendor/NOTICE.md.
"""

from __future__ import annotations

__all__ = ["Session", "get_session", "login", "DeepSeekClient", "Reply", "DeepSeekPow"]


def __getattr__(name: str):
    if name in ("Session", "get_session", "login"):
        from . import auth

        return getattr(auth, name)
    if name in ("DeepSeekClient", "Reply"):
        from . import client

        return getattr(client, name)
    if name == "DeepSeekPow":
        from .pow import DeepSeekPow

        return DeepSeekPow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
