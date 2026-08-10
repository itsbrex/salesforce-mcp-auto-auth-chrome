from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from salesforce_mcp_auto_auth_chrome import browser_only


def test_browser_only_server_exposes_exact_read_tools() -> None:
    assert [tool.name for tool in asyncio.run(browser_only.list_tools())] == [
        "browser_search_ownership",
        "browser_get_account_pipeline",
        "browser_get_account_activities",
        "browser_get_accounts_context",
    ]


def test_browser_only_runtime_requires_configured_org() -> None:
    with pytest.raises(ValueError, match="SALESFORCE_INSTANCE_URL"):
        browser_only.create_runtime({})


def test_browser_only_entrypoint_has_no_sid_or_upstream_http_imports() -> None:
    source = Path(browser_only.__file__).read_text(encoding="utf-8")
    for forbidden in [
        "resolve_session",
        "read_sid",
        "install_patch",
        "simple_salesforce",
        "src.salesforce",
        "urllib",
        "requests",
    ]:
        assert forbidden not in source
