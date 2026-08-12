"""Patch simple_salesforce, then hand off to mcp-salesforce-connector."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import cast

from . import __version__
from .browser import BrowserBridgeError, SalesforceBrowser
from .browser_tools import install_browser_tools
from .browsers import default_browsers
from .orgs import resolve_session
from .patch import PENDING_LOGIN_SENTINEL
from .patch import install as install_patch

log = logging.getLogger(__name__)


_OAUTH_ENV_VARS = (
    "SALESFORCE_CLIENT_ID",
    "SALESFORCE_CLIENT_SECRET",
    "SALESFORCE_DOMAIN",
)


def parse_env(
    environ: Mapping[str, str],
) -> tuple[list[str] | None, list[str] | None]:
    """Parse the browser/profile scoping env vars.

    Three comma-separated lists control which cookie stores are scanned:

    - ``SALESFORCE_BROWSERS`` — allowlist of browser keys, in priority order.
      Naming an opt-in browser here (e.g. ``firefox``) is how you enable it.
    - ``SALESFORCE_SKIP_BROWSERS`` — denylist of browser keys to exclude (applied
      after the allowlist; e.g. ``safari`` to skip probing Safari entirely).
    - ``SALESFORCE_PROFILES`` — profile names to restrict to.

    Returns ``(browsers, profiles)``. ``browsers`` is ``None`` (meaning "the
    default scan set" — every registered browser except opt-in ones like
    Firefox) only when neither the allowlist nor the denylist is set; once a
    denylist is applied it becomes an explicit list (possibly empty, meaning
    "scan nothing"), computed against the default scan set.
    """
    browsers = _split(environ.get("SALESFORCE_BROWSERS"))
    profiles = _split(environ.get("SALESFORCE_PROFILES"))
    skip = _split(environ.get("SALESFORCE_SKIP_BROWSERS"))
    if skip:
        excluded = {s.lower() for s in skip}
        base = browsers if browsers is not None else default_browsers()
        browsers = [b for b in base if b.lower() not in excluded]
    return browsers, profiles


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


def main() -> int:
    """Run the MCP server with auto-auth from Chrome cookies.

    Reads the optional `SALESFORCE_INSTANCE_URL` from env (a Lightning or My
    Domain URL — it's normalized to the My Domain REST host), or auto-discovers a
    logged-in org from the browser cookie stores. Installs the per-call sid
    refresh patch, then calls `mcp-salesforce-connector`'s entry point. Always
    exits with the connector's exit code (or 1 on misconfiguration).
    """
    if os.environ.get("SALESFORCE_BROWSER_ONLY") == "1":
        from .browser_only import main as browser_only_main

        return browser_only_main(os.environ)

    # Logs go to stderr — stdout is the MCP stdio protocol channel and must stay
    # clean. Other modules log via `logging`; configure the root logger here so
    # their messages (and ours) actually surface.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[salesforce-mcp-auto-auth-chrome] %(levelname)s: %(message)s",
    )

    # macOS-only: cookie stores live under ~/Library, decryption uses the macOS
    # Keychain via the `security` CLI, and Safari cookies are a macOS binary
    # format. Fail fast with a clear message instead of a confusing downstream
    # traceback on Linux/Windows.
    if sys.platform != "darwin":
        log.error(
            "this server is macOS-only (it reads browser cookies via the macOS "
            "Keychain and ~/Library paths); detected platform %r.",
            sys.platform,
        )
        return 1

    configured_url = os.environ.get("SALESFORCE_INSTANCE_URL")

    # Resolve which browsers/profiles to search (env overrides; defaults = all).
    browsers, profiles = parse_env(os.environ)

    # Resolve the org: normalize a configured Lightning/My Domain URL, or
    # auto-discover a logged-in org whose sid validates against the REST API.
    resolved = resolve_session(configured_url, browsers, profiles)
    instance_url = resolved.instance_url
    initial_sid = resolved.sid
    if not instance_url:
        log.error(
            "no Salesforce org configured via SALESFORCE_INSTANCE_URL and none "
            "could be auto-detected from your browsers. Log into a Salesforce org "
            "in a supported browser, or set SALESFORCE_INSTANCE_URL."
        )
        return 1

    # The connector reads SALESFORCE_INSTANCE_URL for its REST base; pin it to the
    # normalized My Domain host so Lightning URLs and auto-discovery work.
    os.environ["SALESFORCE_INSTANCE_URL"] = instance_url

    # Seed env so mcp-salesforce-connector initializes happily even if no
    # browser currently has a sid. The per-call patch (installed below) ensures
    # the right token is used for every actual API request — and it rejects this
    # sentinel defensively, so it can never reach Salesforce as a bogus token.
    os.environ["SALESFORCE_ACCESS_TOKEN"] = initial_sid or PENDING_LOGIN_SENTINEL

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
    log.info(
        "v%s ready for %s (initial sid: %s; pinned to %s)",
        __version__,
        instance_url,
        "present" if initial_sid else "absent — will check per call",
        pinned,
    )

    # Hand off to mcp-salesforce-connector's main. Its entry point is exposed as
    # `src.salesforce:main` (an artifact of that package's `src/`-layout publish).
    # Import defensively: a missing/renamed dependency should produce a clear,
    # actionable message rather than an opaque ModuleNotFoundError for the bare
    # top-level name `src`.
    try:
        from src.salesforce import main as connector_main
        from src.salesforce import server as connector_server
    except ImportError as e:
        log.error(
            "could not import the bundled MCP Salesforce connector "
            "(`from src.salesforce import main`): %s. Ensure "
            "'mcp-salesforce-connector' is installed in this environment "
            "(run `uv sync`).",
            e,
        )
        return 1

    # The browser-owned reads only work against a My Domain host, which is a
    # narrower set than the orgs the sid patch serves (classic pod and login
    # hosts resolve fine but have no Lightning equivalent to drive). Losing
    # those four tools must not cost the user the other fourteen, so failing to
    # build the runtime downgrades the surface instead of killing startup.
    browser_runtime: SalesforceBrowser | None = None
    try:
        browser_runtime = SalesforceBrowser(
            instance_url,
            pin_browser=resolved.browser,
            pin_profile=resolved.profile,
            browsers=browsers,
            profiles=profiles,
        )
    except BrowserBridgeError as e:
        log.warning("browser-owned tools unavailable for %s: %s", instance_url, e)
    else:
        install_browser_tools(
            connector_server.server,
            upstream_list=connector_server.handle_list_tools,
            upstream_call=connector_server.handle_call_tool,
            runtime=browser_runtime,
        )

    # The connector is untyped; it returns an int exit code.
    try:
        return cast(int, connector_main())
    except KeyboardInterrupt:
        # Ctrl+C / SIGINT during the stdio server loop. The bundled connector's
        # `asyncio.run(server.run())` re-raises KeyboardInterrupt and dumps a
        # traceback; swallow it here for a clean shutdown. (KeyboardInterrupt is a
        # BaseException, so the `except ImportError` above does not catch it.)
        log.info("interrupted; shutting down.")
        return 0
    finally:
        # The runtime owns a background tab in the user's real browser; leaving
        # it open outlives the process the user thinks they just quit.
        if browser_runtime is not None:
            browser_runtime.close()


if __name__ == "__main__":
    sys.exit(main())
