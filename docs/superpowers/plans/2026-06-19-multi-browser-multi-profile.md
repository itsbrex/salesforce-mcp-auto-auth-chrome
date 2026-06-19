# Multi-Browser, Multi-Profile Salesforce Session Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-Chrome-default-profile `sid` lookup with a native cookie reader that scans multiple browsers (Chrome, Comet, Arc, Edge, Brave, Firefox, Safari) and every profile within each, returning the first valid Salesforce `sid`.

**Architecture:** Port GoFi's manual cookie-decryption approach into native Python (no `pycookiecheat`). A browser registry describes each browser's on-disk data dir, family (`chromium`/`firefox`/`safari`), and macOS Keychain service. Discovery enumerates profiles per browser into `CookieSource` records. Family-specific readers extract the `sid` cookie: Chromium via SQLite + Keychain + AES-128-CBC, Firefox via plaintext SQLite, Safari via a `.binarycookies` parser. An orchestrator iterates sources in priority order and returns the first non-empty `sid`. Optional env vars restrict/override which browsers and profiles are searched.

**Tech Stack:** Python 3.12+, `cryptography` (AES-128-CBC), stdlib `hashlib` (PBKDF2-SHA1), `sqlite3`, `subprocess` (`security` Keychain CLI), `struct` (binarycookies). `uv` for env/deps. `pytest` for tests.

## Global Constraints

- **Platform:** macOS only. All paths and Keychain logic target macOS. (Same scope as today — `pyproject.toml:28`.)
- **Python:** `requires-python = ">=3.12"`. Use `from __future__ import annotations` at the top of every module (repo convention).
- **Error policy:** Cookie reading NEVER raises to callers. Any failure (missing file, locked Keychain, decrypt error, permission denied) is logged and yields `None`/skips the source, so startup never crashes and errors surface at tool-call time. (See `auth.py:7-11` docstring.)
- **Style:** Google-style docstrings with Args/Returns. Module-level `log = logging.getLogger(__name__)`. Small focused modules, pure functions, full type hints. No classes except frozen dataclasses.
- **Type hints:** Use `str | None` unions (PEP 604), not `Optional`.
- **No `rm`:** never shell out to `rm`. Tests use `tmp_path`/`TemporaryDirectory`.
- **Verified facts** (probed on this machine, 2026-06-19) — use these exact values:
  - Chromium cookie schema columns: `host_key`, `name`, `value`, `encrypted_value`, `last_access_utc`.
  - macOS Chromium decryption: prefix `v10`, key = `PBKDF2-HMAC-SHA1(password, salt=b"saltysalt", iterations=1003, dklen=16)`, cipher = AES-128-CBC with IV = 16 × `0x20` (space) bytes. Newer Chrome prepends a 32-byte `SHA256(host_key)` to the plaintext — strip it only when it matches.
  - Keychain services found: `Chrome Safe Storage`/`Chrome`, `Comet Safe Storage`/`Comet`, `Arc Safe Storage`/`Arc`, `Microsoft Edge Safe Storage`/`Microsoft Edge`, `Brave Safe Storage`/`Brave`.
  - Profile dirs (macOS): chromium profiles are `Default` and `Profile N` under the data dir, each containing a `Cookies` file directly. Exclude `System Profile` and `Guest Profile`.
  - Data dirs: Chrome `~/Library/Application Support/Google/Chrome`; Comet `~/Library/Application Support/Comet`; Arc `~/Library/Application Support/Arc/User Data`; Edge `~/Library/Application Support/Microsoft Edge`; Brave `~/Library/Application Support/BraveSoftware/Brave-Browser`; Firefox `~/Library/Application Support/Firefox/Profiles`; Safari `~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies`.

---

## File Structure

- `src/salesforce_mcp_auto_auth_chrome/browsers.py` — **new.** `BrowserConfig`, `CookieSource` dataclasses, the `BROWSERS` registry (priority order), and `discover_sources()` profile enumeration.
- `src/salesforce_mcp_auto_auth_chrome/chromium.py` — **new.** Chromium-family cookie reading: SQLite query, Keychain password fetch, AES-128-CBC decryption.
- `src/salesforce_mcp_auto_auth_chrome/firefox.py` — **new.** Firefox plaintext `moz_cookies` reading.
- `src/salesforce_mcp_auto_auth_chrome/safari.py` — **new.** `.binarycookies` parser + cookie lookup.
- `src/salesforce_mcp_auto_auth_chrome/cookies.py` — **new.** Orchestrator `read_sid()`, host-suffix matching, env-var parsing.
- `src/salesforce_mcp_auto_auth_chrome/auth.py` — **modify.** Re-export `read_sid`; keep `read_sid_from_chrome` as a backward-compatible alias.
- `src/salesforce_mcp_auto_auth_chrome/__main__.py` — **modify.** Parse `SALESFORCE_BROWSERS` / `SALESFORCE_PROFILES`, pass through to read + patch.
- `src/salesforce_mcp_auto_auth_chrome/patch.py` — **modify.** Accept `browsers`/`profiles`, call `read_sid`.
- `pyproject.toml` — **modify.** Drop `pycookiecheat`, add `cryptography`, add dev group with `pytest`.
- `tests/` — **new.** Unit tests for each module.
- `README.md`, `docs/how-it-works.md` — **modify.** Document multi-browser/profile support + env vars + Safari Full Disk Access.

---

### Task 1: Project setup — dependencies & test scaffold

**Files:**
- Modify: `pyproject.toml:34-38` (dependencies), append dev group + pytest config
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cryptography` importable; `pytest` runnable via `uv run pytest`.

- [ ] **Step 1: Replace runtime dependencies**

In `pyproject.toml`, replace lines 34-38:

```toml
dependencies = [
    "mcp-salesforce-connector>=0.1.15",
    "cryptography>=42",
    "simple-salesforce>=1.12",
]
```

- [ ] **Step 2: Add dev dependency group + pytest config**

Append to the end of `pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "pytest>=8",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

- [ ] **Step 3: Create the test package**

Create `tests/__init__.py` (empty file):

```python
```

Create `tests/conftest.py`:

