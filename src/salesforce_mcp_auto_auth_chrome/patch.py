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
"""

from __future__ import annotations

import inspect
import logging

from .auth import read_sid

log = logging.getLogger(__name__)

# Sentinel seeded into SALESFORCE_ACCESS_TOKEN by __main__ when no sid is
# available at startup, so the connector can construct without a real token.
# The per-call patch always overwrites the token with a freshly read sid (or
# raises), so this value must never reach Salesforce — `_patched_call_salesforce`
# rejects it defensively before any request goes out.
PENDING_LOGIN_SENTINEL = "PENDING_CHROME_LOGIN"

# The upstream method we patch. If simple_salesforce changes this signature,
# our patch could pass arguments wrong — we self-check at install time.
_EXPECTED_PARAMS = ("self", "method", "url", "name", "retries", "max_retries")


def install(
    instance_url: str,
    pin_browser: str | None = None,
    pin_profile: str | None = None,
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> None:
    """Install the per-call sid refresh patch.

    Call this exactly once, before importing/starting `mcp-salesforce-connector`.
    The patch persists for the lifetime of the process.

    Per call it reads a fresh sid: first from the pinned browser/profile (the one
    that won startup resolution), then — if that profile no longer has a session
    — falling back to the user-configured browser/profile set. Pinning keeps the
    token source stable so calls don't drift to another profile holding a stale
    sid for the same host.

    Args:
        instance_url: The Salesforce My Domain URL this server is bound to.
        pin_browser: Browser key of the resolved source to prefer per-call.
        pin_profile: Profile name of the resolved source to prefer per-call.
        browsers: User-configured browser keys (fallback search scope).
        profiles: User-configured profile names (fallback search scope).
    """
    import simple_salesforce  # imported here so callers don't pay the cost unless they use this

    _orig_call = simple_salesforce.Salesforce._call_salesforce

    # Self-check: warn loudly if the upstream signature drifted from what we
    # build our call against, so an upstream bump can't silently break auth.
    actual = tuple(inspect.signature(_orig_call).parameters)
    if actual[: len(_EXPECTED_PARAMS)] != _EXPECTED_PARAMS:
        log.warning(
            "simple_salesforce.Salesforce._call_salesforce signature changed "
            "(got %s, expected prefix %s) — the auto-auth patch may misbehave; "
            "check for a simple-salesforce update.",
            actual,
            _EXPECTED_PARAMS,
        )

    # Token-expiry retry: on a 401 (INVALID_SESSION_ID), re-read the sid once
    # and retry — covers a cookie that rotated between our read and the request.
    try:
        from simple_salesforce.exceptions import SalesforceExpiredSession

        _expired_exc: tuple[type[BaseException], ...] = (SalesforceExpiredSession,)
    except Exception:  # noqa: BLE001 — exception class moved/renamed upstream
        _expired_exc = ()

    def _fresh_sid() -> str | None:
        if pin_browser:
            sid = read_sid(
                instance_url,
                [pin_browser],
                [pin_profile] if pin_profile else None,
            )
            if sid:
                return sid
        return read_sid(instance_url, browsers, profiles)

    def _patched_call_salesforce(
        self,
        method: str,
        url: str,
        name: str = "",
        retries: int = 0,
        max_retries: int = 3,
        **kwargs,
    ):
        sid = _fresh_sid()
        if not sid or sid == PENDING_LOGIN_SENTINEL:
            raise RuntimeError(
                f"Not logged into Salesforce in any supported browser for "
                f"{instance_url}. Open that org in your browser, sign in, then "
                f"retry. (No 'sid' cookie found.)"
            )
        # Refresh in place for this call — the connector sees the new headers
        # because _call_salesforce starts with `self.headers.copy()`.
        self.session_id = sid
        self.headers["Authorization"] = "Bearer " + sid
        try:
            return _orig_call(
                self,
                method,
                url,
                name=name,
                retries=retries,
                max_retries=max_retries,
                **kwargs,
            )
        except _expired_exc:
            # Session expired at request time. Re-read the cookie once — if the
            # browser has since refreshed it, retry with the new value.
            fresh = _fresh_sid()
            if not fresh or fresh == sid:
                raise
            log.info("session expired mid-call; retrying with refreshed sid")
            self.session_id = fresh
            self.headers["Authorization"] = "Bearer " + fresh
            return _orig_call(
                self,
                method,
                url,
                name=name,
                retries=retries,
                max_retries=max_retries,
                **kwargs,
            )

    simple_salesforce.Salesforce._call_salesforce = _patched_call_salesforce
