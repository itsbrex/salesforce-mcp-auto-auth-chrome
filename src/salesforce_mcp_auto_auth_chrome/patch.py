"""Monkey-patch `simple_salesforce` to refresh the session id on every API call.

Why monkey-patch?
-----------------
`mcp-salesforce-connector` uses `simple_salesforce.Salesforce` under the hood.
That library reads `session_id` once at construction time and builds a
`headers["Authorization"]` value from it. If the session expires mid-run, the
MCP server is stuck with a stale token until restart.

Rather than fork `mcp-salesforce-connector`, we patch the central HTTP method
(`Salesforce._call_salesforce`) so that every outgoing request first reads a
fresh `sid` from Chrome and overwrites the headers. This is invisible to the
rest of the connector.

If no session is present in Chrome at call time, we raise a `RuntimeError`
with a friendly message — this bubbles up through the connector and surfaces
in the chat as a tool error, which is much nicer than a startup crash.

The moving parts (`_fresh_sid`, the signature self-check, the expired-session
retry) live at module level and take their configuration via `_PatchConfig`, so
each is unit-testable without monkey-patching a live `Salesforce` class.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .cookies import read_sid
from .useragent import resolve_user_agent

log = logging.getLogger(__name__)

__all__ = ["install", "PENDING_LOGIN_SENTINEL"]

# Sentinel seeded into SALESFORCE_ACCESS_TOKEN by __main__ when no sid is
# available at startup, so the connector can construct without a real token.
# The per-call patch always overwrites the token with a freshly read sid (or
# raises), so this value must never reach Salesforce — `_patched_call` rejects
# it defensively before any request goes out.
PENDING_LOGIN_SENTINEL = "PENDING_CHROME_LOGIN"

# The upstream method we patch. If simple_salesforce changes this signature,
# our patch could pass arguments wrong — we self-check at install time.
_EXPECTED_PARAMS = ("self", "method", "url", "name", "retries", "max_retries")


@dataclass(frozen=True)
class _PatchConfig:
    """Resolved per-call sid lookup configuration captured at install time."""

    instance_url: str
    pin_browser: str | None
    pin_profile: str | None
    browsers: list[str] | None
    profiles: list[str] | None
    user_agent: str


def _fresh_sid(cfg: _PatchConfig) -> str | None:
    """Read a fresh sid, kept pinned to the browser/profile resolved at startup.

    Pinning keeps the token source stable so calls don't drift to another
    profile. Critically, when a source is pinned we do NOT expand the search on
    a miss: if the pinned profile has logged out while a *different* profile is
    logged into the same org as another user, broadening the scope would hand
    that other user's ``sid`` to every connector operation — including writes —
    running them under the wrong principal and audit identity. A logged-out pin
    returns ``None`` (pending-login) instead. The unpinned scope is used only
    when no source was pinned at startup.
    """
    if cfg.pin_browser:
        return read_sid(
            cfg.instance_url,
            [cfg.pin_browser],
            [cfg.pin_profile] if cfg.pin_profile else None,
        )
    return read_sid(cfg.instance_url, cfg.browsers, cfg.profiles)


def _apply_sid(sf: object, sid: str) -> None:
    """Overwrite the live session id + Authorization header for this call.

    The connector sees the new headers because `_call_salesforce` starts with
    `self.headers.copy()`.
    """
    sf.session_id = sid  # type: ignore[attr-defined]
    sf.headers["Authorization"] = "Bearer " + sid  # type: ignore[attr-defined]


def _signature_ok(orig_call: Callable[..., Any]) -> bool:
    """Warn (and return False) if the upstream method signature drifted.

    A silent upstream rename/reorder of `_call_salesforce`'s parameters would
    make us pass arguments wrong; surfacing it loudly turns a mystery auth
    failure into an actionable "check for a simple-salesforce update" hint.
    """
    actual = tuple(inspect.signature(orig_call).parameters)
    if actual[: len(_EXPECTED_PARAMS)] == _EXPECTED_PARAMS:
        return True
    log.warning(
        "simple_salesforce.Salesforce._call_salesforce signature changed "
        "(got %s, expected prefix %s) — the auto-auth patch may misbehave; "
        "check for a simple-salesforce update.",
        actual,
        _EXPECTED_PARAMS,
    )
    return False


def _resolve_expired_exc() -> tuple[type[BaseException], ...]:
    """Return the SalesforceExpiredSession type, or () if it moved/renamed.

    `except ():` never matches, so an upstream relocation simply disables the
    retry path instead of crashing at import.
    """
    try:
        from simple_salesforce.exceptions import SalesforceExpiredSession

        return (SalesforceExpiredSession,)
    except Exception:  # noqa: BLE001 — exception class moved/renamed upstream
        return ()


def _patched_call(
    cfg: _PatchConfig,
    orig_call: Callable[..., Any],
    expired_exc: tuple[type[BaseException], ...],
    sf: Any,
    method: str,
    url: str,
    name: str,
    retries: int,
    max_retries: int,
    kwargs: dict[str, Any],
) -> Any:
    """Refresh the sid, delegate to the original call, retry once on expiry."""
    sid = _fresh_sid(cfg)
    if not sid or sid == PENDING_LOGIN_SENTINEL:
        raise RuntimeError(
            f"Not logged into Salesforce in any supported browser for "
            f"{cfg.instance_url}. Open that org in your browser, sign in, then "
            f"retry. (No 'sid' cookie found.)"
        )
    _apply_sid(sf, sid)
    # Send a real browser User-Agent instead of requests' `python-requests/x.y`
    # default, so this traffic matches the browser whose session we're using
    # rather than fingerprinting as a bot. Set per call (the connector copies
    # `self.headers` on every request); persists across the expiry retry below.
    sf.headers["User-Agent"] = cfg.user_agent
    try:
        return orig_call(
            sf,
            method,
            url,
            name=name,
            retries=retries,
            max_retries=max_retries,
            **kwargs,
        )
    except expired_exc:
        # Session expired at request time. Re-read the cookie once — if the
        # browser has since refreshed it, retry with the new value.
        fresh = _fresh_sid(cfg)
        if not fresh or fresh == sid:
            raise
        log.info("session expired mid-call; retrying with refreshed sid")
        _apply_sid(sf, fresh)
        return orig_call(
            sf,
            method,
            url,
            name=name,
            retries=retries,
            max_retries=max_retries,
            **kwargs,
        )


def install(
    instance_url: str,
    pin_browser: str | None = None,
    pin_profile: str | None = None,
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
    user_agent: str | None = None,
) -> None:
    """Install the per-call sid refresh patch.

    Call this exactly once, before importing/starting `mcp-salesforce-connector`.
    The patch persists for the lifetime of the process.

    Args:
        instance_url: The Salesforce My Domain URL this server is bound to.
        pin_browser: Browser key of the resolved source to prefer per-call.
        pin_profile: Profile name of the resolved source to prefer per-call.
        browsers: User-configured browser keys (fallback search scope).
        profiles: User-configured profile names (fallback search scope).
        user_agent: Browser User-Agent to send on every API call. Defaults to
            `resolve_user_agent()` (env override → live browser → fallback);
            pass explicitly to bypass the live probe (e.g. in tests).
    """
    import simple_salesforce  # local import keeps cost off the no-op path

    ua = user_agent if user_agent is not None else resolve_user_agent()
    cfg = _PatchConfig(instance_url, pin_browser, pin_profile, browsers, profiles, ua)
    orig_call = simple_salesforce.Salesforce._call_salesforce  # type: ignore[attr-defined]
    _signature_ok(orig_call)
    expired_exc = _resolve_expired_exc()

    def _replacement(
        self: Any,
        method: str,
        url: str,
        name: str = "",
        retries: int = 0,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> Any:
        return _patched_call(
            cfg,
            orig_call,
            expired_exc,
            self,
            method,
            url,
            name,
            retries,
            max_retries,
            kwargs,
        )

    simple_salesforce.Salesforce._call_salesforce = _replacement  # type: ignore[attr-defined,method-assign]
