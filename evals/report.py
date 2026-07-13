"""Run results: aggregation, ``results.json``, and the human report.

A case is OK when any trial passed (pass@k > 0); a ``must_fail`` case --
the grader-honesty fixture -- is OK only when every trial failed, because
a passing must-fail case means a grader stopped grading. ``pass_all``
(pass^k) is reported alongside for reliability reading: a case can be OK
by pass@k while pass^k warns that the behavior is flaky.

``results.json`` is the machine-comparable artifact (the ``compare``
subcommand diffs two of them); ``report.md`` is the human summary. Both
live next to the run's ``turns.jsonl`` so a result always travels with
the transcripts that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.graders import GradeResult
from evals.judge import JudgeVerdict


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    trial: int
    passed: bool
    grades: tuple[GradeResult, ...]
    decisions: int = 0
    executed_calls: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    output_tokens: int = 0
    final_reply: str = ""
    # Format 2 (inspection server): the transcript address and the model's
    # standing inputs, so a reviewer never has to reconstruct them.
    session: str = ""  # this trial's session key in the run's turns.jsonl
    system_prompt: str = ""  # the exact prompt the driver sent
    bound_tools: tuple[str, ...] = ()
    harness_error: str = ""  # the harness (not the model) broke the trial
    # The judge's SEPARATE verdict (--judge + a case rubric); it reports
    # alongside the code grades and never overrides them.
    judge: JudgeVerdict | None = None


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    suite: str
    must_fail: bool = False
    skipped: bool = False
    mode: str = ""  # the mode the case ran in ("" = the profile default)
    judge_rubric: str = ""  # the rubric as it was at run time
    trials: tuple[TrialOutcome, ...] = ()

    @property
    def pass_any(self) -> bool:
        return any(t.passed for t in self.trials)

    @property
    def pass_all(self) -> bool:
        return bool(self.trials) and all(t.passed for t in self.trials)

    @property
    def pass_rate(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.passed for t in self.trials) / len(self.trials)

    @property
    def ok(self) -> bool:
        """The run-level verdict (drives the exit code)."""
        if self.skipped:
            return True
        if self.must_fail:
            return not self.pass_any
        return self.pass_any


@dataclass(frozen=True, slots=True)
class RunResult:
    run_dir: Path
    driver: str
    model: str
    label: str
    started: datetime
    finished: datetime
    cases: tuple[CaseOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(case.ok for case in self.cases)


def write_run_artifacts(result: RunResult) -> None:
    (result.run_dir / "results.json").write_text(
        json.dumps(_as_json(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (result.run_dir / "report.md").write_text(_as_markdown(result), encoding="utf-8")


def load_results(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "results.json"
    if not path.exists():
        raise FileNotFoundError(f"{run_dir} has no results.json (not an eval run?)")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _as_json(result: RunResult) -> dict[str, Any]:
    return {
        # Bumped when the shape gains fields the inspection server reads;
        # readers treat missing keys as "an older run", never an error.
        "format": 2,
        "run": {
            "driver": result.driver,
            "model": result.model,
            "label": result.label,
            "started": result.started.isoformat(timespec="seconds"),
            "finished": result.finished.isoformat(timespec="seconds"),
            "ok": result.ok,
        },
        "cases": [
            {
                "id": case.case_id,
                "suite": case.suite,
                "must_fail": case.must_fail,
                "skipped": case.skipped,
                "mode": case.mode,
                "judge_rubric": case.judge_rubric,
                "ok": case.ok,
                "pass_rate": round(case.pass_rate, 3),
                "pass_any": case.pass_any,
                "pass_all": case.pass_all,
                "trials": [
                    {
                        "trial": t.trial,
                        "passed": t.passed,
                        "session": t.session,
                        "system_prompt": t.system_prompt,
                        "bound_tools": list(t.bound_tools),
                        "harness_error": t.harness_error,
                        "decisions": t.decisions,
                        "executed_calls": t.executed_calls,
                        "latency_s": round(t.latency_s, 3),
                        "cost_usd": round(t.cost_usd, 6),
                        "output_tokens": t.output_tokens,
                        "grades": [
                            {
                                "grader": g.grader,
                                "passed": g.passed,
                                "detail": g.detail,
                            }
                            for g in t.grades
                        ],
                        "judge": (
                            {
                                "passed": t.judge.passed,
                                "score": t.judge.score,
                                "reasoning": t.judge.reasoning,
                                "error": t.judge.error,
                            }
                            if t.judge is not None else None
                        ),
                        "final_reply": t.final_reply,
                    }
                    for t in case.trials
                ],
            }
            for case in result.cases
        ],
    }


def _as_markdown(result: RunResult) -> str:
    ran = [c for c in result.cases if not c.skipped]
    skipped = [c for c in result.cases if c.skipped]
    lines = [
        f"# Eval run {result.run_dir.name}",
        "",
        f"- driver: `{result.driver}` (model: `{result.model}`)",
        f"- started {result.started.isoformat(timespec='seconds')}, "
        f"finished {result.finished.isoformat(timespec='seconds')}",
        f"- verdict: **{'OK' if result.ok else 'FAILED'}** "
        f"({sum(c.ok for c in ran)}/{len(ran)} cases ok"
        + (f", {len(skipped)} skipped" if skipped else "") + ")",
        "",
        "| case | verdict | pass@k | pass^k | judge | trials | calls | decisions | cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for case in ran:
        calls = _mean(t.executed_calls for t in case.trials)
        decisions = _mean(t.decisions for t in case.trials)
        cost = sum(t.cost_usd for t in case.trials)
        verdict = "ok" if case.ok else "FAIL"
        if case.must_fail:
            verdict += " (must-fail)"
        judged = [t.judge for t in case.trials if t.judge is not None]
        judge_cell = (
            f"{sum(v.passed for v in judged)}/{len(judged)}" if judged else "-"
        )
        lines.append(
            f"| {case.case_id} | {verdict} | {'yes' if case.pass_any else 'no'} "
            f"| {'yes' if case.pass_all else 'no'} | {judge_cell} "
            f"| {len(case.trials)} | {calls:.1f} | {decisions:.1f} | ${cost:.4f} |"
        )
    failures = [
        (case, trial, grade)
        for case in ran
        for trial in case.trials
        for grade in trial.grades
        if not grade.passed
    ]
    if failures:
        lines += ["", "## Failed grades", ""]
        for case, trial, failed in failures:
            lines.append(
                f"- `{case.case_id}` trial {trial.trial} -- "
                f"**{failed.grader}**: {failed.detail}"
            )
    judge_flags = [
        (case, trial)
        for case in ran
        for trial in case.trials
        if trial.judge is not None and (not trial.judge.passed or trial.judge.error)
    ]
    if judge_flags:
        lines += ["", "## Judge findings", ""]
        for case, trial in judge_flags:
            verdict = trial.judge
            assert verdict is not None
            note = verdict.error or f"score {verdict.score:.2f}: {verdict.reasoning}"
            lines.append(f"- `{case.case_id}` trial {trial.trial} -- {note}")
    if skipped:
        lines += ["", "## Skipped", ""]
        lines += [f"- `{c.case_id}` (no script for this driver)" for c in skipped]
    lines += [
        "",
        "Review: `python -m graph_context.orchestrator.inspect_server` then "
        f"open `http://127.0.0.1:8765/#/runs/{result.run_dir.name}` "
        "(transcripts, grades, prompts).",
        "",
    ]
    return "\n".join(lines)


def _mean(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0
