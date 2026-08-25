"""MCP registration for typed browser-owned Salesforce reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import mcp.types as types


class BrowserRuntime(Protocol):
    def search_ownership(self, term: str) -> list[dict[str, str]]: ...

    def get_account_pipeline(self, account_id: str) -> list[dict[str, Any]]: ...

    def get_account_activities(
        self, account_id: str, *, limit: int
    ) -> dict[str, list[dict[str, str]]]: ...

    def get_accounts_context(
        self, account_ids: list[str], *, limit: int
    ) -> list[dict[str, Any]]: ...


def browser_tools() -> list[types.Tool]:
    """Return narrow read-only tools; no arbitrary URL, JavaScript, or SOQL."""
    account_id = {
        "type": "string",
        "pattern": "^001[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$",
        "description": "Salesforce Account ID (15 or 18 characters).",
    }
    return [
        types.Tool(
            name="browser_search_ownership",
            description=(
                "Search fixed Account, Contact, and Lead ownership fields inside "
                "the user's authenticated Salesforce browser session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 320,
                        "pattern": r"^[^\u0000-\u001f\u007f]+$",
                    }
                },
                "required": ["term"],
                "additionalProperties": False,
            },
        ),
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
        types.Tool(
            name="browser_get_accounts_context",
            description=(
                "Read pipeline and activity context for up to 10 Accounts in one "
                "validated managed browser session. Read-only; one background tab; "
                "cookies and browser security headers never enter tool arguments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_ids": {
                        "type": "array",
                        "items": account_id,
                        "minItems": 1,
                        "maxItems": 10,
                        "uniqueItems": True,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                        "default": 25,
                    },
                },
                "required": ["account_ids"],
                "additionalProperties": False,
            },
        ),
    ]


async def handle_browser_tool(
    name: str, arguments: dict[str, Any], runtime: BrowserRuntime
) -> list[types.TextContent]:
    """Dispatch one typed browser tool and return compact JSON."""
    if name == "browser_search_ownership":
        if set(arguments) != {"term"} or not isinstance(arguments.get("term"), str):
            raise ValueError("unexpected browser tool arguments")
        ownership_payload: object = {
            "records": await asyncio.to_thread(
                runtime.search_ownership, arguments["term"]
            )
        }
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    ownership_payload, separators=(",", ":"), ensure_ascii=True
                ),
            )
        ]

    if name == "browser_get_accounts_context":
        if not set(arguments).issubset({"account_ids", "limit"}):
            raise ValueError("unexpected browser tool arguments")
        account_ids = arguments.get("account_ids")
        if not isinstance(account_ids, list) or not all(
            isinstance(account_id, str) for account_id in account_ids
        ):
            raise ValueError("account_ids must be an array of strings")
        limit = arguments.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        batch_payload: object = {
            "accounts": await asyncio.to_thread(
                runtime.get_accounts_context, account_ids, limit=limit
            )
        }
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    batch_payload, separators=(",", ":"), ensure_ascii=True
                ),
            )
        ]

    account_id = arguments.get("account_id")
    if not isinstance(account_id, str):
        raise ValueError("missing account_id")
    if name == "browser_get_account_pipeline":
        if set(arguments) != {"account_id"}:
            raise ValueError("unexpected browser tool arguments")
        payload: object = {
            "records": await asyncio.to_thread(
                runtime.get_account_pipeline, account_id
            )
        }
    elif name == "browser_get_account_activities":
        if not set(arguments).issubset({"account_id", "limit"}):
            raise ValueError("unexpected browser tool arguments")
        limit = arguments.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        payload = await asyncio.to_thread(
            runtime.get_account_activities, account_id, limit=limit
        )
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
    upstream_call: Callable[[str, dict[str, Any]], Awaitable[list[types.TextContent]]],
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
