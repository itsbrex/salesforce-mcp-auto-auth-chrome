# CONTEXT — domain glossary

The shared vocabulary for this codebase. Two layers: the **design vocabulary**
(how we talk about structure) and the **domain language** (what the concepts in
this package are called). Use these terms exactly — in code, comments, commits,
and review.

## Design vocabulary

- **module** — a unit that hides a design decision behind an interface. Not
  "component", "service", "layer", or "wrapper".
- **interface** — the surface a module presents to callers. Not "API" or
  "signature" when we mean the whole surface.
- **implementation** — the work a module hides behind its interface.
- **depth** — the ratio of hidden implementation to exposed interface. A **deep**
  module hides a lot behind a small interface; a **shallow** module's interface is
  nearly as complex as its implementation.
- **seam** — a boundary where two modules meet and could be separated or
  substituted. Not "boundary".
- **adapter** — a module that presents a common interface over a varying
  implementation, so callers dispatch through the interface instead of switching
  on a type tag.
- **leverage** — the ratio of what a module does for callers to what it costs
  them to use. An indirection with no leverage is a hop that buys nothing.
- **locality** — related knowledge living in one place. A concept that forces you
  to bounce between modules to understand it has poor locality.
- **deletion test** — for a suspected shallow module: would deleting it
  *concentrate* complexity (good — it was hiding a real decision) or just *move*
  it (a sign it never earned its place)?

## Domain language

### Cookie source
A single resolved cookie store: one browser profile with a path to its cookie DB
plus (for Chromium) its Keychain coordinates. Modeled by `CookieSource` in
`browsers.py`. Discovery walks each browser's data directory and yields one
cookie source per profile that actually holds a store.

### Browser family
The kind of cookie store a browser uses — `chromium`, `firefox`, or `safari` —
which selects the decryption/parsing strategy. Historically a stringly-typed
`family` tag matched in several switch statements; the target shape is one
**cookie reader** adapter per family (see below).

### Cookie reader
The **adapter** that owns everything a browser family knows how to do: discover
its profiles, read one cookie for a host, and list all Salesforce `sid` cookies
in a store. One reader per family, selected by a registry lookup rather than a
`family ==` switch. Deep by design — the callers (`cookies.py`) talk to the
reader **interface** and never branch on family.

### sid
The Salesforce session cookie a browser stores when logged into an org. Usable
directly as an `Authorization: Bearer <sid>` token. Reading it never raises —
failures return `None` so auth errors surface at tool-call time, not startup.

### My Domain normalization
Salesforce serves the Lightning UI from `<org>.lightning.force.com` but the REST
API (and the `sid` that authorizes it) lives on `<org>.my.salesforce.com`. The
Lightning host carries a *different* `sid` the REST API rejects. `instance.py`
normalizes any Salesforce URL to its My Domain REST host before we read cookies
or call the API.

### Org resolution
Choosing which org to bind at startup and an initial `sid`: honor a configured
URL if set, else auto-discover a logged-in org whose `sid` validates against the
REST API. Includes the **ranking rule** below. Currently lives in `cookies.py`
(`discover_orgs`, `resolve_session`); a candidate deepening lifts it into its own
`orgs.py` module so resolution is testable without the cookie readers.

### Ranking rule
Among discovered orgs, a `sid` whose cookie was set on a `my.salesforce.com`
host outranks a Lightning-derived one (the latter's `sid` is rejected by REST).
This is domain knowledge that deserves a name and a home rather than living
inside a discovery loop.

### Per-call sid refresh patch
`patch.py` monkey-patches `simple_salesforce.Salesforce._call_salesforce` so
every outgoing request reads a fresh `sid` from the pinned browser/profile. A
**deep** module: a one-call `install()` interface hides a signature self-check,
an expired-session retry, a pending-login sentinel, and browser User-Agent
matching.

### Pinned source
The exact browser/profile the initial `sid` came from. Per-call refresh reads
from the *same* source first so tokens don't drift to another profile holding a
stale `sid` for the same host, falling back to the configured scope only when the
pinned source has logged out.
