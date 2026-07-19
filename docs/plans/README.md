# docs/plans/

Interactive, self-contained HTML plan/decision pages — one file each,
offline-ready, approve/reject/edit-able, numbered per repo.

## Browse

```bash
bun run plans            # interactive picker (fzf if installed, else numbered)
bun run plans latest     # open the newest plan (highest #seq)
bun run plans <substr>   # open first plan whose filename matches <substr>
open docs/plans/index.html   # dashboard (auto-regenerated each session)
```

(No `package.json`? Use `node scripts/plans.mjs …` or the global `claude-plans`
command — same behavior.)

## Create

```bash
bun run plans new <slug> --title "Title" --source "where this came from"
```

Stamps `YYYY-MM-DD-NNN-<slug>.html` from `.plan-template.html` with the next
sequence number and this repo's accent color, then rebuilds the dashboard.
Fill in the thesis paragraph and `PLAN_ITEMS` (see `DESIGN.md`).

## Convention

- One plan = one `*.html` file: `YYYY-MM-DD-NNN-short-title.html`. `NNN` is the
  repo-monotonic sequence — highest number is always the latest plan.
- `plans.config.json` holds the repo's randomized accent color + `nextSeq`.
  Commit it; it keeps every machine's plans cohesive.
- Every page is interactive: ✓ approve / ✗ reject / double-click-edit each
  decision, then **Submit to Claude** (localhost listener, JSON download
  fallback). Decisions persist in `localStorage`.
- `DESIGN.md` holds the visual system + the decision-item contract.
- Pages are fully self-contained (inline CSS/JS, system fonts): they open from
  `file://` with no server and no build step.

This folder + `scripts/plans.mjs` are auto-scaffolded and version-upgraded from
`~/.claude/templates/plans/` by a SessionStart hook. Hook-owned files:
`plans.mjs`, `.plan-template.html`, `DESIGN.md`, `README.md`, `index.html`.
Plan pages themselves are never touched. Opt a repo out with an empty
`.no-claude-plans` file at its root.
