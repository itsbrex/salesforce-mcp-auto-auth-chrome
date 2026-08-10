"""MCP registration for typed browser-owned Salesforce reads."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import mcp.types as types


class BrowserRuntime(Protocol):
    def get_account_pipeline(self, account_id: str) -> list[dict[str, Any]]: ...

    def get_account_activities(
        self, account_id: str, *, limit: int
    ) -> dict[str, list[dict[str, str]]]: ...


def browser_tools() -> list[types.Tool]:
    """Return narrow read-only tools; no arbitrary URL, JavaScript, or SOQL."""
    account_id = {
        "type": "string",
        "pattern": "^001[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$",
        "description": "Salesforce Account ID (15 or 18 characters).",
    }
    return [
        types.Tool(
            name="browser_get_account_pipeline",
            description=(
                "Read standard opportunity pipeline fields for one Account inside "
                "the user's authenticated Salesforce browser session. Read-only; "
                "cookies and browser security headers never enter tool arguments."
            ),
            inputSchema={
                "type": "object",
                "properties": {"account_id": account_id},
                "required": ["account_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="browser_get_account_activities",
            description=(
                "Read upcoming and historical activity grids for one Account via "
                "native Salesforce related-list pages in a managed background tab."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": account_id,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                        "default": 25,
                    },
                },
                "required": ["account_id"],
                "additionalProperties": False,
            },
        ),
    ]


async def handle_browser_tool(
    name: str, arguments: dict[str, Any], runtime: BrowserRuntime
) -> list[types.TextContent]:
    """Dispatch one typed browser tool and return compact JSON."""
    account_id = arguments.get("account_id")
    if not isinstance(account_id, str):
        raise ValueError("missing account_id")
    if name == "browser_get_account_pipeline":
        if set(arguments) != {"account_id"}:
            raise ValueError("unexpected browser tool arguments")
        payload: object = {"records": runtime.get_account_pipeline(account_id)}
    elif name == "browser_get_account_activities":
        if not set(arguments).issubset({"account_id", "limit"}):
            raise ValueError("unexpected browser tool arguments")
        limit = arguments.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        payload = runtime.get_account_activities(account_id, limit=limit)
    else:
        raise ValueError(f"unknown browser tool: {name}")
    return [
        types.TextContent(
            type="text",
            text=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
        )
    ]


def install_browser_tools(
    server: Any,
    *,
    upstream_list: Callable[[], Awaitable[list[types.Tool]]],
    upstream_call: Callable[
        [str, dict[str, Any]], Awaitable[list[types.TextContent]]
    ],
    runtime: BrowserRuntime,
) -> None:
    """Extend connector handlers while preserving every upstream dispatch."""
    names = {tool.name for tool in browser_tools()}

    async def combined_list_tools() -> list[types.Tool]:
        return [*await upstream_list(), *browser_tools()]

    async def combined_call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name in names:
            return await handle_browser_tool(name, arguments, runtime)
        return await upstream_call(name, arguments)

    server.list_tools()(combined_list_tools)
    server.call_tool()(combined_call_tool)
