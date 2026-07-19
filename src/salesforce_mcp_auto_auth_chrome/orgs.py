"""Resolve which Salesforce org to bind to from discovered cookie sessions.

Sits one level above ``cookies.py``: where that module reads a ``sid`` for a
*known* host, this one decides *which* org to use when the host isn't given —
scanning every browser/profile for logged-in Salesforce sessions, mapping each
to its My Domain REST URL, applying the ranking rule (a ``sid`` set on a
``my.salesforce.com`` host outranks a Lightning-derived one the REST API
rejects), and validating candidates against the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .browsers import CookieSource, discover_sources, reader_for
from .cookies import read_sid_with_source
from .instance import normalize_instance_url
from .validate import session_is_valid

log = logging.getLogger(__name__)

__all__ = [
    "OrgCandidate",
    "ResolvedSession",
    "discover_orgs",
    "resolve_session",
]


@dataclass(frozen=True)
class OrgCandidate:
    """A discovered Salesforce session: a My Domain REST URL + its sid."""

    instance_url: str
    sid: str
    browser: str
    profile: str


@dataclass(frozen=True)
class ResolvedSession:
    """The org bound at startup, plus the source to pin per-call refresh to.

    ``browser``/``profile`` identify the exact cookie store the initial sid came
    from, so the per-call refresh can keep reading from the *same* profile
    instead of drifting to another profile that happens to hold a stale sid for
    the same host.
    """

    instance_url: str | None
    sid: str | None
    browser: str | None = None
    profile: str | None = None


def discover_orgs(
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> list[OrgCandidate]:
    """Scan all cookie sources for logged-in Salesforce orgs.

    Each Salesforce ``sid`` cookie is mapped to its My Domain REST URL. Returns
    deduped candidates in priority order, with sids whose cookie was already set
    on a ``my.salesforce.com`` host ranked ahead of Lightning-derived ones (the
    latter carry a different sid that the REST API rejects).
    """
    primary: list[OrgCandidate] = []
    derived: list[OrgCandidate] = []
    seen: set[tuple[str, str]] = set()
    for source in discover_sources(browsers, profiles):
        try:
            pairs = _list_sids_from_source(source)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "listing sids failed for %s/%s: %s: %s",
                source.browser,
                source.profile,
                type(e).__name__,
                e,
            )
            continue
        for host_key, sid in pairs:
            instance_url = normalize_instance_url(host_key)
            if not instance_url:
                continue
            host = instance_url[len("https://") :]
            if not host.endswith("salesforce.com"):
                continue  # Lightning/VF hosts that didn't map to a My Domain
            key = (instance_url, sid)
            if key in seen:
                continue
            seen.add(key)
            candidate = OrgCandidate(instance_url, sid, source.browser, source.profile)
            bare = host_key.lstrip(".").lower()
            (primary if bare.endswith("salesforce.com") else derived).append(candidate)
    return primary + derived


def resolve_session(
    configured_url: str | None,
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> ResolvedSession:
    """Resolve the org to bind to, an initial ``sid``, and the source to pin.

    Strategy:

    1. If ``configured_url`` is set, normalize it to My Domain and try to read a
       ``sid`` for it directly (trusted — no network probe), recording which
       browser/profile it came from.
    2. Otherwise (or if step 1 found no sid), auto-discover orgs from cookies and
       return the first whose sid validates against the REST API.
    3. If nothing validates, return the normalized configured URL (if any) with
       a ``None`` sid so the error surfaces at tool-call time.
    """
    normalized = normalize_instance_url(configured_url) if configured_url else None
    if normalized:
        found = read_sid_with_source(normalized, browsers, profiles)
        if found:
            sid, browser, profile = found
            return ResolvedSession(normalized, sid, browser, profile)

    for candidate in discover_orgs(browsers, profiles):
        if session_is_valid(candidate.instance_url, candidate.sid):
            log.info(
                "auto-discovered org %s via %s/%s",
                candidate.instance_url,
                candidate.browser,
                candidate.profile,
            )
            return ResolvedSession(
                candidate.instance_url,
                candidate.sid,
                candidate.browser,
                candidate.profile,
            )

    return ResolvedSession(normalized, None, None, None)


def _list_sids_from_source(source: CookieSource) -> list[tuple[str, str]]:
    return reader_for(source.family).list_sids(source)
