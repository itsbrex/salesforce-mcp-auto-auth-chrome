"""Entry point — patch `simple_salesforce`, then hand off to mcp-salesforce-connector."""
from __future__ import annotations

import os
import sys

from . import __version__
from .auth import read_sid_from_chrome
from .patch import install as install_patch


_OAUTH_ENV_VARS = (
    "SALESFORCE_CLIENT_ID",
    "SALESFORCE_CLIENT_SECRET",
    "SALESFORCE_DOMAIN",
)


def main() -> int:
    """Run the MCP server with auto-auth from Chrome cookies.

    Reads `SALESFORCE_INSTANCE_URL` from env, installs the per-call sid refresh
    patch, then calls `mcp-salesforce-connector`'s entry point. Always exits
    with the connector's exit code (or 1 on misconfiguration).
    """
    instance_url = os.environ.get("SALESFORCE_INSTANCE_URL")
    if not instance_url:
        print(
            "[salesforce-mcp-auto-auth-chrome] ERROR: SALESFORCE_INSTANCE_URL is not set. "
            "The Claude Desktop config entry for this MCP server must provide it.",
            file=sys.stderr,
        )
        return 1

    # Seed env so mcp-salesforce-connector initializes happily even if Chrome
    # currently has no sid. The per-call patch (installed below) ensures the
    # right token is used for every actual API request.
    initial_sid = read_sid_from_chrome(instance_url)
    os.environ["SALESFORCE_ACCESS_TOKEN"] = initial_sid or "PENDING_CHROME_LOGIN"

    # Clear OAuth env vars so the connector takes the session_id path. If a
    # user has those set from a previous config, leaving them in would make
    # the connector try OAuth-based auth instead.
    for key in _OAUTH_ENV_VARS:
        os.environ.pop(key, None)

    install_patch(instance_url)

    print(
        f"[salesforce-mcp-auto-auth-chrome v{__version__}] Ready for {instance_url} "
        f"(initial sid: {'present' if initial_sid else 'absent — will check Chrome per call'})",
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
