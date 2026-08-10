from __future__ import annotations

import json

from salesforce_mcp_auto_auth_chrome import session_status


def test_read_session_status_requires_live_salesforce_validation(monkeypatch) -> None:
    class FakeBrowser:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def session_is_active(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(session_status, "SalesforceBrowser", FakeBrowser)

    assert session_status.read_session_status(
        "https://secret.my.salesforce.com", ["comet"], None
    ) == {"state": "active"}


def test_read_session_status_is_inactive_without_valid_sid(monkeypatch) -> None:
    assert session_status.read_session_status(None, None, None) == {"state": "inactive"}

    class InactiveBrowser:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def session_is_active(self) -> bool:
            return False

    monkeypatch.setattr(session_status, "SalesforceBrowser", InactiveBrowser)
    assert session_status.read_session_status(None, None, None) == {"state": "inactive"}


def test_main_outputs_only_sanitized_state(monkeypatch, capsys) -> None:
    secrets = [
        "SID_SECRET",
        "secret.my.salesforce.com",
        "Secret Profile",
        "secret@example.invalid",
    ]

    class FakeBrowser:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def session_is_active(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(session_status, "SalesforceBrowser", FakeBrowser)

    assert (
        session_status.main(
            {
                "SALESFORCE_INSTANCE_URL": "https://secret.my.salesforce.com",
                "SALESFORCE_BROWSERS": "comet",
            }
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"state": "active"}
    assert captured.err == ""
    assert all(secret not in captured.out for secret in secrets)