```python
"""Shared test helpers for the cookie-reader test suite."""
from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def encrypt_chromium_value(
    value: str, password: str, host_key: str, *, with_host_hash: bool = False
) -> bytes:
    """Produce a macOS-Chromium `v10` encrypted_value blob for `value`.

    Mirrors the decryption algorithm under test: PBKDF2-SHA1 key derivation,
    AES-128-CBC with a 16-space IV, optional 32-byte SHA256(host_key) prefix.
    """
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)
    plain = value.encode("utf-8")
    if with_host_hash:
        plain = hashlib.sha256(host_key.encode("utf-8")).digest() + plain
    pad = 16 - (len(plain) % 16)
    plain += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    enc = cipher.encryptor()
    return b"v10" + enc.update(plain) + enc.finalize()
```

- [ ] **Step 4: Sync and verify pytest collects nothing yet**

Run: `uv sync && uv run pytest`
Expected: exit 0, "no tests ran" (collected 0 items). `cryptography` installs cleanly; `pycookiecheat` is gone.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/conftest.py
git commit -m "chore: swap pycookiecheat for cryptography, add pytest scaffold"
```

---

### Task 2: Browser registry & profile discovery

**Files:**
- Create: `src/salesforce_mcp_auto_auth_chrome/browsers.py`
- Test: `tests/test_browsers.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BrowserConfig(key: str, family: str, base_dir: Path, keychain_service: str | None, keychain_account: str | None)` — frozen dataclass.
  - `CookieSource(browser: str, profile: str, family: str, path: Path, keychain_service: str | None, keychain_account: str | None)` — frozen dataclass.
  - `BROWSERS: tuple[BrowserConfig, ...]` — priority-ordered registry.
  - `discover_sources(browsers: list[str] | None = None, profiles: list[str] | None = None) -> list[CookieSource]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_browsers.py`:

```python
from __future__ import annotations

from pathlib import Path

from salesforce_mcp_auto_auth_chrome import browsers
from salesforce_mcp_auto_auth_chrome.browsers import BrowserConfig, discover_sources


def _fake_chromium(tmp_path: Path) -> BrowserConfig:
    base = tmp_path / "Chrome"
    for name in ("Default", "Profile 1", "System Profile"):
        (base / name).mkdir(parents=True)
        (base / name / "Cookies").write_bytes(b"")
    return BrowserConfig("chrome", "chromium", base, "Chrome Safe Storage", "Chrome")


def test_discover_enumerates_chromium_profiles_excluding_system(tmp_path, monkeypatch):
    cfg = _fake_chromium(tmp_path)
    monkeypatch.setattr(browsers, "BROWSERS", (cfg,))

    sources = discover_sources()

    names = sorted(s.profile for s in sources)
    assert names == ["Default", "Profile 1"]
    assert all(s.browser == "chrome" and s.family == "chromium" for s in sources)
    assert all(s.path.name == "Cookies" for s in sources)


def test_discover_filters_by_browser_key_and_order(tmp_path, monkeypatch):
    chrome = _fake_chromium(tmp_path)
    comet_base = tmp_path / "Comet"
    (comet_base / "Default").mkdir(parents=True)
    (comet_base / "Default" / "Cookies").write_bytes(b"")
    comet = BrowserConfig("comet", "chromium", comet_base, "Comet Safe Storage", "Comet")
    monkeypatch.setattr(browsers, "BROWSERS", (chrome, comet))

    sources = discover_sources(browsers=["comet", "chrome"])

    assert sources[0].browser == "comet"  # requested order wins
    assert {s.browser for s in sources} == {"comet", "chrome"}


def test_discover_filters_by_profile_name(tmp_path, monkeypatch):
    monkeypatch.setattr(browsers, "BROWSERS", (_fake_chromium(tmp_path),))

    sources = discover_sources(profiles=["default"])  # case-insensitive

    assert [s.profile for s in sources] == ["Default"]


