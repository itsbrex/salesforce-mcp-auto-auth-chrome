# Design — Interactive Plan Pages (v2)

System doc for the self-contained HTML plan/decision pages under `docs/plans/`.
A plan page is a single offline `.html` file the reader **acts on**: every
decision item can be approved, rejected, or edited in place, then submitted
back to Claude for execution.

Reader context: a developer on a laptop reviewing proposals — approving some,
rewording others — in a dim room at night or a bright cafe by day. The page must
stay legible in glare, dense without sprawling, and every action must feel
instant.

## Creating a page (required workflow)

Never hand-roll a plan page from scratch. Stamp one:

```bash
bun run plans new <slug> --title "Human Title" --source "claude session <id> / <what prompted it>"
```

This assigns the next sequence number from `plans.config.json`, injects the
repo's persistent accent color, stamps date/repo/provenance into the meta strip,
writes `YYYY-MM-DD-NNN-<slug>.html`, and regenerates the dashboard. Then edit
the file: fill the thesis paragraph (`#plan-sub`), replace the sample
`PLAN_ITEMS`, and optionally add `.prose` context sections above the groups.

## Numbering & provenance

- `plans.config.json` holds `nextSeq`; every stamped page gets a monotonically
  increasing `#NNN` badge, embedded as `data-plan-seq` on `<html>` and in the
  filename. Highest seq = latest plan, always.
- The meta strip on every page shows `#NNN · date · repo · source`. `--source`
  should say where the content came from (session, prompt, review, etc.).
- The dashboard (`index.html`, auto-regenerated) lists pages newest-first with
  seq badges and marks the latest.

## Interactivity contract

Each page defines `PLAN_ITEMS`, an array of decision items:

```js
{ id: "d01",            // unique + stable within the page
  group: "Migration",   // section heading it renders under
  kind: "edit",         // "edit" | "structural" | "verify" | "note"
  title: "One-line statement of the decision",
  why: "Rationale / consequence the reader needs to judge it",
  current: "Status quo (omit or \"\" when N/A)",
  suggested: "Proposed change — double-click-editable by the reader" }
```

Reader affordances (already wired in the template — do not remove):

- ✓ Approve / ✗ Reject toggles per item, tri-state with pending.
- Double-click the suggested text → textarea; Esc cancels, blur saves; edited
  items get an `edited` badge and a revert button.
- All decisions persist to `localStorage` (`plans:<slug>:<seq>`).
- Filter chips (All / per-group / Pending only), expand/collapse all,
  stat tiles, toast confirmations.
- **Submit to Claude** POSTs the full decision payload to
  `http://127.0.0.1:47613/submit`; when the listener is offline it downloads
  `<slug>-decisions.json` instead. A status dot pings `/ping` every 5s.

### Receiving decisions (Claude side)

When you expect the user to submit, start a listener in the background and let
its exit notify you:

```ts
// bun run listener.ts   (adapt OUT path per session)
const OUT = "<scratchpad>/plan-decisions.json";
const CORS = { "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type" };
Bun.serve({ port: 47613, hostname: "127.0.0.1", async fetch(req) {
  if (req.method === "OPTIONS") return new Response(null, {status:204, headers:CORS});
  const u = new URL(req.url);
  if (req.method === "GET" && u.pathname === "/ping")
    return Response.json({ok:true}, {headers:CORS});
  if (req.method === "POST" && u.pathname === "/submit") {
    await Bun.write(OUT, await req.text());
    setTimeout(() => process.exit(0), 400);
    return Response.json({ok:true}, {headers:CORS});
  }
  return new Response("plans listener", {headers:CORS});
}});
```

Payload shape: `{version, kind:"plan-decisions", plan:{seq,slug,…}, submittedAt,
decisions:[{id,group,kind,title,status,current,suggested,edited}]}`. Execute
`approved` items (honoring reader edits in `suggested`), skip `rejected` and
`pending`, treat `note` kinds as acknowledgments.

