from __future__ import annotations

import asyncio
import json

from salesforce_mcp_auto_auth_chrome.browser_tools import (
    browser_tools,
    handle_browser_tool,
    install_browser_tools,
)


class FakeRuntime:
    def search_ownership(self, term: str) -> list[dict[str, object]]:
        return [{"type": "Account", "name": term}]

    def get_account_pipeline(self, account_id: str) -> list[dict[str, object]]:
        return [{"accountId": account_id, "name": "Example"}]

    def get_account_activities(
        self, account_id: str, *, limit: int
    ) -> dict[str, list[dict[str, str]]]:
        return {"upcoming": [{"subject": account_id}], "recent": []}

    def get_accounts_context(
        self, account_ids: list[str], *, limit: int
    ) -> list[dict[str, object]]:
        return [
            {
                "accountId": account_id,
                "opportunities": [],
                "upcoming": [{"subject": account_id, "limit": limit}],
                "recent": [],
            }
            for account_id in account_ids
        ]


class FakeServer:
    def __init__(self) -> None:
        self.list_handler = None
        self.call_handler = None

    def list_tools(self):
        def register(handler):
            self.list_handler = handler
            return handler

        return register

    def call_tool(self):
        def register(handler):
            self.call_handler = handler
            return handler

        return register


def test_browser_tools_are_typed_and_disallow_extra_inputs() -> None:
    tools = browser_tools()

    assert [tool.name for tool in tools] == [
        "browser_search_ownership",
        "browser_get_account_pipeline",
        "browser_get_account_activities",
        "browser_get_accounts_context",
    ]
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert tools[0].inputSchema["properties"]["term"]["maxLength"] == 320
    assert tools[2].inputSchema["properties"]["limit"]["maximum"] == 25
    assert tools[3].inputSchema["properties"]["account_ids"]["maxItems"] == 10
    assert tools[3].inputSchema["properties"]["account_ids"]["uniqueItems"] is True


def test_handle_browser_tool_returns_compact_json() -> None:
    content = asyncio.run(
        handle_browser_tool(
            "browser_get_account_pipeline",
            {"account_id": "001000000000000AAA"},
            FakeRuntime(),
        )
    )

    assert json.loads(content[0].text) == {
        "records": [{"accountId": "001000000000000AAA", "name": "Example"}]
    }


def test_install_preserves_upstream_tools_and_dispatch() -> None:
    server = FakeServer()

    async def upstream_list():
        return []

    async def upstream_call(name: str, arguments: dict[str, object]):
        del arguments
        return [f"upstream:{name}"]

    install_browser_tools(
        server,
        upstream_list=upstream_list,
        upstream_call=upstream_call,
        runtime=FakeRuntime(),
    )

    assert server.list_handler is not None
    assert server.call_handler is not None
    assert len(asyncio.run(server.list_handler())) == 4
    assert asyncio.run(server.call_handler("existing", {})) == ["upstream:existing"]
    result = asyncio.run(
        server.call_handler(
            "browser_get_account_activities",
            {"account_id": "001000000000000AAA", "limit": 5},
        )
    )
    assert json.loads(result[0].text)["upcoming"][0]["subject"].startswith("001")


def test_handle_batch_tool_returns_all_account_contexts() -> None:
    content = asyncio.run(
        handle_browser_tool(
            "browser_get_accounts_context",
            {
                "account_ids": [
                    "001000000000000AAA",
                    "001000000000001AAA",
                ],
                "limit": 10,
            },
            FakeRuntime(),
        )
    )

    payload = json.loads(content[0].text)
    assert [item["accountId"] for item in payload["accounts"]] == [
        "001000000000000AAA",
        "001000000000001AAA",
    ]


def test_handle_ownership_tool_accepts_term_not_arbitrary_sosl() -> None:
    content = asyncio.run(
        handle_browser_tool(
            "browser_search_ownership",
            {"term": "Example Company"},
            FakeRuntime(),
        )
    )

    assert json.loads(content[0].text) == {
        "records": [{"type": "Account", "name": "Example Company"}]
    }
