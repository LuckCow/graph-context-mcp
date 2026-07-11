---
name: evals-add
description: Turn a dogfooding failure into a new eval case — when the user reports incorrect or unwanted bot behavior (usually with an expectation of what should have happened), or asks to "add an eval" / "pin this behavior". Gathers evidence from the turn log, aligns the expected behavior with the user BEFORE writing anything, then authors + validates the case. Running/comparing existing evals is /evals-run.
allowed-tools: Bash(python -m evals run --driver scripted*) Bash(python -m pytest tests/evals*) Read
---

Convert one observed failure into one eval case in `evals/cases/*.toml`
(format: docs/TESTING.md "Behavioral evals"; loader `evals/dataset.py`).
The case is a RED TEST: it should fail live today and pass once the
behavior is fixed. One behavior per case.

## 1. Gather evidence

Find the failing turn(s) in `logs/turns.jsonl` (parse it; match on the
user's description — each turn is user → llm_turn/tool_result records →
turn_end). Extract: profile + active mode, the exact user text, the
trajectory (decision count, tool calls with arguments, rejected calls),
the final reply, and the graph effects. If the log doesn't contain it or
several turns could match, show the candidates and ask which one.

## 2. Align on the expectation — ask, don't assume (MANDATORY)

Being on the same page about expectations is the whole point of this
skill; be forward about asking. Never infer the expectation silently from
the failure. Restate the failure in one sentence, then propose the case as
a concrete grader list and put it to the user with AskUserQuestion before
writing any TOML. Cover at minimum:

- **The pass condition**: which end-state assertions define success
  (nodes/edges/fields/session state), and what must NOT exist.
- **Trajectory strictness**: proposed `max_decisions` /
  `max_executed_calls` ceilings and forbidden tools — offer strict vs.
  lenient options with the log's actual numbers as context.
- **Balanced twin**: every "should not X" expectation needs a twin where
  X is correct (the mode-boundary pair in `mode_boundary.toml` is the
  template) — propose the twin's wording.
- **Subjective parts**: if honesty/tone/faithfulness matters, propose the
  judge rubric wording and ask whether it captures the intent. When the
  expectation is INVISIBLE to code graders (fabricated success, dishonest
  replies), propose `[case.judge] required = true` — the judge then gates
  the trial and runs on every live run.
- Trial count (flaky behaviors deserve 3).

Iterate until the user signs off on the grader list. Anything they
correct, restate back once.

## 3. Author

- Suite file by theme (`evals/cases/`); the suite `profile` must match
  the failure's; start a new file if no theme fits.
- Seed the MINIMAL world reproducing the preconditions. Custom or
  misconfigured space modes → `[[case.modes]]` `{name, goal, mutating}`
  (`case.mode` takes the slug, e.g. `task_creation_mode`). Prior
  conversation → `seed_memory` on the first turn.
- Turn text = the log's user message, lightly cleaned; pin any names the
  graders reference (graders match names exactly).
- Graders assert outcomes, never call sequences. Field checks:
  `fields_truthy` / `fields_falsy` on node refs (falsy = absent or a
  knob-off spelling: "", "0", "false", "no", "off").
- Write the `[[case.script]]` reference solution — the trajectory a good
  agent would take. It must satisfy the graders; if it can't, the case is
  unsolvable or a grader is wrong.

## 4. Validate

`python -m evals run --driver scripted --case <id>` must be all-ok, and
`python -m pytest tests/evals -q` green (the CI replay re-proves every
shipped script). Fix dataset errors — they name the file, case, and key.

## 5. Red-test proof (offer, never spend silently)

Offer a live run of just this case (`python -m evals run --case <id>` —
the permission prompt is the user's budget authorization). Expected
result: FAIL, reproducing the report. If it passes live, the case isn't
pinning the failure — go back to step 2 with the transcript in hand
(grader too loose? turn wording drifted from the log?).

## 6. Hand off

The case is the expectation, not the fix. Fixing is prompt/profile
engineering (tool docstrings, mode goals — they're prompts); afterwards,
/evals-run compare shows the case flipping red → green without
regressions elsewhere.
