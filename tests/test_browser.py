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
        fail_on_history: bool = False,
    ) -> None:
        self.browser_user_id = browser_user_id
        self.profile_output = profile_output
        self.fail_on_history = fail_on_history
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> str:
        del timeout
        call = list(args)
        self.calls.append(call)
        joined = " ".join(call)
        if call[-2:] == ["profile", "list"]:
            return self.profile_output
        if call[-2:] == ["tab", "list"]:
            return json.dumps(
                [
                    {
                        "targetId": "SF-TARGET",
                        "url": "https://acme.lightning.force.com/lightning/page/home",
                        "title": "Salesforce",
                    },
                    {
                        "targetId": "OTHER-TARGET",
                        "url": "https://example.invalid/",
                        "title": "Other",
                    },
                ]
            )
        if call[-1:] == ["bind"]:
            return json.dumps(
                {
                    "session": "synthetic",
                    "url": "https://acme.lightning.force.com/lightning/page/home",
                    "title": "Salesforce",
                }
            )
        if " open " in f" {joined} ":
            return json.dumps(
                {
                    "url": "https://acme.lightning.force.com/lightning/page/home",
                    "page": "SF-TARGET",
                }
            )
        if call[-1:] == ["close"]:
            return json.dumps({"closed": True})
        if "chatter/users/me" in joined:
            return json.dumps(
                {
                    "host": "acme.lightning.force.com",
                    "userId": self.browser_user_id,
                }
            )
        if "const prefixes=" in joined:
            return json.dumps(
                {
                    "sourceType": "classic_search_page",
                    "totalSize": 1,
                    "records": [
                        {
                            "type": "Account",
                            "id": "001000000000000AAA",
                            "name": "Example Company",
                            "owner": "Owner",
                            "email": "",
                            "website": "https://example.invalid",
                            "accountId": "",
                            "company": "",
                        }
                    ],
                }
            )
        if "const accountIds=" in joined and "ui-api/related-list-records" in joined:
            return json.dumps(
                [
                    {
                        "accountId": account_id,
                        "pipeline": {
                            "done": True,
                            "sourceKeys": ["count", "nextPageToken", "records"],
                            "totalSize": 1,
                            "records": [
                                {
                                    "id": opportunity_id,
                                    "accountId": account_id,
                                    "name": f"Example {index}",
                                    "stage": "Qualification",
                                    "closeDate": "2026-12-01",
                                    "owner": "Owner",
                                    "amount": 125000,
                                    "expectedRevenue": 50000,
                                    "probability": 40,
                                    "type": "New Business",
                                }
                            ],
                        },
                    }
                    for index, (account_id, opportunity_id) in enumerate(
                        [
                            ("001000000000000AAA", "006000000000000AAA"),
                            ("001000000000001AAA", "006000000000001AAA"),
                        ],
                        start=1,
                    )
                ]
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
            if self.fail_on_history:
                raise BrowserBridgeError("synthetic history failure")
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
        binary="opencli",
        pace_seconds=0,
    )


def test_pipeline_uses_browser_owned_credentials_without_exporting_sid() -> None:
    runner = FakeRunner()

    records = _browser(runner).get_account_pipeline("001000000000000AAA")

    assert len(records) == 1
    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert "--window background" in command_text
    assert "--tab SF-TARGET" in command_text
    assert "credentials:'same-origin'" in command_text
    assert "Accept:'application/json'" in command_text
    assert "Authorization" not in command_text
    assert "Cookie" not in command_text
    assert "FROM Opportunity" not in command_text
    assert not any(call[-1:] == ["close"] for call in runner.calls)


def test_ownership_search_executes_inside_browser_with_fixed_contract() -> None:
    runner = FakeRunner()

    records = _browser(runner).search_ownership("Example Company")

    assert records == [
        {
            "type": "Account",
            "id": "001000000000000AAA",
            "name": "Example Company",
            "owner": "Owner",
            "email": "",
            "website": "https://example.invalid",
            "accountId": "",
            "company": "",
        }
    ]
    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert (
        "/_ui/search/ui/UnifiedSearchResults?searchType=2&str=Example%20Company"
        in command_text
    )
    assert "--tab SF-TARGET" in command_text
    assert "document.querySelectorAll('table')" in command_text
    assert "actionLabels.has" in command_text
    assert "/services/data/v60.0/search/?q=" not in command_text
    ownership_command = next(
        command for command in command_text.splitlines() if "const prefixes=" in command
    )
    assert "fetch(" not in ownership_command
    assert "Authorization" not in command_text
    assert "Cookie" not in command_text


