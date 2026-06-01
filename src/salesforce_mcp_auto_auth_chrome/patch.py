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

from .auth import read_sid_from_chrome


def install(instance_url: str) -> None:
    """Install the per-call sid refresh patch.

    Call this exactly once, before importing/starting `mcp-salesforce-connector`.
    The patch persists for the lifetime of the process.

    Args:
        instance_url: The Salesforce My Domain URL this server is bound to.
            Used to look up the right `sid` cookie in Chrome.
    """
    import simple_salesforce  # imported here so callers don't pay the cost unless they use this

    _orig_call = simple_salesforce.Salesforce._call_salesforce

    def _patched_call_salesforce(
        self,
        method: str,
        url: str,
        name: str = "",
        retries: int = 0,
        max_retries: int = 3,
        **kwargs,
    ):
        sid = read_sid_from_chrome(instance_url)
        if not sid:
            raise RuntimeError(
                f"Not logged into Salesforce in Chrome for {instance_url}. "
                f"Open that org in Chrome, sign in, then retry. "
                f"(No 'sid' cookie found.)"
            )
        # Refresh in place for this call — the connector sees the new headers
        # because _call_salesforce starts with `self.headers.copy()`.
        self.session_id = sid
        self.headers["Authorization"] = "Bearer " + sid
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
