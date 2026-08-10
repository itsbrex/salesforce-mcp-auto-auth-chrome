from __future__ import annotations

import json

from salesforce_mcp_auto_auth_chrome import session_status
from salesforce_mcp_auto_auth_chrome.orgs import ResolvedSession


def test_read_session_status_requires_live_salesforce_validation(monkeypatch) -> None:
    monkeypatch.setattr(
        session_status,
        "resolve_session",
        lambda *_args: ResolvedSession(
            "https://secret.my.salesforce.com",
            "SID_SECRET",
            "comet",
            "Secret Profile",
        ),
    )
    monkeypatch.setattr(session_status, "session_is_valid", lambda *_args: True)

    assert session_status.read_session_status(None, ["comet"], None) == {
        "state": "active"
    }


def test_read_session_status_is_inactive_without_valid_sid(monkeypatch) -> None:
    monkeypatch.setattr(
        session_status,
        "resolve_session",
        lambda *_args: ResolvedSession("https://acme.my.salesforce.com", None),
    )
    assert session_status.read_session_status(None, None, None) == {
        "state": "inactive"
    }

    monkeypatch.setattr(
        session_status,
        "resolve_session",
        lambda *_args: ResolvedSession(
            "https://acme.my.salesforce.com", "EXPIRED_SID", "comet", "Default"
        ),
    )
    monkeypatch.setattr(session_status, "session_is_valid", lambda *_args: False)
    assert session_status.read_session_status(None, None, None) == {
        "state": "inactive"
    }


def test_main_outputs_only_sanitized_state(monkeypatch, capsys) -> None:
    secrets = [
        "SID_SECRET",
        "secret.my.salesforce.com",
        "Secret Profile",
        "secret@example.invalid",
    ]
    monkeypatch.setattr(
        session_status,
        "resolve_session",
        lambda *_args: ResolvedSession(
            "https://secret.my.salesforce.com",
            "SID_SECRET",
            "comet",
            "Secret Profile",
        ),
    )
    monkeypatch.setattr(session_status, "session_is_valid", lambda *_args: True)

    assert session_status.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"state": "active"}
    assert captured.err == ""
    assert all(secret not in captured.out for secret in secrets)
