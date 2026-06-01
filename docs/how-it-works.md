# How it works

This doc captures the architecture and the design decisions, including the things we tried first and discarded. If you're forking or extending this package, read this before changing anything load-bearing.

## The core insight

When you log into Salesforce in Chrome, Chrome stores a cookie named `sid` on the My Domain (e.g. `acme.my.salesforce.com`). The value of that cookie:

- **Is the active session id** for your UI session
- **Is also accepted as a Bearer token** by Salesforce's REST API (with rare org-level exceptions like IP-locked sessions)
- **Refreshes automatically** every time you load a Salesforce page in Chrome — as long as you're actively using the org, the cookie stays alive

So if we could just *borrow* that cookie value every time we want to make an API call, we'd never have to manually paste a token again.

That's the whole package, in one sentence.

## The pieces

```
┌────────────────────────────────────────────────────────────────┐
│  Claude Desktop                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  claude_desktop_config.json                              │  │
│  │  "command": "uvx",                                       │  │
│  │  "args": [...this package...],                           │  │
│  │  "env": { "SALESFORCE_INSTANCE_URL": "https://..." }     │  │
│  └─────────────┬────────────────────────────────────────────┘  │
└────────────────┼───────────────────────────────────────────────┘
                 │ launches via stdio
                 ▼
┌────────────────────────────────────────────────────────────────┐
│  salesforce-mcp-auto-auth-chrome (this package)                │
│                                                                │
│  __main__.py                                                   │
│  ├── reads SALESFORCE_INSTANCE_URL                             │
│  ├── seeds env so mcp-salesforce-connector inits cleanly       │
│  ├── calls patch.install(instance_url)                         │
│  └── hands off to mcp-salesforce-connector's main()            │
│                                                                │
│  patch.py                                                      │
│  └── monkey-patches simple_salesforce.Salesforce               │
│      ._call_salesforce → reads fresh sid per call              │
│                                                                │
│  auth.py                                                       │
│  └── chrome_cookies(instance_url) → sid                        │
└────────────────────────────────────────────────────────────────┘
                 │
                 │ delegates to
                 ▼
┌────────────────────────────────────────────────────────────────┐
│  mcp-salesforce-connector  (unmodified, from PyPI)             │
│  └── uses simple_salesforce.Salesforce for all API calls       │
│      └── every call goes through ._call_salesforce             │
│          └── ← OUR PATCH RUNS HERE                             │
└────────────────────────────────────────────────────────────────┘
                 │
                 │ HTTPS, with fresh Bearer token
                 ▼
            Salesforce REST API
```

## Why monkey-patch, why not fork

`mcp-salesforce-connector` is a pretty thin wrapper over `simple_salesforce` plus an MCP-protocol shell. Forking it to add cookie-based auth would mean:

