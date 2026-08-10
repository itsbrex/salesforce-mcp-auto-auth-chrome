"""Print sanitized validity state for browser-owned Salesforce session."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal, TypedDict

from .__main__ import parse_env
from .browser import BrowserBridgeError, SalesforceBrowser
from .instance import is_salesforce_host, normalize_instance_url


class SessionStatus(TypedDict):
    """Public status boundary. Never add session or identity fields."""

    state: Literal["active", "inactive"]


def read_session_status(
    configured_url: str | None,
    browsers: list[str] | None,
    profiles: list[str] | None,
) -> SessionStatus:
    """Resolve browser session and confirm Salesforce accepts current SID."""
    if not configured_url:
        return {"state": "inactive"}
    try:
        instance_url = normalize_instance_url(configured_url)
    except ValueError:
        return {"state": "inactive"}
    if instance_url is None or not is_salesforce_host(instance_url):
        return {"state": "inactive"}
    browser = SalesforceBrowser(
        instance_url,
        pin_browser=browsers[0] if browsers and len(browsers) == 1 else None,
        pin_profile=profiles[0] if profiles and len(profiles) == 1 else None,
        browsers=browsers,
        profiles=profiles,
    )
    try:
        active = browser.session_is_active()
    except BrowserBridgeError:
        active = False
    finally:
        browser.close()
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
