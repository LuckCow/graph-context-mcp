---
name: evals-run
description: Run the WP16 behavioral eval harness (real-model runs over the orchestrator, graded) and interpret the results — baseline runs, prompt/model experiments, regression comparisons. Use when asked to "run the evals", measure a prompt/profile/model change, or check for behavior regressions. Live runs spend Claude subscription quota. For AUTHORING a new case from a dogfooding failure, use /evals-add instead.
allowed-tools: Bash(python -m evals run --driver scripted*) Bash(python -m evals compare *) Bash(python -m pytest tests/evals *) Bash(ls evals/runs*) Read
---

Budget gate: live runs (`python -m evals run` without `--driver scripted`)
spend subscription quota and are deliberately NOT pre-approved — the
permission prompt is the user's authorization step. Never work around it
(no wrapping in scripts, no driver default changes); state the expected
scale (cases × trials, plus judge calls if `--judge`) when asking.

The eval harness (ADR 024, docs/TESTING.md "Behavioral evals") grades real
`Orchestrator.handle_message` turns against fresh in-memory runtimes.
Workflow:

1. **Validate cheaply first.** Before any live run:
   `python -m evals run --driver scripted` — must be all-ok
   (it replays every case's reference script; a failure here is a broken
   case or grader, not model behavior, and must be fixed before spending
   quota).

2. **Run live with a label.** Ask what the experiment is if not obvious
   from context, then:
   `python -m evals run --label <experiment> [--case <id>] [--trials N] [--judge] [--model M] [--effort E]`
   Default to the full dataset with per-case trial counts; add `--judge`
   when subjective cases (rubric-bearing) matter to the question being
   asked. A full live run takes several minutes and real quota — for a
   quick sanity signal, offer `--case <id> --trials 1` first.

3. **Compare when a baseline exists.** Find prior runs with
   `ls -dt evals/runs/*/` and diff:
   `python -m evals compare <baseline-dir> <candidate-dir>`
   Nonzero exit = pass-rate regression.

4. **Interpret, don't just paste.** Read `<run>/results.json` for failed
   grades and judge reasoning; lead with what regressed/improved and the
   probable cause (grader too strict vs. behavior change). For any failed
   live case, read that session's slice of `<run>/turns.jsonl` before
   concluding — the transcript, not the grade, is the evidence. Offer the
   viewer for the user's own reading:
   `python -m graph_context.orchestrator.turn_log_server --log <run>/turns.jsonl`

Adding cases is /evals-add's job — it interrogates the failure and aligns
graders with the user before any TOML is written. Hand over rather than
improvising a case here.