def test_discover_skips_missing_base_dir(tmp_path, monkeypatch):
    cfg = BrowserConfig("chrome", "chromium", tmp_path / "nope", "S", "A")
    monkeypatch.setattr(browsers, "BROWSERS", (cfg,))

    assert discover_sources() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browsers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'salesforce_mcp_auto_auth_chrome.browsers'`

- [ ] **Step 3: Implement `browsers.py`**

Create `src/salesforce_mcp_auto_auth_chrome/browsers.py`:

```python
"""Browser registry and profile discovery for cookie reading (macOS).

Each supported browser is described by a `BrowserConfig`. Discovery walks each
browser's on-disk data directory and yields one `CookieSource` per profile that
actually contains a cookie store. The registry order is the search priority.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_HOME = Path.home()
_APP_SUPPORT = _HOME / "Library" / "Application Support"


@dataclass(frozen=True)
class BrowserConfig:
    """Static description of a browser's cookie storage on macOS.

    Args:
        key: Lowercase identifier, e.g. ``"chrome"``.
        family: One of ``"chromium"``, ``"firefox"``, ``"safari"`` — selects
            the reader used for this browser's cookie store.
        base_dir: The browser's data directory (profiles live under it for
            chromium/firefox; for safari this is the Cookies directory).
        keychain_service: macOS Keychain service holding the Safe Storage key
            (chromium only; ``None`` for firefox/safari).
        keychain_account: macOS Keychain account for the Safe Storage key.
    """

    key: str
    family: str
    base_dir: Path
    keychain_service: str | None = None
    keychain_account: str | None = None


@dataclass(frozen=True)
class CookieSource:
    """A single resolved cookie store (one browser profile)."""

    browser: str
    profile: str
    family: str
    path: Path
    keychain_service: str | None
    keychain_account: str | None


# Priority order: first match wins in the orchestrator.
BROWSERS: tuple[BrowserConfig, ...] = (
    BrowserConfig(
        "chrome", "chromium", _APP_SUPPORT / "Google" / "Chrome",
        "Chrome Safe Storage", "Chrome",
    ),
    BrowserConfig(
        "comet", "chromium", _APP_SUPPORT / "Comet",
        "Comet Safe Storage", "Comet",
    ),
    BrowserConfig(
        "arc", "chromium", _APP_SUPPORT / "Arc" / "User Data",
        "Arc Safe Storage", "Arc",
    ),
    BrowserConfig(
        "edge", "chromium", _APP_SUPPORT / "Microsoft Edge",
        "Microsoft Edge Safe Storage", "Microsoft Edge",
    ),
    BrowserConfig(
        "brave", "chromium",
        _APP_SUPPORT / "BraveSoftware" / "Brave-Browser",
        "Brave Safe Storage", "Brave",
    ),
    BrowserConfig(
        "firefox", "firefox", _APP_SUPPORT / "Firefox" / "Profiles",
    ),
    BrowserConfig(
        "safari", "safari",
        _HOME / "Library" / "Containers" / "com.apple.Safari" / "Data"
        / "Library" / "Cookies",
    ),
)

_CHROMIUM_SKIP = {"System Profile", "Guest Profile"}


def _chromium_sources(cfg: BrowserConfig) -> list[CookieSource]:
    out: list[CookieSource] = []
    if not cfg.base_dir.is_dir():
        return out
    for child in sorted(cfg.base_dir.iterdir()):
        if not child.is_dir() or child.name in _CHROMIUM_SKIP:
            continue
        if child.name != "Default" and not child.name.startswith("Profile "):
            continue
        # Newer Chromium uses <profile>/Network/Cookies; macOS today uses
        # <profile>/Cookies. Prefer Network/ when present.
        cookies = child / "Network" / "Cookies"
        if not cookies.is_file():
            cookies = child / "Cookies"
        if cookies.is_file():
            out.append(CookieSource(
                cfg.key, child.name, cfg.family, cookies,
                cfg.keychain_service, cfg.keychain_account,
            ))
    return out


def _firefox_sources(cfg: BrowserConfig) -> list[CookieSource]:
    out: list[CookieSource] = []
    if not cfg.base_dir.is_dir():
        return out
    for child in sorted(cfg.base_dir.iterdir()):
        cookies = child / "cookies.sqlite"
        if child.is_dir() and cookies.is_file():
            out.append(CookieSource(
                cfg.key, child.name, cfg.family, cookies, None, None,
            ))
    return out


def _safari_sources(cfg: BrowserConfig) -> list[CookieSource]:
    cookies = cfg.base_dir / "Cookies.binarycookies"
    if cookies.is_file():
        return [CookieSource(cfg.key, "default", cfg.family, cookies, None, None)]
    return []


def discover_sources(
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> list[CookieSource]:
    """Enumerate cookie stores across browsers and profiles.

    Args:
        browsers: Optional lowercase browser keys to restrict to, in priority
            order. ``None`` means all registered browsers in registry order.
        profiles: Optional profile names to restrict to (case-insensitive).
            ``None`` means all discovered profiles.

    Returns:
        Resolved `CookieSource` records, in search priority order.
    """
    if browsers is not None:
        wanted = [b.lower() for b in browsers]
        by_key = {cfg.key: cfg for cfg in BROWSERS}
        configs = [by_key[k] for k in wanted if k in by_key]
    else:
        configs = list(BROWSERS)

    profile_filter = {p.lower() for p in profiles} if profiles else None

    sources: list[CookieSource] = []
    for cfg in configs:
        if cfg.family == "chromium":
            found = _chromium_sources(cfg)
        elif cfg.family == "firefox":
            found = _firefox_sources(cfg)
        elif cfg.family == "safari":
            found = _safari_sources(cfg)
        else:  # pragma: no cover — registry is closed
            found = []
        if profile_filter is not None:
            found = [s for s in found if s.profile.lower() in profile_filter]
        sources.extend(found)
    return sources
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_browsers.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/browsers.py tests/test_browsers.py
git commit -m "feat: add browser registry and multi-profile discovery"
```

---

### Task 3: Chromium cookie reader & decryption

**Files:**
- Create: `src/salesforce_mcp_auto_auth_chrome/chromium.py`
- Test: `tests/test_chromium.py`

**Interfaces:**
- Consumes: `CookieSource` from `browsers`.
- Produces: `read_chromium_cookie(source: CookieSource, host: str, name: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chromium.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from salesforce_mcp_auto_auth_chrome import chromium
from salesforce_mcp_auto_auth_chrome.browsers import CookieSource
from tests.conftest import encrypt_chromium_value

_PASSWORD = "test-safe-storage-key"


def _make_db(path: Path, rows: list[tuple[str, str, bytes]]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB)"
    )
    con.executemany(
        "INSERT INTO cookies (host_key, name, value, encrypted_value) "
        "VALUES (?, ?, ?, ?)",
        [(h, "sid", v, e) for (h, v, e) in rows],
    )
    con.commit()
    con.close()


def _source(path: Path) -> CookieSource:
    return CookieSource(
        "chrome", "Default", "chromium", path,
        "Chrome Safe Storage", "Chrome",
    )


def test_reads_encrypted_sid(tmp_path, monkeypatch):
    host = ".my.salesforce.com"
    enc = encrypt_chromium_value("THE_SID_VALUE", _PASSWORD, host)
    db = tmp_path / "Cookies"
    _make_db(db, [(host, "", enc)])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: _PASSWORD)

    assert chromium.read_chromium_cookie(_source(db), "acme.my.salesforce.com", "sid") == "THE_SID_VALUE"


def test_strips_host_hash_prefix(tmp_path, monkeypatch):
    host = "acme.my.salesforce.com"
    enc = encrypt_chromium_value("HASHED_SID", _PASSWORD, host, with_host_hash=True)
    db = tmp_path / "Cookies"
    _make_db(db, [(host, "", enc)])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: _PASSWORD)

    assert chromium.read_chromium_cookie(_source(db), host, "sid") == "HASHED_SID"


def test_prefers_longest_host_suffix_match(tmp_path, monkeypatch):
    db = tmp_path / "Cookies"
    _make_db(db, [
        (".salesforce.com", "", encrypt_chromium_value("WRONG", _PASSWORD, ".salesforce.com")),
        ("acme.my.salesforce.com", "", encrypt_chromium_value("RIGHT", _PASSWORD, "acme.my.salesforce.com")),
    ])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: _PASSWORD)

    assert chromium.read_chromium_cookie(_source(db), "acme.my.salesforce.com", "sid") == "RIGHT"


def test_returns_plaintext_value_when_present(tmp_path, monkeypatch):
    db = tmp_path / "Cookies"
    _make_db(db, [(".my.salesforce.com", "PLAINTEXT_SID", b"")])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: None)

    assert chromium.read_chromium_cookie(_source(db), "acme.my.salesforce.com", "sid") == "PLAINTEXT_SID"


def test_returns_none_when_keychain_unavailable(tmp_path, monkeypatch):
    host = ".my.salesforce.com"
    db = tmp_path / "Cookies"
    _make_db(db, [(host, "", encrypt_chromium_value("X", _PASSWORD, host))])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: None)

    assert chromium.read_chromium_cookie(_source(db), "acme.my.salesforce.com", "sid") is None


def test_returns_none_when_no_matching_host(tmp_path, monkeypatch):
    db = tmp_path / "Cookies"
    _make_db(db, [(".example.com", "", encrypt_chromium_value("X", _PASSWORD, ".example.com"))])
    monkeypatch.setattr(chromium, "_keychain_password", lambda s, a: _PASSWORD)

    assert chromium.read_chromium_cookie(_source(db), "acme.my.salesforce.com", "sid") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chromium.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'salesforce_mcp_auto_auth_chrome.chromium'`

- [ ] **Step 3: Implement `chromium.py`**

Create `src/salesforce_mcp_auto_auth_chrome/chromium.py`:

```python
"""Read and decrypt Chromium-family cookies on macOS.

