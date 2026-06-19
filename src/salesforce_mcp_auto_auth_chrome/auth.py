"""Read the Salesforce `sid` session cookie from the user's browsers (macOS).

The `sid` cookie is what a browser stores when you're logged into a Salesforce
org. It's identical in value (and works identically) to a session-based API
access token, so we can use it directly as an `Authorization: Bearer <sid>`
header.

Reading scans multiple browsers and profiles (see `cookies.read_sid`). This
module deliberately swallows all errors and returns `None` instead of raising,
so the MCP server can start even when no session is available and surface a
friendly error only when a tool is actually invoked.
"""

from __future__ import annotations

from .cookies import read_sid

__all__ = ["read_sid", "read_sid_from_chrome"]


def read_sid_from_chrome(instance_url: str) -> str | None:
    """Back-compat shim: read the `sid` from Chrome's default-priority profiles.

    Prefer `read_sid` for multi-browser support. Retained so existing callers
    and external imports keep working.
    """
    return read_sid(instance_url, browsers=["chrome"])
