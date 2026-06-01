"""Read the Salesforce `sid` session cookie from Chrome on macOS.

The `sid` cookie is what Chrome stores when you're logged into a Salesforce org.
It's identical in value (and works identically) to a session-based API access
token, so we can use it directly as an `Authorization: Bearer <sid>` header.

This module deliberately swallows all errors and returns `None` instead of
raising. The reason: we want the rest of the wrapper to be able to start
the MCP server even when no session is available, and surface a friendly
error only at the point a tool is actually invoked.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def read_sid_from_chrome(instance_url: str) -> str | None:
    """Read the Salesforce `sid` cookie from Chrome for the given instance URL.

    Args:
        instance_url: The Salesforce My Domain URL, e.g.
            ``https://acme.my.salesforce.com``. Must include the scheme.

    Returns:
        The session id string, or ``None`` if no valid session was found.
        Returns ``None`` (rather than raising) on any failure — cookie missing,
        Keychain locked, pycookiecheat error, etc. — so callers can defer the
        error to the actual tool-call time.
    """
    try:
        from pycookiecheat import chrome_cookies
    except ImportError as e:
        log.error("pycookiecheat not installed: %s", e)
        return None

    try:
        cookies = chrome_cookies(instance_url)
    except Exception as e:  # noqa: BLE001 — intentional broad catch, see module docstring
        log.warning("cookie read failed for %s: %s: %s", instance_url, type(e).__name__, e)
        return None

    sid = cookies.get("sid")
    if not sid:
        log.info(
            "no 'sid' cookie in Chrome for %s (found %d other cookies)",
            instance_url,
            len(cookies),
        )
        return None
    return sid