Chromium browsers (Chrome, Comet, Arc, Edge, Brave) store cookies in a SQLite
DB. Values are encrypted with a key derived from a per-browser password held in
the macOS Keychain ("<Browser> Safe Storage"). The algorithm: strip the ``v10``
prefix, derive an AES-128 key via PBKDF2-HMAC-SHA1 (salt ``saltysalt``, 1003
iterations), decrypt AES-128-CBC with a 16-space IV, drop PKCS7 padding, and —
for newer Chromium — strip a leading 32-byte ``SHA256(host_key)`` prefix.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .browsers import CookieSource

log = logging.getLogger(__name__)

_SALT = b"saltysalt"
_IV = b" " * 16
_ITERATIONS = 1003
_KEY_LEN = 16


def read_chromium_cookie(source: CookieSource, host: str, name: str) -> str | None:
    """Return cookie `name` for `host` from a Chromium cookie store, or None."""
    rows = _query_cookies(source.path, name)
    if rows is None:
        return None
    match = _best_host_match(rows, host)
    if match is None:
        return None
    host_key, value, encrypted = match
    if value:
        return value
    if not encrypted:
        return None
    password = _keychain_password(source.keychain_service, source.keychain_account)
    if not password:
        log.warning(
            "no Keychain password for %s (%s); cannot decrypt cookie",
            source.browser, source.keychain_service,
        )
        return None
    try:
        return _decrypt(bytes(encrypted), _derive_key(password), host_key) or None
    except Exception as e:  # noqa: BLE001 — decryption failures must not raise
        log.warning("decrypt failed for %s/%s: %s: %s",
                    source.browser, source.profile, type(e).__name__, e)
        return None


def _query_cookies(
    db_path: Path, name: str
) -> list[tuple[str, str, bytes]] | None:
    """Copy the (possibly locked) DB to a temp file and read matching rows."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "Cookies"
            shutil.copy2(db_path, tmp_db)
            con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            try:
                cur = con.execute(
                    "SELECT host_key, value, encrypted_value FROM cookies "
                    "WHERE name = ?",
                    (name,),
                )
                return [(h, v or "", e or b"") for (h, v, e) in cur.fetchall()]
            finally:
                con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("cookie DB read failed for %s: %s: %s",
                    db_path, type(e).__name__, e)
        return None


def _best_host_match(
    rows: list[tuple[str, str, bytes]], host: str
) -> tuple[str, str, bytes] | None:
    """Pick the row whose host_key is the longest suffix-match of `host`."""
    target = host.lower()
    best: tuple[str, str, bytes] | None = None
    best_len = -1
    for host_key, value, encrypted in rows:
        bare = host_key.lstrip(".").lower()
        if target == bare or target.endswith("." + bare):
            if len(bare) > best_len:
                best = (host_key, value, encrypted)
                best_len = len(bare)
    return best


def _derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), _SALT, _ITERATIONS, _KEY_LEN
    )


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16:
        return data[:-pad]
    return data


def _decrypt(encrypted: bytes, key: bytes, host_key: str) -> str | None:
    if encrypted[:3] != b"v10":
        return None
    ciphertext = encrypted[3:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(_IV))
    decryptor = cipher.decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    plain = _pkcs7_unpad(plain)
    domain_hash = hashlib.sha256(host_key.encode("utf-8")).digest()
    if plain[:32] == domain_hash:
        plain = plain[32:]
    return plain.decode("utf-8", errors="replace")


def _keychain_password(service: str | None, account: str | None) -> str | None:
    """Fetch the Safe Storage key from the macOS Keychain via `security`."""
    if not service or not account:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", service, "-a", account],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Keychain lookup failed for %s: %s", service, e)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chromium.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/chromium.py tests/test_chromium.py
git commit -m "feat: add Chromium cookie reader with macOS Keychain decryption"
```

---

### Task 4: Firefox cookie reader

**Files:**
- Create: `src/salesforce_mcp_auto_auth_chrome/firefox.py`
- Test: `tests/test_firefox.py`

**Interfaces:**
- Consumes: `CookieSource` from `browsers`.
- Produces: `read_firefox_cookie(source: CookieSource, host: str, name: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_firefox.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from salesforce_mcp_auto_auth_chrome.browsers import CookieSource
from salesforce_mcp_auto_auth_chrome.firefox import read_firefox_cookie


def _make_db(path: Path, rows: list[tuple[str, str]]) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO moz_cookies (host, name, value) VALUES (?, 'sid', ?)",
        rows,
    )
    con.commit()
    con.close()


def _source(path: Path) -> CookieSource:
    return CookieSource("firefox", "p.default", "firefox", path, None, None)


def test_reads_plaintext_sid(tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_db(db, [(".my.salesforce.com", "FF_SID")])

    assert read_firefox_cookie(_source(db), "acme.my.salesforce.com", "sid") == "FF_SID"


def test_prefers_longest_host_suffix_match(tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_db(db, [(".salesforce.com", "WRONG"), ("acme.my.salesforce.com", "RIGHT")])

    assert read_firefox_cookie(_source(db), "acme.my.salesforce.com", "sid") == "RIGHT"


def test_returns_none_when_no_match(tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_db(db, [(".example.com", "X")])

    assert read_firefox_cookie(_source(db), "acme.my.salesforce.com", "sid") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_firefox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'salesforce_mcp_auto_auth_chrome.firefox'`

- [ ] **Step 3: Implement `firefox.py`**

Create `src/salesforce_mcp_auto_auth_chrome/firefox.py`:

```python
"""Read Firefox cookies on macOS.

