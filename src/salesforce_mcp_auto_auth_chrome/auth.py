"""Deprecated back-compat surface for reading the Salesforce `sid` cookie.

The live code path reads sids via `cookies.read_sid` directly — nothing in this
package imports from here anymore. This module survives only to keep two public
names importable for external callers written against the old layout:

- `read_sid` — re-exported from `cookies` (its current home).
- `read_sid_from_chrome` — the single-browser shim, deprecated and scheduled for
  removal in the next minor release.

Both are slated to go with `auth.py` itself at the 0.2.0 bump.
"""

from __future__ import annotations

import warnings

from .cookies import read_sid

__all__ = ["read_sid", "read_sid_from_chrome"]


def read_sid_from_chrome(instance_url: str) -> str | None:
    """Back-compat shim: read the `sid` from Chrome's default-priority profiles.

    .. deprecated::
        Use :func:`read_sid` for multi-browser support. This shim is scheduled
        for removal in the next minor release.
    """
    warnings.warn(
        "read_sid_from_chrome is deprecated; use read_sid (multi-browser "
        "support) instead. This shim will be removed in the next minor release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return read_sid(instance_url, browsers=["chrome"])
