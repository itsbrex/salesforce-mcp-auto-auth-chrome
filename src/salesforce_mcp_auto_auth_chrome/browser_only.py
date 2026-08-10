"""Strict browser-only MCP server for Ownership Desk."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from . import __version__
from .browser import SalesforceBrowser
from .browser_tools import browser_tools, handle_browser_tool
from .instance import is_salesforce_host, normalize_instance_url

server = Server("salesforce-browser-only")
_runtime: SalesforceBrowser | None = None


def create_runtime(environ: Mapping[str, str]) -> SalesforceBrowser:
    """Create browser runtime without reading or exporting Salesforce SID."""
    configured_url = environ.get("SALESFORCE_INSTANCE_URL")
    if not configured_url:
        raise ValueError("SALESFORCE_INSTANCE_URL is required")
    instance_url = normalize_instance_url(configured_url)
    if instance_url is None or not is_salesforce_host(instance_url):
        raise ValueError("SALESFORCE_INSTANCE_URL must use a Salesforce host")
    browsers = _split(environ.get("SALESFORCE_BROWSERS"))
    profiles = _split(environ.get("SALESFORCE_PROFILES"))
    return SalesforceBrowser(
        instance_url,
        pin_browser=browsers[0] if browsers and len(browsers) == 1 else None,
        pin_profile=profiles[0] if profiles and len(profiles) == 1 else None,
        browsers=browsers,
        profiles=profiles,
        binary=environ.get("SALESFORCE_OPENCLI_BIN"),
    )


async def list_tools() -> list[types.Tool]:
    """Return exact browser-owned read surface."""
    return browser_tools()


async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Dispatch only fixed browser-owned reads."""
    if _runtime is None:
        raise RuntimeError("Salesforce browser runtime is unavailable")
    return await handle_browser_tool(name, arguments, _runtime)


server.list_tools()(list_tools)  # type: ignore[no-untyped-call]
server.call_tool()(call_tool)


async def run(runtime: SalesforceBrowser) -> None:
    """Run strict stdio MCP server."""
    global _runtime
    _runtime = runtime
    try:
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(
                read,
                write,
                InitializationOptions(
                    server_name="salesforce-browser-only",
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        runtime.close()


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Start strict server or fail closed on missing browser configuration."""
    try:
        runtime = create_runtime(environ)
    except (TypeError, ValueError) as error:
        print(f"[salesforce-browser-only] {error}", file=sys.stderr)
        return 1
    try:
        asyncio.run(run(runtime))
    except KeyboardInterrupt:
        return 0
    return 0


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    values = [part.strip() for part in value.split(",") if part.strip()]
    return values or None


if __name__ == "__main__":
    raise SystemExit(main())
