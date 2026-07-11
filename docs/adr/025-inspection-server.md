# ADR 025: The turn-log viewer grows into an inspection server; the model's standing inputs are logged

**Status:** Accepted (2026-07-11)

## Context

ADR 024's eval harness writes `evals/runs/<ts>[-label]/` directories
(`results.json`, `report.md`, `turns.jsonl`), but reviewing a run meant
reading markdown and hand-launching the turn-log viewer per run — and
comparing a case across runs meant opening N files. Worse, three of the
model's actual inputs were **invisible in every artifact**: the mode
prompt (`ModeSpec.goal`), the full system prompt the driver assembles
around it, and the per-turn context block (ADR 020). A reviewer judging
"did the agent behave well?" could not see what the agent was told.

## Decision

1. **One inspection server, grown from the turn-log viewer.**
   `orchestrator/turn_log_server.py` becomes `inspect_server.py` (the old
   module path stays as a shim — historical report.md footers name it).
   `/` is now an eval dashboard (`inspect.html`); the live viewer moved
   to `/logs`; `/runs/<id>/log` replays any run's transcript. The viewer
   HTML reaches its stream via a **relative** `events` URL, so the same
   unmodified file serves both the live tail (`/logs` → `/events`) and
   any run replay (`/runs/<id>/log` → `/runs/<id>/events`).
2. **The server never imports `evals`.** The wheel ships only
   `graph_context`; `evals/` is repo tooling that imports the
   orchestrator. `orchestrator/eval_index.py` reads the *artifacts*
   (json/tomllib) instead — tolerant where `dataset.py` is strict,
   because a viewer of historical runs must render everything else when
   one file is corrupt (failures become `warnings`, never a crash).
   Run/case ids from URLs pass `safe_child` (plain-segment allowlist +
   resolved containment) before touching the filesystem.
3. **The model's standing inputs are logged.** Two new turn-log events:
   `prompt` (mode goal, the exact assembled system prompt, and the bound
   tool surface with docstrings), logged when a session's
   (mode, goal, tools) fingerprint changes — first turn, `/mode` switch —
   never per decision; and `context` (the rendered ADR 020 context
   block), logged per turn when non-empty. The driver protocol gains
   `system_prompt(goal)` so the diary records what the driver *actually
   sends* from the same code path that sends it (`ClaudeAgentDriver`
   appends its guidance; scripted/manual drivers return the goal).
4. **results.json format 2 (additive).** Per-trial `session` (the
   transcript address — format 1 readers synthesize it from the
   historical `<case_id>#t<trial>` formula), `system_prompt`,
   `bound_tools`, `harness_error`; per-case `mode`, `judge_rubric`;
   top-level `format: 2`. `report.md` survives as the CLI's printed
   summary; its footer now points at the inspection server.
5. **The UI stays in the house style: stdlib + one self-contained HTML.**
   No Flask/React — the egress firewall makes new dependencies a
   container-image change, and the existing single-file pattern
   (hash-routed vanilla JS, `createElement`/`textContent` only) carries
   the dashboard, run detail, and case detail fine. `GC_EVAL_ROOT`
   (default `evals`, off-values disable) points the server at the
   artifacts; `serve` passes it through.

## Consequences

- Reviewing a run is: open `http://127.0.0.1:8765/`, click the case,
  read grades/judge/prompts, follow the transcript link (the viewer
  opens pre-filtered to `<case>#t<n>` via `?session=`).
- Dogfooding transcripts now carry the prompt and context block the
  model saw, at negligible byte cost (prompt events are deduped per
  mode-change; the diary's byte cap already bounds context events).
- The `{case_id}#t{trial}` session formula is pinned by format 2's
  explicit `session` field going forward; only pre-format-2 runs depend
  on the synthesis.
- `eval_index`'s tolerant TOML reading can lag `dataset.py` — the cost
  is missing dashboard metadata plus a warning, never a wrong verdict
  (verdicts come from results.json, written by the harness itself). The
  writer/reader contract is pinned by a round-trip test
  (`tests/evals/test_harness_smoke.py`) that scans a real scripted run
  through `eval_index`.
