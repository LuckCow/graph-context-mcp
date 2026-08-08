---
name: prose-shot
description: Playwright visual check of prose.html — render the real page + CodeMirror bundle with mocked /api/prose routes, interact, screenshot light+dark. Use after any prose.html CSS/JS change, or when asked to "screenshot the prose page" / "check it with playwright".
allowed-tools: Bash(PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python *) Bash(python *)
---

Visual-check prose.html without the live stack: `harness.py` (in this
skill's base directory) loads the REAL `src/graph_context/orchestrator/
prose.html` and vendored CodeMirror bundle in headless chromium, mocking
every `/api/prose/*` route with fixture payloads — no orchestrator, no
Anytype, no historian.

## Run

```bash
PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python <skill-dir>/harness.py \
  --out <scratchpad-dir>
```

- `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` is load-bearing: chromium is
  preinstalled there in the devcontainer image, and the egress firewall
  blocks `playwright install` downloads.
- The stock harness renders one doc with all four review states (plain /
  needs_change / approved / locked paragraphs), drags a keyboard
  selection from plain text into the needs_change paragraph, and writes
  `selection_light.png` / `selection_dark.png`.

Then READ the PNGs (the Read tool renders images) and actually look at
them — that is the verification, not the script exiting 0. Check both
themes; send them to the user with SendUserFile when they demonstrate
the change under discussion.

## Custom scenarios

Copy `harness.py` to the scratchpad and edit — never edit the skill's
copy in place for a one-off. The docstring documents the wire shapes
(`spans` are positional `[start, end, author, status, intent]`; offsets
are code points, so keep fixture bodies ASCII). Common variations:

- **Comments / view modes / marks**: extend `doc_payload()` (`comments`
  rows need `{id, text, state, hash, at, by, state_at, state_by,
  anchor}`); click legend chips before the screenshot for view modes.
- **Interactions**: prefer keyboard gestures (`Control+Home`,
  `Shift+ArrowDown`, `Shift+End`) over mouse coordinates — layout moves.
- **Write flows** (save, 409 conflict, marks POST): add
  `page.route("**/api/prose/save", ...)` etc. returning the shapes in
  `orchestrator/prose_bridge.py`; `writes_enabled: True` in the spaces
  payload is what arms the editor + action bar.
- **SSE**: aborting `api/prose/events*` is fine (EventSource retries
  quietly); to test live-update reconciliation you need a real server
  instead — that's `orchestrator.serve`, out of this skill's scope.

Gotchas learned the hard way:

- The vendored bundle's own focused-selection CSS is opaque and
  higher-specificity than page rules; the page overrides with
  `!important` (see the comment block in prose.html around
  `.cm-selectionBackground`). Don't "simplify" those away.
- CodeMirror's selection layer sits at inline `z-index: -1` (behind the
  text); prose.html lifts it above the content so selections stay
  visible over the opaque review-state highlights.
- A blank screenshot usually means the doc fetch failed silently —
  check `page.on("console", print)` / `page.on("pageerror", print)`.

This is a display-only check; it proves nothing about the bridge or
historian. Domain/server changes still need the pytest suites (/dod).
