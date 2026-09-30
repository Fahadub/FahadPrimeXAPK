"""Arabic / English for the messages the PC sends back to the phone.

The phone sends its interface language in the ``X-Lang`` header; it is kept per request thread.
Background work (games, screen watching, background commands) copies it into its own thread.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

_local = threading.local()


def set_lang(code: str | None) -> None:
    _local.lang = "en" if str(code or "").lower().startswith("en") else "ar"


def lang() -> str:
    return getattr(_local, "lang", "ar")


def is_en() -> bool:
    return lang() == "en"


def t(ar: str, en: str) -> str:
    """Pick the text for the current language."""
    return en if is_en() else ar


def language_name() -> str:
    return "English" if is_en() else "Arabic"


def bound(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so that it runs with the caller's language (for worker threads)."""
    code = lang()

    def run(*args: Any, **kwargs: Any) -> Any:
        set_lang(code)
        return fn(*args, **kwargs)

    return run