## Theme

OLED-native dark, single theme. True black `#000` base, near-black elevated
surfaces separated by lightness + hairline borders, never drop shadow. No
gradient backgrounds, no gradient text, no glassmorphism. Precision-instrument
feel: dense, legible, fast.

## Color

OKLCH. **The primary accent is per-repo**, randomized once by the SessionStart
hook into `docs/plans/plans.config.json` and injected into every stamped page —
all of a repo's plans share one identity color. Never hardcode a different
accent; read the config. Supporting roles are fixed:

```css
/* Base */
--bg:            oklch(0 0 0);
--surface-1:     oklch(0.169 0.004 265);  /* card / panel */
--surface-2:     oklch(0.214 0.005 265);  /* sticky bar, open cards */
--surface-3:     oklch(0.255 0.006 265);  /* buttons, inputs */
--hairline:      oklch(0.30 0.006 265);
--hairline-strong: oklch(0.40 0.008 265);

/* Ink */
--ink:           oklch(0.971 0 0);
--ink-muted:     oklch(0.74 0.012 265);   /* >=4.5:1 on black */
--ink-faint:     oklch(0.62 0.012 265);   /* labels/meta at >=13px */

/* Roles */
--accent:        <from plans.config.json>; /* approvals, badges, latest, identity */
--accent-ink:    oklch(0.17 0.03 <hue>);   /* text on accent fills */
--danger:        oklch(0.70 0.20 25);      /* reject, current-state stripe */
--warn:          oklch(0.83 0.16 75);      /* edited badge */
--focus:         oklch(0.86 0.16 215);     /* focus ring, visible on any accent */
```

The curated accent pool (all ≥7:1 on black): lime 132, cyan 215, violet 300,
amber 75, pink 8, coral 30, mint 165, azure 245, magenta 330, chartreuse 105.

## Typography

System stacks only, so pages open offline with zero network requests:

```css
--font-ui:   system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
--font-mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace;
```

Fixed rem scale `.75 / .8125 / .9375 / 1 / 1.125 / 1.75`. Display 700, body 400,
labels 500–600, heading letter-spacing -0.02em. Prose measure ≤76ch. Mono for:
seq badges, dates, filenames, labels, counts, code. Code blocks never wrap;
`overflow-x:auto`.

## Spacing, radius, layout

8px rhythm (`4 8 12 16 24 32 48 64`). Radius 8/12/16, pill 999 for chips/badges.
Touch-target floor 44px (38px buttons acceptable inside dense desktop toolbars).
Content `max-width:1100px` (dashboard 900px). Sticky toolbar solid `--surface-2`
+ hairline, no blur. Mobile <640px: single column, cards stack full width.

## Components

- **Toolbar** (sticky): listener status dot + live counts + expand/collapse +
  primary Submit button.
- **Meta strip**: seq pill + date + repo + source — provenance at a glance.
- **Stat tiles**: `repeat(auto-fit, minmax(140px,1fr))` — decisions/approved/
  rejected/pending, mono numerals.
- **Decision card**: `<details open>` with dot + title + state badge + chevron;
  body = why → current (danger-striped) → suggested (accent-striped, editable)
  → approve/reject/revert row.
- **Filter chips**: single-select `aria-pressed`, All / groups / Pending only.
- **Prose sections**: optional free-form context blocks above the groups.
- **Toast**: `aria-live=polite`, auto-dismiss.
- States everywhere: default, hover, focus-visible, active, edited, empty.

## Motion

150–250ms ease-out: chevron rotate, hover lift, chip select, toast slide. No
page-load choreography. `@media (prefers-reduced-motion: reduce)` → instant.

## Self-contained rule

Every plan page is one `.html` with all CSS + JS inline and system-font stacks.
It must open from `file://` with no network and no build step — `bun run plans`
(or a double-click) always works offline. The only network call is the optional
`127.0.0.1:47613` listener ping/submit, which degrades gracefully.