Firefox stores cookies unencrypted in ``cookies.sqlite`` (table ``moz_cookies``,
columns ``host``, ``name``, ``value``), so no Keychain/decryption is needed.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .browsers import CookieSource

log = logging.getLogger(__name__)


def read_firefox_cookie(source: CookieSource, host: str, name: str) -> str | None:
    """Return cookie `name` for `host` from a Firefox cookie store, or None."""
    rows = _query_cookies(source.path, name)
    if rows is None:
        return None
    target = host.lower()
    best: str | None = None
    best_len = -1
    for cookie_host, value in rows:
        bare = cookie_host.lstrip(".").lower()
        if target == bare or target.endswith("." + bare):
            if len(bare) > best_len and value:
                best = value
                best_len = len(bare)
    return best


def _query_cookies(db_path: Path, name: str) -> list[tuple[str, str]] | None:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "cookies.sqlite"
            shutil.copy2(db_path, tmp_db)
            con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            try:
                cur = con.execute(
                    "SELECT host, value FROM moz_cookies WHERE name = ?",
                    (name,),
                )
                return [(h, v or "") for (h, v) in cur.fetchall()]
            finally:
                con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("Firefox cookie read failed for %s: %s: %s",
                    db_path, type(e).__name__, e)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_firefox.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/firefox.py tests/test_firefox.py
