# ADR 024: A behavioral eval harness runs the real pipeline; runs are not tests

**Status:** Accepted (2026-07-10)

## Context

Since WP6 the answer to "does the model use the tools well?" has been
manual dogfooding — the risk register literally prescribes "dogfooding
transcripts; iterate on descriptions." That leaves no way to answer the
question that now matters most: **did this prompt/profile/model change
make the agent better or worse?** Tool docstrings are prompts (WP2),
mode goals are prompts (ADR 015), the context block is a prompt
(ADR 020); all of them get edited, and none of their effects are
measured. The nearest precedent in-repo is the ranking eval golden
(ADR 016): a small TOML case file that turned "vibes" into a review
artifact for weight changes. This ADR extends that discipline to the
orchestrator's end-to-end behavior, following Anthropic's published
guidance for agent evals (small unambiguous task sets drawn from real
usage, code graders first, judge with reasoning, outcomes over paths,
clean per-trial isolation, pass@k/pass^k).

## Decision

1. **Evals are runs, not tests.** They spend Claude subscription quota
   and are nondeterministic, so the entry point is `python -m evals run`
   (top-level `evals/` package), never pytest collection. CI exercises
   only the harness *plumbing*, via a scripted driver over the same code
   path (`tests/evals/`); the scripted path never imports
   claude-agent-sdk.
2. **The harness drives the real seam.** Each trial runs
   `Orchestrator.handle_message` with the real pipeline, mode registry,
   and (live) `ClaudeAgentDriver` against a **fresh in-memory runtime
   per trial** (`composition.build_runtime`, `GC_BACKEND=memory`),
   seeded through the repository port. No mocked pipeline, no shared
   state between trials, provenance off unless a case opts in.
3. **Cases are TOML; graders are outcomes.** `evals/cases/*.toml`
   follows the ranking-golden posture (loud validation, errors name file
   + case). Graders assert on the graph end-state, session state, and
   the final reply, plus *loose* trajectory bounds (forbidden tools,
   call ceilings) — never a prescribed call sequence. A rejected call
   (unbound in the mode) is not an execution; the binding table decides.
4. **Every case's `[[case.script]]` is its reference solution.** Under
   `--driver scripted` it replays through the identical control flow; a
   CI test replays every shipped script and fails on any case whose own
   graders it cannot satisfy — unsolvable cases die cheaply. A
   `must_fail` fixture inverts the check to keep the graders honest.
5. **The turn log is the transcript of record.** Each run writes one
   `turns.jsonl` in its run directory (`evals/runs/<ts>[-label]/`,
   gitignored) using the production `TurnLog`, so the existing viewer
   replays eval transcripts unchanged. `results.json` (machine) and
   `report.md` (human) sit beside it; `python -m evals compare A B`
   diffs two runs and flags pass-rate regressions.
6. **The judge is optional and never overrules code graders — except by
   per-case opt-in.** `--judge` scores rubric-bearing cases in a
   tool-less, settings-less claude-agent-sdk session (subscription, same
   as the driver — never the anthropic SDK), reasoning-first JSON
   verdicts, reported alongside the code grades. A case may declare
   `[case.judge] required = true` for expectations only a judge can
   catch (calibration 2026-07-10: fabricated success — "Created X" with
   no node in the graph — passed every code grader); a required judge
   runs on every live run and its failure fails the trial via a
   synthetic `judge.required` grade.
7. **Usage is observed at the driver, not the pipeline.**
   `ClaudeAgentDriver` gains an optional `on_result` callback fed from
   the SDK's `ResultMessage`, translated to the pure
   `drivers.DecideUsage`; cost/latency/token metrics land in the report
   without the pipeline learning anything about billing.

## Consequences

* Prompt edits (docstrings, goals, context block) and model/effort
  changes get a comparable artifact: run before, run after, `compare`.
* The dataset is a maintained artifact like the ranking golden: grown
  from dogfooding transcripts, calibrated by reading eval transcripts in
  the viewer (`--label` names the experiment).
* Judge rubrics need periodic calibration against one's own verdicts;
  the reasoning field is what makes that audit possible.
* `evals/` sits outside the `graph_context` import-linter root by
  design; the one place it touches claude-agent-sdk is a lazy import on
  the live paths, keeping CI (dev extra only) green.
