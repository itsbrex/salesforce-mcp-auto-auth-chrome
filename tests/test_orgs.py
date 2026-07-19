from __future__ import annotations

from pathlib import Path

from salesforce_mcp_auto_auth_chrome import chromium, orgs
from salesforce_mcp_auto_auth_chrome.browsers import CookieSource


def _src(browser: str, family: str) -> CookieSource:
    return CookieSource(browser, "Default", family, Path("/x"), None, None)


# --- discover_orgs -----------------------------------------------------------


def test_discover_orgs_maps_lightning_and_ranks_my_domain_first(monkeypatch):
    src = _src("chrome", "chromium")
    monkeypatch.setattr(orgs, "discover_sources", lambda b, p: [src])
    monkeypatch.setattr(
        chromium.READER,
        "list_sids",
        lambda s: [
            (".acme.lightning.force.com", "LIGHTNING_SID"),
            (".acme.my.salesforce.com", "MY_SID"),
        ],
    )

    discovered = orgs.discover_orgs()

    pairs = [(o.instance_url, o.sid) for o in discovered]
    assert pairs[0] == ("https://acme.my.salesforce.com", "MY_SID")  # primary first
    assert ("https://acme.my.salesforce.com", "LIGHTNING_SID") in pairs


def test_discover_orgs_dedupes(monkeypatch):
    src = _src("chrome", "chromium")
    monkeypatch.setattr(orgs, "discover_sources", lambda b, p: [src])
    monkeypatch.setattr(
        chromium.READER,
        "list_sids",
        lambda s: [(".acme.my.salesforce.com", "SID")] * 3,
    )

    assert len(orgs.discover_orgs()) == 1


# --- resolve_session ---------------------------------------------------------


def test_resolve_configured_normalizes_and_pins_source(monkeypatch):
    monkeypatch.setattr(
        orgs,
        "read_sid_with_source",
        lambda u, b, p: (
            ("SID", "comet", "Profile 1")
            if u == "https://acme.my.salesforce.com"
            else None
        ),
    )

    r = orgs.resolve_session("https://acme.lightning.force.com")

    assert r.instance_url == "https://acme.my.salesforce.com"
    assert r.sid == "SID"
    assert (r.browser, r.profile) == ("comet", "Profile 1")


def test_resolve_autodiscovers_first_valid_with_source(monkeypatch):
    monkeypatch.setattr(orgs, "read_sid_with_source", lambda u, b, p: None)
    cands = [
        orgs.OrgCandidate(
            "https://acme.my.salesforce.com", "LIGHTNING", "chrome", "Profile 6"
        ),
        orgs.OrgCandidate(
            "https://acme.my.salesforce.com", "GOOD", "chrome", "Profile 4"
        ),
    ]
    monkeypatch.setattr(orgs, "discover_orgs", lambda b, p: cands)
    monkeypatch.setattr(orgs, "session_is_valid", lambda u, s: s == "GOOD")

    r = orgs.resolve_session(None)

    assert (r.instance_url, r.sid) == ("https://acme.my.salesforce.com", "GOOD")
    assert (r.browser, r.profile) == ("chrome", "Profile 4")


def test_resolve_returns_empty_when_nothing_found(monkeypatch):
    monkeypatch.setattr(orgs, "read_sid_with_source", lambda u, b, p: None)
    monkeypatch.setattr(orgs, "discover_orgs", lambda b, p: [])

    r = orgs.resolve_session(None)
    assert (r.instance_url, r.sid) == (None, None)


def test_resolve_defers_error_for_configured_without_session(monkeypatch):
    monkeypatch.setattr(orgs, "read_sid_with_source", lambda u, b, p: None)
    monkeypatch.setattr(orgs, "discover_orgs", lambda b, p: [])

    r = orgs.resolve_session("https://acme.lightning.force.com")

    assert r.instance_url == "https://acme.my.salesforce.com"
    assert r.sid is None
