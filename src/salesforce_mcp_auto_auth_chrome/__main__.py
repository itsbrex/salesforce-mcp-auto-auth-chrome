"""Entry point — patch `simple_salesforce`, then hand off to mcp-salesforce-connector."""

from __future__ import annotations

import os
import sys

from . import __version__
from .cookies import parse_env, resolve_session
from .patch import install as install_patch


_OAUTH_ENV_VARS = (
    "SALESFORCE_CLIENT_ID",
    "SALESFORCE_CLIENT_SECRET",
    "SALESFORCE_DOMAIN",
)


def main() -> int:
    """Run the MCP server with auto-auth from Chrome cookies.

    Reads the optional `SALESFORCE_INSTANCE_URL` from env (a Lightning or My
    Domain URL — it's normalized to the My Domain REST host), or auto-discovers a
    logged-in org from the browser cookie stores. Installs the per-call sid
    refresh patch, then calls `mcp-salesforce-connector`'s entry point. Always
    exits with the connector's exit code (or 1 on misconfiguration).
    """
    configured_url = os.environ.get("SALESFORCE_INSTANCE_URL")

    # Resolve which browsers/profiles to search (env overrides; defaults = all).
    browsers, profiles = parse_env(os.environ)

    # Resolve the org: normalize a configured Lightning/My Domain URL, or
    # auto-discover a logged-in org whose sid validates against the REST API.
    resolved = resolve_session(configured_url, browsers, profiles)
    instance_url = resolved.instance_url
    initial_sid = resolved.sid
    if not instance_url:
        print(
            "[salesforce-mcp-auto-auth-chrome] ERROR: no Salesforce org configured "
            "via SALESFORCE_INSTANCE_URL and none could be auto-detected from your "
            "browsers. Log into a Salesforce org in a supported browser, or set "
            "SALESFORCE_INSTANCE_URL.",
            file=sys.stderr,
        )
        return 1

    # The connector reads SALESFORCE_INSTANCE_URL for its REST base; pin it to the
    # normalized My Domain host so Lightning URLs and auto-discovery work.
    os.environ["SALESFORCE_INSTANCE_URL"] = instance_url

    # Seed env so mcp-salesforce-connector initializes happily even if no
    # browser currently has a sid. The per-call patch (installed below) ensures
    # the right token is used for every actual API request.
    os.environ["SALESFORCE_ACCESS_TOKEN"] = initial_sid or "PENDING_CHROME_LOGIN"

    # Clear OAuth env vars so the connector takes the session_id path. If a
    # user has those set from a previous config, leaving them in would make
    # the connector try OAuth-based auth instead.
    for key in _OAUTH_ENV_VARS:
        os.environ.pop(key, None)

    install_patch(
        instance_url,
        pin_browser=resolved.browser,
        pin_profile=resolved.profile,
        browsers=browsers,
        profiles=profiles,
    )

    pinned = (
        f"{resolved.browser}/{resolved.profile}" if resolved.browser else "any profile"
    )
    print(
        f"[salesforce-mcp-auto-auth-chrome v{__version__}] Ready for {instance_url} "
        f"(initial sid: {'present' if initial_sid else 'absent — will check per call'}; "
        f"pinned to {pinned})",
        file=sys.stderr,
    )

    # Hand off to mcp-salesforce-connector's main. Its entry point is
    # exposed as `src.salesforce:main` (an odd convention from that package's
    # `src/`-layout publish — see its pyproject.toml for the [project.scripts]
    # section).
    from src.salesforce import main as connector_main  # type: ignore[import-not-found]

    return connector_main()


if __name__ == "__main__":
    sys.exit(main())
