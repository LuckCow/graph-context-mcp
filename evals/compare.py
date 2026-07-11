"""Baseline-vs-candidate diff over two runs' ``results.json``.

The comparison that matters is per-case pass-rate movement: any drop is a
flagged regression (and the nonzero exit code), because with trial counts
this small a drop is either real or noise worth reading transcripts over.
Cost and trajectory deltas ride along as context -- a case that still
passes but suddenly needs twice the calls is drifting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.report import load_results


def compare_runs(baseline_dir: str, candidate_dir: str) -> tuple[str, bool]:
    """Markdown report plus a did-anything-regress flag."""
    baseline = load_results(Path(baseline_dir))
    candidate = load_results(Path(candidate_dir))
    base_cases = {c["id"]: c for c in baseline["cases"]}
    cand_cases = {c["id"]: c for c in candidate["cases"]}
    shared = sorted(base_cases.keys() & cand_cases.keys())
    regressions: list[str] = []
    lines = [
        "# Eval comparison",
        "",
        f"- baseline:  `{baseline_dir}` ({_run_label(baseline)})",
        f"- candidate: `{candidate_dir}` ({_run_label(candidate)})",
        "",
        "| case | baseline | candidate | Δ pass | Δ calls | Δ cost |",
        "|---|---|---|---|---|---|",
    ]
    for case_id in shared:
        base, cand = base_cases[case_id], cand_cases[case_id]
        if base.get("skipped") or cand.get("skipped"):
            continue
        delta = cand["pass_rate"] - base["pass_rate"]
        if delta < 0 and not base.get("must_fail"):
            regressions.append(case_id)
        lines.append(
            f"| {case_id} | {base['pass_rate']:.2f} | {cand['pass_rate']:.2f} "
            f"| {delta:+.2f}{' ⚠' if delta < 0 else ''} "
            f"| {_metric_delta(base, cand, 'executed_calls'):+.1f} "
            f"| ${_metric_delta(base, cand, 'cost_usd'):+.4f} |"
        )
    only_base = sorted(base_cases.keys() - cand_cases.keys())
    only_cand = sorted(cand_cases.keys() - base_cases.keys())
    if only_base:
        lines += ["", f"Only in baseline: {', '.join(only_base)}"]
    if only_cand:
        lines += ["", f"Only in candidate: {', '.join(only_cand)}"]
    lines += [
        "",
        f"**{'REGRESSED: ' + ', '.join(regressions) if regressions else 'No regressions.'}**",
        "",
    ]
    return "\n".join(lines), bool(regressions)


def _run_label(results: dict[str, Any]) -> str:
    run = results["run"]
    label = run.get("label") or "unlabelled"
    return f"{label}, {run['driver']}/{run['model']}, {run['started']}"


def _metric_delta(base: dict[str, Any], cand: dict[str, Any], key: str) -> float:
    return _mean_metric(cand, key) - _mean_metric(base, key)


def _mean_metric(case: dict[str, Any], key: str) -> float:
    trials = case.get("trials", [])
    if not trials:
        return 0.0
    return sum(float(t.get(key, 0)) for t in trials) / len(trials)
