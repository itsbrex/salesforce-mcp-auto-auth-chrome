"""Shared data types and the cookie-reader interface.

This is a leaf module (standard library only) so every browser family's reader
can depend on it without an import cycle back to the registry in ``browsers.py``.

``BrowserConfig`` and ``CookieSource`` describe *where* a cookie store lives;
``CookieReader`` is the interface each family's reader implements — the single
seam callers dispatch through instead of switching on a ``family`` tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = ["BrowserConfig", "CookieSource", "CookieReader"]


@dataclass(frozen=True)
class BrowserConfig:
    """Static description of a browser's cookie storage on macOS.

    Args:
        key: Lowercase identifier, e.g. ``"chrome"``.
        family: One of ``"chromium"``, ``"firefox"``, ``"safari"`` — selects
            the reader used for this browser's cookie store.
        base_dir: The browser's data directory (profiles live under it for
            chromium/firefox; for safari this is the Cookies directory).
        keychain_service: macOS Keychain service holding the Safe Storage key
            (chromium only; ``None`` for firefox/safari).
        keychain_account: macOS Keychain account for the Safe Storage key.
    """

    key: str
    family: str
    base_dir: Path
    keychain_service: str | None = None
    keychain_account: str | None = None


@dataclass(frozen=True)
class CookieSource:
    """A single resolved cookie store (one browser profile)."""

    browser: str
    profile: str
    family: str
    path: Path
    keychain_service: str | None
    keychain_account: str | None


class CookieReader(Protocol):
    """Adapter over one browser family's cookie store.

    A reader owns everything its family knows how to do: enumerate its profiles,
    read one cookie for a host, and list every Salesforce ``sid`` in a store.
    Callers dispatch through this interface (via the registry in ``browsers.py``)
    instead of branching on ``CookieSource.family`` — so adding a family means
    adding one reader, not editing every dispatch site.
    """

    def discover(self, cfg: BrowserConfig) -> list[CookieSource]:
        """Enumerate the cookie stores this browser exposes on disk."""
        ...

    def read(self, source: CookieSource, host: str, name: str) -> str | None:
        """Return cookie ``name`` for ``host`` from ``source``, or ``None``."""
        ...

    def list_sids(self, source: CookieSource) -> list[tuple[str, str]]:
        """Return all ``(host, sid)`` Salesforce session cookies in ``source``."""
        ...