- Maintaining a fork forever (or PR'ing upstream and waiting)
- Diverging from the upstream connector when it adds features
- Confusing users who'd need to track two packages

Instead, we treat `mcp-salesforce-connector` as a black box and patch the one method it uses for *every* HTTP call: `simple_salesforce.Salesforce._call_salesforce`. The patch is class-level, applied once at startup, and is invisible to anything that imports `simple_salesforce` after that point.

If the connector ever switches off `simple_salesforce`, this package will need updating. That's a real risk but a small one — `simple_salesforce` is the de facto Python client and changing libraries is a large project.

## Why defer auth to tool-call time

Our first iteration of this wrapper failed at startup if Chrome didn't have a `sid` cookie. It printed a clear error and exited 1.

That's the "fail loudly" pattern, and it's the right pattern for *required* configuration. But it's the wrong pattern for *optional* infrastructure.

Most users add several Salesforce orgs to their Claude Desktop config but only actively use a couple at a time. With "fail at startup," every Claude Desktop restart triggered a flurry of "Server disconnected" warnings for the orgs the user wasn't actively logged into. Annoying enough that we rebuilt.

The current design:

1. **Startup:** Always succeed. Seed `SALESFORCE_ACCESS_TOKEN` to either a real `sid` (if Chrome has one) or the literal string `"PENDING_CHROME_LOGIN"`. The MCP server initializes, tools register, no warnings.
2. **Tool call:** Re-read `sid` from Chrome. If present → use it. If absent → raise `RuntimeError` with a friendly message. The MCP protocol surfaces that exception as a tool error, which Claude shows in the chat as actionable feedback.

This means: **errors surface only when the user actually cares**. If they never use the `salesforce-sandbox-3` org during a session, they never see an error about it.

## Why refresh on every call (not just at startup)

Salesforce sessions can expire mid-conversation — especially for sandboxes with shorter session timeouts. If we cached the `sid` at startup, a long-running Claude session could hit "INVALID_SESSION_ID" after a few hours and start failing.

Reading the `sid` from Chrome's cookie store is cheap (sub-100ms, even with the Keychain dance amortized). So we just do it on every call. This means:

- As long as you're using Chrome normally, your session stays alive *in Chrome*
- Every Claude tool call inherits that freshness
- If you do log out, the very next tool call will surface a clear error instead of a confusing INVALID_SESSION_ID

The only meaningful downside is a small per-call latency. If you're making thousands of calls per minute, add a TTL cache. For interactive Claude usage, it's a non-issue.

## Why clear OAuth env vars

`mcp-salesforce-connector` supports multiple auth flows (OAuth client credentials, username/password, session id). Which one it picks depends on which env vars are set, with OAuth taking precedence over session id.

If a user previously configured an org with OAuth and later switched to this wrapper without removing the OAuth env vars from their config, the connector would still try the OAuth path — and our cookie-based patch would be bypassed.

So in `__main__.py` we explicitly delete `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`, and `SALESFORCE_DOMAIN` from the process env before launching the connector. This is a defensive belt-and-suspenders move; not strictly needed if the user's config is clean.

## Why one wrapper per org (and not one wrapper, many orgs)

You could imagine a single MCP server that exposes tools like `query(org="dev1", sql="...")` and routes to the right org per call. We chose not to do that because:

- Each MCP server in Claude Desktop maps cleanly to "one org" in the user's mental model
- Claude's tool-picking is more accurate when tool names are unambiguous ("salesforce-prod-sandbox.run_soql_query" leaves no doubt)
- Independent failure: if one org's session expires, it doesn't break the others

The cost is a few processes, but those processes are idle when not in use and consume <50MB each.

## What we tried first and discarded

| Attempt | Why it didn't work |
|---|---|
| Fork `mcp-salesforce-connector`, add a `--auth-from-chrome` flag | Maintenance burden, divergence from upstream |
| Use Chrome's DevTools Protocol to read cookies | Requires Chrome to be running with remote debugging enabled; way too much friction for users |
| Use `mcp-salesforce-connector`'s OAuth path with a shared connected app | Needs a connected app per org; you might as well just paste a token |
| Wrap `requests.Session` instead of `_call_salesforce` | Too low-level — every other library that uses `requests` would also be affected |
| Cache the `sid` for 5 minutes | Caused stale-token errors more often than the per-call read added latency |

## What we learned that's generally useful for MCP wrappers

1. **Treat the upstream MCP server as a library**, not a process to spawn. If you can `from x import main`, you can wrap behavior without subprocess gymnastics.
2. **Monkey-patch as close to the network boundary as possible.** One patch in `simple_salesforce._call_salesforce` covers everything; patching at the SObject level (every CRUD method, every query helper) would be a maintenance nightmare.
3. **Differentiate startup errors from runtime errors.** Startup errors should be loud and rare. Runtime errors should be friendly and surface in the chat. Most "missing config" cases are actually "missing user state," and user state is a runtime concern.
4. **Refresh secrets on every use, not at startup.** It's almost always cheaper than the failure mode of a stale credential, especially in interactive contexts.
5. **Use a persistent venv, not ephemeral `uvx`.** A persistent venv eliminates 5+ seconds of dependency resolution every time Claude Desktop starts. The user-facing form factor is still `uvx --from git+...`, but the package itself is small and stable, so the venv lasts.
