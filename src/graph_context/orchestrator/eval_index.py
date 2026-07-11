"""Read-side index over eval artifacts for the inspection server.

The eval harness (the repo-root ``evals`` package) WRITES
``evals/runs/<ts>[-label]/results.json`` and defines cases in
``evals/cases/*.toml``; this module READS those artifacts back for the
inspection UI. It deliberately does not import ``evals`` -- that package
is repo-tooling outside the shipped wheel and it imports the orchestrator,
so the dependency arrow only points this way at the file-format level:
plain ``json``/``tomllib``, no code sharing.

Two consequences of being the read side:

* **Tolerant, never strict.** ``dataset.py`` validates hard because a bad
  case must not cost a live run; a VIEWER of historical artifacts must
  render everything else even when one file is corrupt, so parse failures
  degrade to entries in the payload's ``warnings`` list.
* **Format-versioned.** ``results.json`` carries ``"format"`` (2 adds
  per-trial ``session``/``system_prompt``/``bound_tools``); missing keys
  read as "an older run". The one format-1 backfill is the trial's
  transcript session key, synthesized from the runner's historical
  ``<case_id>#t<trial>`` formula.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HISTORY_CAP = 20  # dashboard trend length per case
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_child(root: Path, name: str) -> Path | None:
    """``root/name`` when ``name`` is one plain path segment, else None.

    The URL-to-filesystem gate for ``/runs/<id>/...`` and
    ``/api/...``: a conservative identifier alphabet (no leading dot, no
    separators) plus a resolved containment check. None means 404."""
    if not _SAFE_NAME.fullmatch(name):
        return None
    child = (root / name).resolve()
    if not child.is_relative_to(root.resolve()):
        return None
    return child


def runs_root(eval_root: Path) -> Path:
    return eval_root / "runs"


def cases_root(eval_root: Path) -> Path:
    return eval_root / "cases"


def run_log_path(eval_root: Path, run_id: str) -> Path | None:
    """The run's transcript file, or None when the id is unsafe/absent."""
    run_dir = safe_child(runs_root(eval_root), run_id)
    if run_dir is None or not run_dir.is_dir():
        return None
    return run_dir / "turns.jsonl"


def summary(eval_root: Path) -> dict[str, Any]:
    """The dashboard payload: every known case with its latest result and
    recent history, every run's header line, and the scan warnings.

    Cases are the union of the TOML-defined set and ids found only in
    runs (``defined: false`` -- the definition was renamed or deleted).
    Deliberately excludes trial payloads (grades, replies, prompts); those
    live behind ``run_detail``/``case_detail``.
    """
    warnings: list[str] = []
    defined = _load_case_defs(eval_root, warnings)
    runs = _load_runs(eval_root, warnings)

    outcomes: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for run in runs:  # newest-first, so each history list is too
        for case in run["results"].get("cases", []):
            outcomes.setdefault(str(case.get("id")), []).append((run, case))

    cases: list[dict[str, Any]] = []
    for case_id in sorted(set(defined) | set(outcomes)):
        definition = defined.get(case_id)
        history = [
            _history_entry(run, case) for run, case in outcomes.get(case_id, [])
        ]
        cases.append({
            "id": case_id,
            "defined": definition is not None,
            **(definition or {}),
            "latest": history[0] if history else None,
            "history": history[:HISTORY_CAP],
        })
    return {
        "eval_root": str(eval_root),
        "cases": cases,
        "runs": [_run_header(run) for run in runs],
        "warnings": warnings,
    }


def run_detail(eval_root: Path, run_id: str) -> dict[str, Any] | None:
    """One run's results.json, normalized (sessions synthesized for
    format-1 runs); None when the id is unsafe, absent, or unreadable."""
    run_dir = safe_child(runs_root(eval_root), run_id)
    if run_dir is None or not run_dir.is_dir():
        return None
    results = _read_results(run_dir, [])
    if results is None:
        return None
    return {
        "id": run_id,
        "has_transcript": (run_dir / "turns.jsonl").exists(),
        "results": results,
    }


def case_detail(eval_root: Path, case_id: str) -> dict[str, Any] | None:
    """One case's definition plus its outcome in every run that ran it.

    None only when the id is entirely unknown -- an undefined id that
    appears in runs still gets a history-only page, and a defined case
    that never ran still gets its definition."""
    warnings: list[str] = []
    definition = _load_case_defs(eval_root, warnings, full=True).get(case_id)
    history: list[dict[str, Any]] = []
    for run in _load_runs(eval_root, warnings):
        for case in run["results"].get("cases", []):
            if str(case.get("id")) == case_id:
                history.append({**_run_header(run), "outcome": case})
    if definition is None and not history:
        return None
    return {
        "id": case_id,
        "defined": definition is not None,
        **(definition or {}),
        "history": history,
        "warnings": warnings,
    }