git commit -m "feat: add Firefox cookie reader"
```

---

### Task 5: Safari `.binarycookies` parser

**Files:**
- Create: `src/salesforce_mcp_auto_auth_chrome/safari.py`
- Test: `tests/test_safari.py`

**Note:** GoFi only stubbed Safari. This implements the documented `.binarycookies`
format from scratch. Reading Safari's cookie file requires **Full Disk Access**
for the host process (Claude Desktop / terminal); a `PermissionError` is logged
and yields `None`.

**Interfaces:**
- Consumes: `CookieSource` from `browsers`.
- Produces:
  - `parse_binarycookies(data: bytes) -> list[tuple[str, str, str]]` — list of `(host, name, value)`.
  - `read_safari_cookie(source: CookieSource, host: str, name: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_safari.py`:

```python
from __future__ import annotations

import struct
from pathlib import Path

from salesforce_mcp_auto_auth_chrome import safari
from salesforce_mcp_auto_auth_chrome.browsers import CookieSource
from salesforce_mcp_auto_auth_chrome.safari import parse_binarycookies, read_safari_cookie


def _build_cookie(host: str, name: str, value: str) -> bytes:
    """Build one binarycookies cookie record (offsets relative to record)."""
    header_len = 56  # 14 little-endian 4-byte fields
    url = host.encode() + b"\x00"
    nm = name.encode() + b"\x00"
    path = b"/\x00"
    val = value.encode() + b"\x00"
    url_off = header_len
    name_off = url_off + len(url)
    path_off = name_off + len(nm)
    value_off = path_off + len(path)
    total = value_off + len(val)
    header = struct.pack(
        "<iiii iiii i i dd",
        total, 0, 0, 0,
        url_off, name_off, path_off, value_off,
        0, 0,        # 8-byte end-of-header marker (two ints)
        0.0, 0.0,    # expiry, creation (mac epoch doubles)
    )
    return header + url + nm + path + val


def _build_page(cookies: list[bytes]) -> bytes:
    n = len(cookies)
    header = struct.pack("<i", 0x00000100) + struct.pack("<i", n)
    offsets_table_len = 4 * n + 4  # +4 trailing footer field
    base = len(header) + offsets_table_len
    offsets = b""
    running = base
    for c in cookies:
        offsets += struct.pack("<i", running)
        running += len(c)
    offsets += struct.pack("<i", 0)  # page footer
    return header + offsets + b"".join(cookies)


def _build_file(pages: list[bytes]) -> bytes:
    magic = b"cook"
    count = struct.pack(">i", len(pages))
    sizes = b"".join(struct.pack(">i", len(p)) for p in pages)
    return magic + count + sizes + b"".join(pages)


def _source(path: Path) -> CookieSource:
    return CookieSource("safari", "default", "safari", path, None, None)


def test_parse_roundtrip():
    data = _build_file([_build_page([
        _build_cookie(".my.salesforce.com", "sid", "SAFARI_SID"),
        _build_cookie(".example.com", "other", "nope"),
    ])])

    parsed = parse_binarycookies(data)

    assert (".my.salesforce.com", "sid", "SAFARI_SID") in parsed


def test_read_safari_cookie_matches_host(tmp_path):
    data = _build_file([_build_page([
        _build_cookie(".my.salesforce.com", "sid", "SAFARI_SID"),
    ])])
    f = tmp_path / "Cookies.binarycookies"
    f.write_bytes(data)

    assert read_safari_cookie(_source(f), "acme.my.salesforce.com", "sid") == "SAFARI_SID"


def test_read_safari_cookie_missing_file(tmp_path):
    assert read_safari_cookie(_source(tmp_path / "nope.binarycookies"), "x.com", "sid") is None


def test_parse_rejects_bad_magic():
    import pytest
    with pytest.raises(ValueError):
        parse_binarycookies(b"nope" + b"\x00" * 20)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_safari.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'salesforce_mcp_auto_auth_chrome.safari'`

- [ ] **Step 3: Implement `safari.py`**

Create `src/salesforce_mcp_auto_auth_chrome/safari.py`:

```python
"""Parse Safari's ``Cookies.binarycookies`` file on macOS.

Format (big-endian header, little-endian page/cookie fields):
  - magic ``cook``
  - int32 BE page count, then page count × int32 BE page sizes
  - each page: int32 ``0x00000100``, int32 LE cookie count, cookie-count ×
    int32 LE cookie offsets (relative to page start), int32 footer, cookies
  - each cookie: int32 LE record size, ... int32 LE offsets to url/name/path/
    value (relative to record start), null-terminated strings at those offsets

Reading the file requires Full Disk Access for the host process.
"""
from __future__ import annotations

import logging
import struct

from .browsers import CookieSource

log = logging.getLogger(__name__)


def read_safari_cookie(source: CookieSource, host: str, name: str) -> str | None:
    """Return cookie `name` for `host` from Safari's cookie store, or None."""
    try:
        data = source.path.read_bytes()
    except (FileNotFoundError, PermissionError) as e:
        log.warning("Safari cookie file unreadable (%s): %s",
                    type(e).__name__, e)
        return None
    try:
        cookies = parse_binarycookies(data)
    except Exception as e:  # noqa: BLE001
        log.warning("Safari cookie parse failed: %s: %s", type(e).__name__, e)
        return None

    target = host.lower()
    best: str | None = None
    best_len = -1
    for cookie_host, cookie_name, value in cookies:
        if cookie_name != name:
            continue
        bare = cookie_host.lstrip(".").lower()
        if (target == bare or target.endswith("." + bare)) and value:
            if len(bare) > best_len:
                best = value
                best_len = len(bare)
    return best


def parse_binarycookies(data: bytes) -> list[tuple[str, str, str]]:
    """Parse a binarycookies blob into a list of ``(host, name, value)``."""
    if data[:4] != b"cook":
        raise ValueError("not a binarycookies file (bad magic)")
    num_pages = struct.unpack(">i", data[4:8])[0]
    page_sizes = [
        struct.unpack(">i", data[8 + 4 * i:12 + 4 * i])[0]
        for i in range(num_pages)
    ]
    offset = 8 + 4 * num_pages
    out: list[tuple[str, str, str]] = []
    for size in page_sizes:
        out.extend(_parse_page(data[offset:offset + size]))
        offset += size
    return out


def _parse_page(page: bytes) -> list[tuple[str, str, str]]:
    num_cookies = struct.unpack("<i", page[4:8])[0]
    offsets = [
        struct.unpack("<i", page[8 + 4 * i:12 + 4 * i])[0]
        for i in range(num_cookies)
    ]
    out: list[tuple[str, str, str]] = []
    for off in offsets:
        record = page[off:]
        url_off = struct.unpack("<i", record[16:20])[0]
        name_off = struct.unpack("<i", record[20:24])[0]
        value_off = struct.unpack("<i", record[28:32])[0]
        host = _read_cstr(record, url_off)
        name = _read_cstr(record, name_off)
        value = _read_cstr(record, value_off)
        out.append((host, name, value))
    return out


def _read_cstr(buf: bytes, offset: int) -> str:
    end = buf.index(b"\x00", offset)
    return buf[offset:end].decode("utf-8", errors="replace")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_safari.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/safari.py tests/test_safari.py
git commit -m "feat: add Safari binarycookies parser"
```

---

### Task 6: Orchestrator — `read_sid` across all sources

**Files:**
- Create: `src/salesforce_mcp_auto_auth_chrome/cookies.py`
- Test: `tests/test_cookies.py`

**Interfaces:**
- Consumes: `discover_sources`/`CookieSource` (browsers), `read_chromium_cookie` (chromium), `read_firefox_cookie` (firefox), `read_safari_cookie` (safari).
- Produces:
  - `read_sid(instance_url: str, browsers: list[str] | None = None, profiles: list[str] | None = None) -> str | None`.
  - `parse_env(environ: Mapping[str, str]) -> tuple[list[str] | None, list[str] | None]` — reads `SALESFORCE_BROWSERS` / `SALESFORCE_PROFILES`, returns `(browsers, profiles)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cookies.py`:

```python
from __future__ import annotations

from pathlib import Path

from salesforce_mcp_auto_auth_chrome import cookies
from salesforce_mcp_auto_auth_chrome.browsers import CookieSource


def _src(browser: str, family: str) -> CookieSource:
    return CookieSource(browser, "Default", family, Path("/x"), None, None)


def test_returns_first_nonempty_sid_in_priority_order(monkeypatch):
    sources = [_src("chrome", "chromium"), _src("comet", "chromium")]
    monkeypatch.setattr(cookies, "discover_sources", lambda b, p: sources)
    # chrome has no sid, comet does
    monkeypatch.setattr(cookies, "read_chromium_cookie",
                        lambda s, h, n: "COMET_SID" if s.browser == "comet" else None)

    assert cookies.read_sid("https://acme.my.salesforce.com") == "COMET_SID"


def test_dispatches_by_family(monkeypatch):
    monkeypatch.setattr(cookies, "discover_sources", lambda b, p: [_src("safari", "safari")])
    monkeypatch.setattr(cookies, "read_safari_cookie", lambda s, h, n: "SAFARI_SID")

    assert cookies.read_sid("https://acme.my.salesforce.com") == "SAFARI_SID"


def test_skips_sources_that_raise(monkeypatch):
    def boom(s, h, n):
        raise RuntimeError("locked")
    sources = [_src("chrome", "chromium"), _src("firefox", "firefox")]
    monkeypatch.setattr(cookies, "discover_sources", lambda b, p: sources)
    monkeypatch.setattr(cookies, "read_chromium_cookie", boom)
    monkeypatch.setattr(cookies, "read_firefox_cookie", lambda s, h, n: "FF_SID")

    assert cookies.read_sid("https://acme.my.salesforce.com") == "FF_SID"


def test_returns_none_for_bad_url(monkeypatch):
    monkeypatch.setattr(cookies, "discover_sources", lambda b, p: [])
    assert cookies.read_sid("not-a-url") is None


def test_parse_env_reads_lists():
    env = {"SALESFORCE_BROWSERS": "comet, chrome", "SALESFORCE_PROFILES": "Default"}
    assert cookies.parse_env(env) == (["comet", "chrome"], ["Default"])


def test_parse_env_empty_returns_none():
    assert cookies.parse_env({}) == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cookies.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'salesforce_mcp_auto_auth_chrome.cookies'`

- [ ] **Step 3: Implement `cookies.py`**

Create `src/salesforce_mcp_auto_auth_chrome/cookies.py`:

```python
"""Find a valid Salesforce ``sid`` across all browsers and profiles.

Iterates discovered cookie sources in priority order and returns the first
non-empty ``sid`` cookie matching the instance host. Reading never raises:
any per-source failure is logged and skipped.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlparse

from .browsers import CookieSource, discover_sources
from .chromium import read_chromium_cookie
from .firefox import read_firefox_cookie
from .safari import read_safari_cookie

log = logging.getLogger(__name__)


def read_sid(
    instance_url: str,
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> str | None:
    """Return the first valid Salesforce ``sid`` for `instance_url`, or None.

    Args:
        instance_url: My Domain URL, e.g. ``https://acme.my.salesforce.com``.
        browsers: Optional lowercase browser keys to restrict/order the search.
        profiles: Optional profile names to restrict the search (case-insensitive).

    Returns:
        The ``sid`` string from the highest-priority browser/profile that has a
        live session, or ``None`` if none do.
    """
    host = urlparse(instance_url).hostname
    if not host:
        log.warning("could not parse host from instance_url=%r", instance_url)
        return None

    for source in discover_sources(browsers, profiles):
        try:
            sid = _read_from_source(source, host)
        except Exception as e:  # noqa: BLE001 — never let one source break the loop
            log.warning("source %s/%s failed: %s: %s",
                        source.browser, source.profile, type(e).__name__, e)
            continue
        if sid:
            log.info("found sid in %s/%s for %s",
                     source.browser, source.profile, host)
            return sid
    log.info("no sid found in any browser/profile for %s", host)
    return None


def _read_from_source(source: CookieSource, host: str) -> str | None:
    if source.family == "chromium":
        return read_chromium_cookie(source, host, "sid")
    if source.family == "firefox":
        return read_firefox_cookie(source, host, "sid")
    if source.family == "safari":
        return read_safari_cookie(source, host, "sid")
    return None


def parse_env(
    environ: Mapping[str, str],
) -> tuple[list[str] | None, list[str] | None]:
    """Parse ``SALESFORCE_BROWSERS`` / ``SALESFORCE_PROFILES`` env vars.

    Each is a comma-separated list. Returns ``(browsers, profiles)`` where each
    element is a list (order preserved) or ``None`` when unset/empty.
    """
    return _split(environ.get("SALESFORCE_BROWSERS")), _split(
        environ.get("SALESFORCE_PROFILES")
    )


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cookies.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/cookies.py tests/test_cookies.py
git commit -m "feat: add multi-source sid orchestrator with env overrides"
```

---

### Task 7: Wire orchestrator into auth, entry point, and patch

**Files:**
- Modify: `src/salesforce_mcp_auto_auth_chrome/auth.py` (full rewrite of body)
- Modify: `src/salesforce_mcp_auto_auth_chrome/__main__.py:8`, `:38`, `:47`
- Modify: `src/salesforce_mcp_auto_auth_chrome/patch.py:21`, `:24-34`, `:47`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `read_sid` (cookies).
- Produces:
  - `auth.read_sid(...)` re-export and `auth.read_sid_from_chrome(instance_url) -> str | None` (back-compat alias, Chrome-only).
  - `patch.install(instance_url: str, browsers: list[str] | None = None, profiles: list[str] | None = None) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
from __future__ import annotations

from salesforce_mcp_auto_auth_chrome import auth


def test_read_sid_from_chrome_is_chrome_scoped(monkeypatch):
    captured = {}

    def fake_read_sid(url, browsers=None, profiles=None):
        captured["browsers"] = browsers
        return "SID"

    monkeypatch.setattr(auth, "read_sid", fake_read_sid)

    assert auth.read_sid_from_chrome("https://acme.my.salesforce.com") == "SID"
    assert captured["browsers"] == ["chrome"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `AttributeError: module 'salesforce_mcp_auto_auth_chrome.auth' has no attribute 'read_sid'`

- [ ] **Step 3: Rewrite `auth.py`**

Replace the entire contents of `src/salesforce_mcp_auto_auth_chrome/auth.py` with:

```python
"""Read the Salesforce `sid` session cookie from the user's browsers (macOS).

The `sid` cookie is what a browser stores when you're logged into a Salesforce
org. It's identical in value (and works identically) to a session-based API
access token, so we can use it directly as an `Authorization: Bearer <sid>`
header.

Reading scans multiple browsers and profiles (see `cookies.read_sid`). This
module deliberately swallows all errors and returns `None` instead of raising,
so the MCP server can start even when no session is available and surface a
friendly error only when a tool is actually invoked.
"""
from __future__ import annotations

from .cookies import read_sid

__all__ = ["read_sid", "read_sid_from_chrome"]


def read_sid_from_chrome(instance_url: str) -> str | None:
    """Back-compat shim: read the `sid` from Chrome's default-priority profiles.

    Prefer `read_sid` for multi-browser support. Retained so existing callers
    and external imports keep working.
    """
    return read_sid(instance_url, browsers=["chrome"])
```

- [ ] **Step 4: Update `__main__.py`**

In `src/salesforce_mcp_auto_auth_chrome/__main__.py`, change the import on line 8:

```python
from .auth import read_sid
```

Replace lines 38-39 (the initial read) with:

```python
    # Resolve which browsers/profiles to search (env overrides; defaults = all).
    from .cookies import parse_env
    browsers, profiles = parse_env(os.environ)

    # Seed env so mcp-salesforce-connector initializes happily even if no
    # browser currently has a sid. The per-call patch (installed below) ensures
    # the right token is used for every actual API request.
    initial_sid = read_sid(instance_url, browsers, profiles)
    os.environ["SALESFORCE_ACCESS_TOKEN"] = initial_sid or "PENDING_CHROME_LOGIN"
```

Replace line 47 (`install_patch(instance_url)`) with:

```python
    install_patch(instance_url, browsers, profiles)
```

- [ ] **Step 5: Update `patch.py`**

In `src/salesforce_mcp_auto_auth_chrome/patch.py`, change the import on line 21:

```python
from .auth import read_sid
```

Replace the `install` signature and docstring (lines 24-34) with:

```python
def install(
    instance_url: str,
    browsers: list[str] | None = None,
    profiles: list[str] | None = None,
) -> None:
    """Install the per-call sid refresh patch.

    Call this exactly once, before importing/starting `mcp-salesforce-connector`.
    The patch persists for the lifetime of the process.

    Args:
        instance_url: The Salesforce My Domain URL this server is bound to.
        browsers: Optional lowercase browser keys to restrict/order the search.
        profiles: Optional profile names to restrict the search.
    """
```

Replace line 47 (`sid = read_sid_from_chrome(instance_url)`) with:

```python
        sid = read_sid(instance_url, browsers, profiles)
```

And update the `RuntimeError` message (lines 49-53) to be browser-agnostic:

```python
            raise RuntimeError(
                f"Not logged into Salesforce in any supported browser for "
                f"{instance_url}. Open that org in your browser, sign in, then "
                f"retry. (No 'sid' cookie found.)"
            )
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (all tests across all modules)

- [ ] **Step 7: Smoke-test against a real org (manual)**

Run: `SALESFORCE_INSTANCE_URL=https://YOURORG.my.salesforce.com uv run python -m salesforce_mcp_auto_auth_chrome`
Expected: stderr prints `Ready for https://YOURORG... (initial sid: present ...)` when logged into that org in any supported browser. First run may show a macOS Keychain prompt per browser ("... wants to use confidential information stored in '<Browser> Safe Storage'") — click **Always Allow**. Ctrl-C to exit.

- [ ] **Step 8: Commit**

```bash
git add src/salesforce_mcp_auto_auth_chrome/auth.py src/salesforce_mcp_auto_auth_chrome/__main__.py src/salesforce_mcp_auto_auth_chrome/patch.py tests/test_auth.py
git commit -m "feat: wire multi-browser sid lookup into entry point and patch"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (the "Want to use Firefox/Safari/Brave/Arc" FAQ around line 262; add env-var docs)
- Modify: `docs/how-it-works.md` (cookie-reading section)

**Interfaces:**
- Consumes: nothing.
- Produces: user-facing docs for supported browsers + env vars.

- [ ] **Step 1: Update the README FAQ and add a configuration section**

In `README.md`, replace the FAQ line that begins "Want to use Firefox/Safari/Brave/Arc instead of Chrome: Not yet..." with:

```markdown
**Want to use a browser other than Chrome:** Supported out of the box. The
wrapper scans Chrome, Comet, Arc, Edge, Brave, Firefox, and Safari — and every
profile in each — and uses the first org session it finds. No config needed.

**Restrict or reorder the search** with optional env vars in your Claude
Desktop config:

- `SALESFORCE_BROWSERS` — comma-separated browser keys, in priority order.
  Keys: `chrome`, `comet`, `arc`, `edge`, `brave`, `firefox`, `safari`.
  Example: `"SALESFORCE_BROWSERS": "comet, chrome"`.
- `SALESFORCE_PROFILES` — comma-separated profile names to limit to (e.g.
  `"Default, Profile 1"`). Applies across all selected browsers.

**Safari note:** reading Safari cookies requires **Full Disk Access** for the
app that launches the MCP server (Claude Desktop or your terminal). Grant it in
System Settings → Privacy & Security → Full Disk Access. Without it, Safari is
silently skipped and other browsers are still used.
```

- [ ] **Step 2: Update `docs/how-it-works.md`**

In `docs/how-it-works.md`, update the cookie-reading description to state that the
wrapper now reads cookies natively (via `cryptography` + the macOS `security`
Keychain CLI) across multiple browsers/profiles instead of delegating to
`pycookiecheat`, and that it returns the first `sid` found in priority order.
Add a bullet noting Chromium decryption details (PBKDF2-SHA1 `saltysalt`/1003,
AES-128-CBC, `v10` prefix, optional 32-byte host-hash) and that Firefox is
plaintext and Safari is parsed from `.binarycookies`.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/how-it-works.md
git commit -m "docs: document multi-browser/profile support and env vars"
```

---

## Self-Review

**1. Spec coverage:**
- Multiple browsers → Task 2 registry (Chrome, Comet, Arc, Edge, Brave, Firefox, Safari) ✓
- Multiple profiles per browser → Task 2 `discover_sources` chromium/firefox enumeration ✓
- Borrow GoFi parsing → Task 3 ports GoFi's Chromium PBKDF2/AES approach (corrected IV to 16 spaces per the authoritative macOS algorithm; added host-hash strip GoFi lacked) ✓
- Comet, Arc → registry entries with verified Keychain services ✓
- Safari (GoFi only stubbed) → Task 5 implements a real `.binarycookies` parser per the chosen decision ✓
- Other GoFi browsers (Edge, Firefox) → registry + Task 4 ✓; Brave added (Chromium, trivial) ✓
- First-valid-by-priority + env overrides → Task 6 ✓
- New feature branch + plan saved in it → branch `feat/multi-browser-multi-profile` created; this file lives under it ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling" placeholders. Every code step shows complete code. Task 8 Step 2 is prose-editing guidance for a human-written doc section (no code contract), which is acceptable.

**3. Type consistency:** `CookieSource`/`BrowserConfig` field names are used identically in Tasks 2–6. Reader signatures `read_chromium_cookie(source, host, name)`, `read_firefox_cookie(source, host, name)`, `read_safari_cookie(source, host, name)` match their call sites in `cookies._read_from_source`. `read_sid(instance_url, browsers, profiles)` signature matches calls in `auth.py`, `__main__.py`. `install(instance_url, browsers, profiles)` matches the `__main__.py` call. `parse_env` returns `(browsers, profiles)` consumed positionally in `__main__.py`.

**Known risk to verify during execution:** The Comet Keychain *account* value is assumed to be `"Comet"` (service `"Comet Safe Storage"` was confirmed present; account not dumped). If decryption returns garbage for Comet, dump the account with `security find-generic-password -s "Comet Safe Storage" | grep acct` and update the `BrowserConfig` account in `browsers.py`.
