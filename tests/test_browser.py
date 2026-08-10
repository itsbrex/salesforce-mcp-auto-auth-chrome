from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from salesforce_mcp_auto_auth_chrome.browser import (
    BrowserBridgeError,
    SalesforceBrowser,
    _resolve_binary,
)


class FakeRunner:
    def __init__(
        self,
        *,
        browser_user_id: str = "005000000000000AAA",
        profile_output: str = "  comet-live comet - connected v1.0.22\n",
    ) -> None:
        self.browser_user_id = browser_user_id
        self.profile_output = profile_output
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> str:
        del timeout
        call = list(args)
        self.calls.append(call)
        joined = " ".join(call)
        if call[-2:] == ["profile", "list"]:
            return self.profile_output
        if " open " in f" {joined} ":
            return json.dumps({"url": "https://example.invalid", "page": "PAGE"})
        if call[-1:] == ["close"]:
            return json.dumps({"closed": True})
        if "chatter/users/me" in joined:
            return json.dumps(
                {
                    "host": "acme.lightning.force.com",
                    "userId": self.browser_user_id,
                }
            )
        if "ui-api/related-list-records" in joined:
            return json.dumps(
                {
                    "done": True,
                    "sourceKeys": ["count", "nextPageToken", "records"],
                    "totalSize": 1,
                    "records": [
                        {
                            "id": "006000000000000AAA",
                            "accountId": "001000000000000AAA",
                            "name": "Example",
                            "stage": "Qualification",
                            "closeDate": "2026-12-01",
                            "owner": "Owner",
                            "amount": 125000,
                            "expectedRevenue": 50000,
                            "probability": 40,
                            "type": "New Business",
                        }
                    ],
                }
            )
        if "OpenActivities" in joined and "eval" in call:
            return json.dumps(
                {
                    "empty": False,
                    "headers": ["Subject", "Status", "Due Date", "Assigned To"],
                    "records": [
                        {
                            "subject": "Follow up",
                            "status": "Not Started",
                            "due": "8/15/2026",
                            "assignee": "Owner",
                        }
                    ],
                }
            )
        if "ActivityHistories" in joined and "eval" in call:
            return json.dumps(
                {
                    "empty": False,
                    "headers": ["Subject", "Completed Date", "Assigned To"],
                    "records": [
                        {
                            "subject": "Call",
                            "date": "8/1/2026",
                            "assignee": "Owner",
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected command: {call!r}")


def _browser(runner: FakeRunner) -> SalesforceBrowser:
    return SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="comet",
        pin_profile="Default",
        runner=runner,
        sid_reader=lambda *_args, **_kwargs: "SID-SECRET",
        identity_reader=lambda _url, _sid: "005000000000000AAA",
        binary="opencli",
    )


def test_pipeline_uses_browser_owned_credentials_without_exporting_sid() -> None:
    runner = FakeRunner()

    records = _browser(runner).get_account_pipeline("001000000000000AAA")

    assert len(records) == 1
    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert "--window background" in command_text
    assert "credentials:'same-origin'" in command_text
    assert "Accept:'application/json'" in command_text
    assert "SID-SECRET" not in command_text
    assert "Authorization" not in command_text
    assert "Cookie" not in command_text
    assert "FROM Opportunity" not in command_text
    assert any("close" in call for call in runner.calls)


def test_single_connected_profile_is_allowed_after_identity_match() -> None:
    runner = FakeRunner()
    browser = SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="chrome",
        pin_profile="Default",
        runner=runner,
        sid_reader=lambda *_args, **_kwargs: "SID",
        identity_reader=lambda _url, _sid: "005000000000000AAA",
        binary="opencli",
    )

    records = browser.get_account_pipeline("001000000000000AAA")

    assert len(records) == 1


def test_ambiguous_profile_mismatch_fails_closed_before_open() -> None:
    runner = FakeRunner(
        profile_output=(
            "  comet-one comet - connected v1.0.22\n"
            "  comet-two comet - connected v1.0.22\n"
        )
    )
    browser = SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="chrome",
        pin_profile="Default",
        runner=runner,
        sid_reader=lambda *_args, **_kwargs: "SID",
        identity_reader=lambda _url, _sid: "005000000000000AAA",
        binary="opencli",
    )

    with pytest.raises(BrowserBridgeError, match="browser profile"):
        browser.get_account_pipeline("001000000000000AAA")

    assert not any("open" in call for call in runner.calls)


def test_user_mismatch_fails_closed_and_closes_managed_tab() -> None:
    runner = FakeRunner(browser_user_id="005999999999999AAA")

    with pytest.raises(BrowserBridgeError, match="identity"):
        _browser(runner).get_account_pipeline("001000000000000AAA")

    assert any("close" in call for call in runner.calls)


def test_account_activities_return_only_typed_dashboard_fields() -> None:
    runner = FakeRunner()

    result = _browser(runner).get_account_activities(
        "001000000000000AAA", limit=10
    )

    assert result == {
        "upcoming": [
            {
                "subject": "Follow up",
                "status": "Not Started",
                "due": "8/15/2026",
                "assignee": "Owner",
            }
        ],
        "recent": [
            {"subject": "Call", "date": "8/1/2026", "assignee": "Owner"}
        ],
    }


def test_invalid_account_id_never_reaches_browser() -> None:
    runner = FakeRunner()

    with pytest.raises(ValueError, match="account_id"):
        _browser(runner).get_account_pipeline("001' OR Name != '")

    assert runner.calls == []


def test_binary_resolution_prefers_executable_user_bridge(
    monkeypatch, tmp_path
) -> None:
    user_binary = tmp_path / "bin" / "opencli"
    user_binary.parent.mkdir()
    user_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    user_binary.chmod(0o755)
    monkeypatch.delenv("SALESFORCE_OPENCLI_BIN", raising=False)
    monkeypatch.setattr(
        "salesforce_mcp_auto_auth_chrome.browser.Path.home", lambda: tmp_path
    )
    monkeypatch.setattr(
        "salesforce_mcp_auto_auth_chrome.browser.shutil.which",
        lambda _name: "/usr/local/bin/opencli",
    )

    assert _resolve_binary(None) == str(user_binary)


def test_missing_bridge_is_deferred_until_browser_tool_call(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("SALESFORCE_OPENCLI_BIN", raising=False)
    monkeypatch.setattr(
        "salesforce_mcp_auto_auth_chrome.browser.Path.home", lambda: tmp_path
    )
    monkeypatch.setattr(
        "salesforce_mcp_auto_auth_chrome.browser.shutil.which", lambda _name: None
    )
    runner = FakeRunner()
    browser = SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="comet",
        pin_profile="Default",
        runner=runner,
        sid_reader=lambda *_args, **_kwargs: "SID",
        identity_reader=lambda _url, _sid: "005000000000000AAA",
    )

    with pytest.raises(BrowserBridgeError, match="unavailable"):
        browser.get_account_pipeline("001000000000000AAA")

    assert runner.calls == []