# -- runs ----------------------------------------------------------------


def _load_runs(eval_root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Every readable run, NEWEST first (the timestamped directory names
    sort chronologically, so reverse-lexicographic is reverse-time)."""
    root = runs_root(eval_root)
    if not root.is_dir():
        return []
    runs = []
    for run_dir in sorted(root.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        results = _read_results(run_dir, warnings)
        if results is not None:
            runs.append({
                "id": run_dir.name,
                "has_transcript": (run_dir / "turns.jsonl").exists(),
                "results": results,
            })
    return runs


def _read_results(run_dir: Path, warnings: list[str]) -> dict[str, Any] | None:
    path = run_dir / "results.json"
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warnings.append(
            f"run {run_dir.name}: no results.json (still running, or not a run)"
        )
        return None
    except (OSError, ValueError) as err:
        warnings.append(f"run {run_dir.name}: unreadable results.json ({err})")
        return None
    if not isinstance(results, dict):
        warnings.append(f"run {run_dir.name}: results.json is not an object")
        return None
    for case in results.get("cases", []):
        for trial in case.get("trials", []):
            if not trial.get("session"):
                # Format 1 predates the explicit key; the runner's session
                # formula has been stable since the harness landed.
                trial["session"] = f"{case.get('id')}#t{trial.get('trial')}"
    return results


def _run_header(run: dict[str, Any]) -> dict[str, Any]:
    results = run["results"]
    meta = results.get("run", {})
    cases = [c for c in results.get("cases", []) if not c.get("skipped")]
    return {
        "id": run["id"],
        "label": meta.get("label", ""),
        "driver": meta.get("driver", ""),
        "model": meta.get("model", ""),
        "started": meta.get("started", ""),
        "finished": meta.get("finished", ""),
        "ok": meta.get("ok"),
        "format": results.get("format", 1),
        "cases_total": len(cases),
        "cases_ok": sum(bool(c.get("ok")) for c in cases),
        "cases_skipped": sum(
            bool(c.get("skipped")) for c in results.get("cases", [])
        ),
        "cost_usd": round(sum(
            t.get("cost_usd", 0.0) or 0.0
            for c in results.get("cases", []) for t in c.get("trials", [])
        ), 4),
        "has_transcript": run["has_transcript"],
    }


def _history_entry(run: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "finished": run["results"].get("run", {}).get("finished", ""),
        "ok": case.get("ok"),
        "skipped": bool(case.get("skipped")),
        "pass_rate": case.get("pass_rate"),
        "pass_all": case.get("pass_all"),
        "trials": len(case.get("trials", [])),
    }


# -- case definitions ------------------------------------------------------


def _load_case_defs(
    eval_root: Path, warnings: list[str], full: bool = False
) -> dict[str, dict[str, Any]]:
    """Case id -> display metadata from the TOML definitions.

    ``full`` adds the case-detail extras (all turns, expectations,
    staged modes, seed counts). Duplicate ids across files are a dataset
    bug the strict loader would reject -- surfaced here as a warning, the
    last definition wins."""
    root = cases_root(eval_root)
    if not root.is_dir():
        return {}
    defs: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            warnings.append(f"cases/{path.name}: unreadable ({err})")
            continue
        suite = data.get("suite", {})
        for case in data.get("case", []):
            case_id = str(case.get("id", "")).strip()
            if not case_id:
                warnings.append(f"cases/{path.name}: a case has no id")
                continue
            if case_id in defs:
                warnings.append(
                    f"cases/{path.name}: duplicate case id {case_id!r} "
                    f"(also in {defs[case_id]['file']})"
                )
            defs[case_id] = _case_def(case, suite, path.name, full)
    return defs


def _case_def(
    case: dict[str, Any], suite: dict[str, Any], file_name: str, full: bool
) -> dict[str, Any]:
    turns = [str(t.get("user", "")) for t in case.get("turn", [])]
    judge = case.get("judge", {})
    definition: dict[str, Any] = {
        "suite": str(suite.get("name", "")),
        "profile": str(suite.get("profile", "")),
        "file": file_name,
        "mode": str(case.get("mode", "")),
        "trials": case.get("trials", 1),
        "must_fail": bool(case.get("must_fail", False)),
        "has_judge": bool(judge),
        "judge_required": bool(judge.get("required", False)),
        "prompt": turns[0] if turns else "",
    }
    if full:
        seed = case.get("seed", {})
        definition.update({
            "turns": turns,
            "expectations": case.get("expect", {}),
            "judge_rubric": str(judge.get("rubric", "")),
            "modes": case.get("modes", []),
            "seed_nodes": len(seed.get("node", [])),
            "seed_edges": len(seed.get("edge", [])),
            "has_script": bool(case.get("script")),
        })
    return definition
