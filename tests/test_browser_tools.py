from __future__ import annotations

import asyncio
import json

from salesforce_mcp_auto_auth_chrome.browser_tools import (
    browser_tools,
    handle_browser_tool,
    install_browser_tools,
)


class FakeRuntime:
    def get_account_pipeline(self, account_id: str) -> list[dict[str, object]]:
        return [{"accountId": account_id, "name": "Example"}]

    def get_account_activities(
        self, account_id: str, *, limit: int
    ) -> dict[str, list[dict[str, str]]]:
        return {"upcoming": [{"subject": account_id}], "recent": []}


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
        "browser_get_account_pipeline",
        "browser_get_account_activities",
    ]
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert tools[1].inputSchema["properties"]["limit"]["maximum"] == 25


def test_handle_browser_tool_returns_compact_json() -> None:
    content = asyncio.run(
        handle_browser_tool(
            "browser_get_account_pipeline",
            {"account_id": "001000000000000AAA"},
            FakeRuntime(),
        )
    )

    assert json.loads(content[0].text) == {
        "records": [
            {"accountId": "001000000000000AAA", "name": "Example"}
        ]
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
    assert len(asyncio.run(server.list_handler())) == 2
    assert asyncio.run(server.call_handler("existing", {})) == ["upstream:existing"]
    result = asyncio.run(
        server.call_handler(
            "browser_get_account_activities",
            {"account_id": "001000000000000AAA", "limit": 5},
        )
    )
    assert json.loads(result[0].text)["upcoming"][0]["subject"].startswith("001")
