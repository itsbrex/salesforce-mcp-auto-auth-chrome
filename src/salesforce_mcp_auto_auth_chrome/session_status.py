"""Print sanitized validity state for browser-owned Salesforce session."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal, TypedDict

from .__main__ import parse_env
from .orgs import resolve_session
from .validate import session_is_valid


class SessionStatus(TypedDict):
    """Public status boundary. Never add session or identity fields."""

    state: Literal["active", "inactive"]


def read_session_status(
    configured_url: str | None,
    browsers: list[str] | None,
    profiles: list[str] | None,
) -> SessionStatus:
    """Resolve browser session and confirm Salesforce accepts current SID."""
    resolved = resolve_session(configured_url, browsers, profiles)
    active = bool(
        resolved.instance_url
        and resolved.sid
        and session_is_valid(resolved.instance_url, resolved.sid)
    )
    return {"state": "active" if active else "inactive"}


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Write one sanitized JSON status object to stdout."""
    browsers, profiles = parse_env(environ)
    status = read_session_status(
        environ.get("SALESFORCE_INSTANCE_URL"), browsers, profiles
    )
    print(json.dumps(status, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
