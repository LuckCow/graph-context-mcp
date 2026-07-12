# Testing & verification

Everything here runs offline against mocks unless explicitly gated live. Definition of Done for any change is all four commands green; CI (`.github/workflows/ci.yml`) runs exactly these on every push:

```bash
pytest              # full mock-backed suite; live E2E self-skips
ruff check src tests
mypy src            # strict
lint-imports        # layer/dependency contracts (import-linter, pyproject.toml)
```

Single file / test: `pytest tests/unit/test_traversal.py -k explore` (pythonpath is configured in `pyproject.toml`).

## Suite map

| Directory | What it covers |
|---|---|
| `tests/unit` | Pure domain logic (graph, traversal, schema, session) — no I/O, runs in milliseconds |
| `tests/contract` | The same behavioral suite run against **both** `InMemoryGraphRepository` and the mock-backed `AnytypeGraphRepository` — fakes are contracts |
| `tests/anytype` | Adapter specifics: mapping, registry, sync, client, mock-server fidelity |
| `tests/application` / `tests/interface` / `tests/orchestrator` | Use-cases, tool surface + presenters, harness pipeline/transports |
| `tests/semantic` | Embedders, semantic index, ranker; ranking eval golden at `tests/semantic/ranking_eval.toml` |
| `tests/evals` | The eval harness's own plumbing, scripted (no model): dataset validation, grader honesty, every shipped case's reference script replayed |
| `tests/e2e` | The contract suite against a **real** Anytype server; self-skips unless `ANYTYPE_E2E=1` |

**Fakes are contracts:** any behavior added to the Anytype adapter must land in the in-memory fake too, with a test. If the fake can't express it, the port is wrong — fix the port.

## MockAnytype and the quirk quarantine

All Anytype representation assumptions (A1–A8) live in `infrastructure/anytype/mapping.py` and are mirrored by `infrastructure/anytype/mock_server.py` (`MockAnytype`), which pins live-server behavior confirmed by spikes (`docs/spikes/`): search caps, the body-editing field-name mismatch (create with `body`, update via the `markdown` PATCH key, never present in list/search — ADR 010), timestamps-from-properties, and the fresh-relation settle window. Chat payload/SSE assumptions (C1–C6) live in `infrastructure/anytype/chat.py`, the chat analogue of `mapping.py`.

## Live E2E (real Anytype server)

```bash
ANYTYPE_E2E=1 python -m pytest tests/e2e -q
```

Runs against the **headless sidecar** (`anytype` compose service, part of the default stack since the WP14 cutover, ADR 019): compose sets `ANYTYPE_API_BASE_URL=http://anytype:31012` and `ANYTYPE_API_KEY_FILE=.devcontainer/secrets/anytype_api_key` (the bot account's key), so no extra env is needed. The desktop app is not involved — the suite find-or-creates a space named exactly `GC-E2E` on the bot account and **resets it before and after each run** (the local API cannot delete spaces). Spike artifacts do not survive a run; the spike scripts reseed themselves. With the sidecar's rate limit disabled the whole suite runs in ~10s.

To point tooling at the desktop app temporarily, override `ANYTYPE_BASE_URL=http://host.docker.internal:31009` with a desktop-issued key.

## Golden snapshot tests

Profile tool docstrings are prompts, and they are pinned by golden snapshots (`tests/interface/golden/`). Editing them is prompt engineering; the golden diff is the review artifact. Regenerate deliberately with:

```bash
GC_REGEN_GOLDENS=1 pytest tests/interface/test_profiles.py
```

## Behavioral evals (WP16, ADR 024)

Evals grade the LLM's actual tool-driving behavior — they are **runs, not
tests**: live runs spend Claude subscription quota and are
nondeterministic, so pytest never collects them and CI never runs them.

```bash
python -m evals run                          # all cases, real model
python -m evals run --case who_is_mira --trials 1
python -m evals run --driver scripted        # replay reference scripts, no model
python -m evals run --judge --label candidate
python -m evals compare evals/runs/A evals/runs/B
```

Each trial gets a **fresh in-memory runtime** (seeded through the
repository port) and runs real `Orchestrator.handle_message` turns. Code
graders check outcomes — graph end-state, session state, reply substrings,
loose trajectory bounds — never a prescribed call sequence; `--judge` adds
a rubric-scored LLM verdict (reasoning-first, reported alongside, never
overruling the code grades — unless the case sets `[case.judge] required =
true`, for judge-only expectations like fabricated success; required
judges run on every live run and gate the trial). Runs land in
`evals/runs/<ts>[-label]/`
(gitignored): `results.json` (format 2 — per-trial grades, judge
verdicts, the transcript session key, the exact system prompt and bound
tools), `report.md`, and a `turns.jsonl` in the standard diary format,
including the `prompt`/`context` events that record what the model was
told (ADR 025).

**Reviewing runs** happens in the inspection server (ADR 025), which
also runs inside `serve`:

```bash
python -m graph_context.orchestrator.inspect_server   # http://127.0.0.1:8765/
```

The dashboard lists every case with its latest verdict and run history;
run/case pages show grades, judge reasoning, and prompts, and link each
trial to its transcript (the viewer opens pre-filtered to
`<case>#t<n>`). `GC_EVAL_ROOT` / `--eval-root` point it at the
artifacts (default `evals`).

**Adding a case** (`evals/cases/*.toml`): give it an `id`, seed nodes,
one or more `[[case.turn]]` user messages, `[case.expect.*]` graders, and
a `[[case.script]]` — the reference solution. The scripted CI replay
(`tests/evals/test_harness_smoke.py`) fails any case whose own script
cannot satisfy its graders, so unsolvable cases die before they cost a
live run. Keep the dataset balanced (behaviors that should AND shouldn't
happen — the mode-boundary twins are the template), grow it from real
dogfooding failures, and calibrate rubrics by reading run transcripts.
The `/evals-add` skill drives this end-to-end from a failure report
(evidence from `logs/turns.jsonl`, grader alignment, validation);
`/evals-run` runs and compares. Node refs support `fields_truthy` /
`fields_falsy` value checks, and `[[case.modes]]` stages custom in-space
Activity Modes (e.g. a deliberately misconfigured read-only mode) into
the trial's registry. A seed node with `out_of_band = true` exists in
the space but not the index until a resync (a human editing the Anytype
UI between syncs — the stale-index/duplicate-node class of failure); it
counts toward the `node_count_delta` baseline, and the runner resyncs
once after the turns so graders always judge the space, not the index.

## Demo scripts

In-process acceptance walkthroughs, all mock-backed (`PYTHONPATH=src python scripts/<name>.py`):

| Script | Shows |
|---|---|
| `demo_wp2_tools.py` | The full tool loop: composite create → scene-assembly `explore` → `find_path` → stale-summary sweep → resync reporting → actionable errors |
| `demo_workspace_profile.py` | The same tool surface as a work knowledge base (`GC_PROFILE=workspace`) |
| `demo_wp12_assistant.py` | `record_procedure` end-to-end — a mode is configuration, not a fork |
| `demo_wp11_search.py` | Semantic search + graph-aware ranking with the deterministic hash embedder |
| `demo_wp6_orchestrator.py` | Mode switching; authoring mode cannot mutate (binding boundary) |
| `demo_wp7_provenance.py` | Automatic provenance — the model volunteers nothing |
| `demo_wp9_body_descriptions.py` | Descriptions living in the Anytype body (ADR 010) |
| `demo_claude_driver.py` | WP6 acceptance with the real Claude driver (needs a subscription session) |