def test_ownership_search_url_encodes_literal_term() -> None:
    runner = FakeRunner()

    _browser(runner).search_ownership("A&B + West")

    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert "str=A%26B%20%2B%20West" in command_text


def test_browser_operations_are_serialized_with_minimum_pacing() -> None:
    runner = FakeRunner()
    current = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    browser = SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="comet",
        pin_profile="Default",
        runner=runner,
        binary="opencli",
        pace_seconds=0.9,
        sleep=sleep,
        monotonic=lambda: current[0],
    )

    browser.get_account_pipeline("001000000000000AAA")

    assert sleeps == [0.9, 0.9]
    assert sum(" --window background" in " ".join(call) for call in runner.calls) == 1


def test_session_status_is_validated_inside_browser() -> None:
    runner = FakeRunner()

    assert _browser(runner).session_is_active() is True

    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert "chatter/users/me" in command_text
    assert "credentials:'same-origin'" in command_text
    assert "Authorization" not in command_text


def test_single_connected_profile_is_allowed_after_identity_match() -> None:
    runner = FakeRunner()
    browser = SalesforceBrowser(
        "https://acme.my.salesforce.com",
        pin_browser="chrome",
        pin_profile="Default",
        runner=runner,
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
        binary="opencli",
    )

    with pytest.raises(BrowserBridgeError, match="browser profile"):
        browser.get_account_pipeline("001000000000000AAA")

    assert not any("open" in call for call in runner.calls)


def test_invalid_browser_identity_fails_closed_and_closes_owned_tab() -> None:
    runner = FakeRunner(browser_user_id="invalid-user")

    with pytest.raises(BrowserBridgeError, match="identity"):
        _browser(runner).get_account_pipeline("001000000000000AAA")

    assert any(call[-1:] == ["close"] for call in runner.calls)


def test_account_activities_return_only_typed_dashboard_fields() -> None:
    runner = FakeRunner()

    result = _browser(runner).get_account_activities("001000000000000AAA", limit=10)

    assert result == {
        "upcoming": [
            {
                "subject": "Follow up",
                "status": "Not Started",
                "due": "8/15/2026",
                "assignee": "Owner",
            }
        ],
        "recent": [{"subject": "Call", "date": "8/1/2026", "assignee": "Owner"}],
    }


def test_account_context_batch_reuses_existing_salesforce_tab() -> None:
    runner = FakeRunner()

    result = _browser(runner).get_accounts_context(
        ["001000000000000AAA", "001000000000001AAA"], limit=10
    )

    assert [context["accountId"] for context in result] == [
        "001000000000000AAA",
        "001000000000001AAA",
    ]
    assert [context["opportunities"][0]["accountId"] for context in result] == [
        "001000000000000AAA",
        "001000000000001AAA",
    ]
    command_text = [" ".join(call) for call in runner.calls]
    assert sum(" --window background" in command for command in command_text) == 1
    assert sum("chatter/users/me" in command for command in command_text) == 1
    assert sum("const accountIds=" in command for command in command_text) == 1
    targeted = [
        command
        for command in command_text
        if " eval " in command
        or (" open " in command and "--window background" not in command)
    ]
    assert all("--tab SF-TARGET" in command for command in targeted)
    assert sum(call[-1:] == ["close"] for call in runner.calls) == 0


def test_account_context_batch_closes_session_on_activity_failure() -> None:
    runner = FakeRunner(fail_on_history=True)

    with pytest.raises(BrowserBridgeError, match="synthetic history failure"):
        _browser(runner).get_accounts_context(
            ["001000000000000AAA", "001000000000001AAA"], limit=10
        )

    assert sum(call[-1:] == ["close"] for call in runner.calls) == 1


@pytest.mark.parametrize(
    "account_ids",
    [
        [],
        ["001000000000000AAA", "001000000000000AAA"],
        ["001000000000000AAA"] * 11,
        ["not-an-account"],
    ],
)
def test_account_context_batch_rejects_invalid_input_before_browser(
    account_ids: list[str],
) -> None:
    runner = FakeRunner()

    with pytest.raises(ValueError):
        _browser(runner).get_accounts_context(account_ids, limit=10)

    assert runner.calls == []


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
    )

    with pytest.raises(BrowserBridgeError, match="unavailable"):
        browser.get_account_pipeline("001000000000000AAA")

    assert runner.calls == []
