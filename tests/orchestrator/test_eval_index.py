"""The eval-artifact index: tolerant scanning, aggregation, path safety.

Fixtures build an eval root on disk the way the harness does (results.json
+ turns.jsonl per run, TOML case files) and pin the payload shapes the
inspection UI reads. Corruption cases prove the "warn, never crash"
posture that separates this reader from the harness's strict loader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_context.orchestrator import eval_index


def _write_run(
    root: Path, run_id: str, cases: list[dict], fmt: int | None = 2,
    transcript: bool = True,
) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    results: dict = {
        "run": {"driver": "scripted", "model": "(scripted)", "label": "",
                "started": "2026-07-11T00:00:00+00:00",
                "finished": "2026-07-11T00:00:01+00:00",
                "ok": all(c.get("ok") for c in cases)},
        "cases": cases,
    }
    if fmt is not None:
        results["format"] = fmt
    (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
    if transcript:
        (run_dir / "turns.jsonl").write_text("{}\n", encoding="utf-8")
    return run_dir


def _case(case_id: str, ok: bool, trials: int = 1, **extra) -> dict:
    return {
        "id": case_id, "suite": "s", "must_fail": False, "skipped": False,
        "ok": ok, "pass_rate": 1.0 if ok else 0.0, "pass_any": ok,
        "pass_all": ok,
        "trials": [
            {"trial": n, "passed": ok, "grades": [], "judge": None,
             "cost_usd": 0.01}
            for n in range(1, trials + 1)
        ],
        **extra,
    }


CASE_TOML = """\
[suite]
name = "smoke"
profile = "fiction"
embedder = "off"

[[case]]
id = "who_is_mira"
trials = 2

[[case.seed.node]]
type = "Character"
name = "Mira"

[[case.turn]]
user = "Who is Mira?"

[case.expect.reply]
must_mention = ["Mira"]

[case.judge]
rubric = "Pass only if the reply describes Mira."

[[case.script]]
reply = "Mira is a character."
"""


@pytest.fixture
def eval_root(tmp_path) -> Path:
    root = tmp_path / "evals"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "smoke.toml").write_text(CASE_TOML, encoding="utf-8")
    return root


class TestSafeChild:
    def test_plain_names_resolve_under_the_root(self, tmp_path) -> None:
        child = eval_index.safe_child(tmp_path, "20260711T000000Z-label")
        assert child == (tmp_path / "20260711T000000Z-label").resolve()

    @pytest.mark.parametrize("name", [
        "..", "../x", "a/b", "a\\b", ".hidden", "", "x" * 200, "a b",
    ])
    def test_traversal_shapes_are_rejected(self, tmp_path, name) -> None:
        assert eval_index.safe_child(tmp_path, name) is None


class TestSummary:
    def test_a_defined_never_run_case_shows_without_results(self, eval_root) -> None:
        data = eval_index.summary(eval_root)
        (case,) = data["cases"]
        assert case["id"] == "who_is_mira"
        assert case["defined"] is True
        assert case["suite"] == "smoke"
        assert case["has_judge"] is True
        assert case["latest"] is None and case["history"] == []
        assert data["runs"] == [] and data["warnings"] == []

    def test_history_is_newest_first_and_latest_tracks_it(self, eval_root) -> None:
        _write_run(eval_root, "20260710T000000Z", [_case("who_is_mira", False)])
        _write_run(eval_root, "20260711T000000Z", [_case("who_is_mira", True)])
        (case,) = eval_index.summary(eval_root)["cases"]
        assert [h["run_id"] for h in case["history"]] == [
            "20260711T000000Z", "20260710T000000Z",
        ]
        assert case["latest"]["ok"] is True

    def test_a_run_only_case_appears_undefined(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [_case("renamed_case", True)])
        cases = {c["id"]: c for c in eval_index.summary(eval_root)["cases"]}
        assert cases["renamed_case"]["defined"] is False
        assert cases["who_is_mira"]["defined"] is True

    def test_run_headers_aggregate_verdicts_and_cost(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [
            _case("who_is_mira", True, trials=2),
            _case("other", False),
            {"id": "skipped_one", "skipped": True, "ok": True, "trials": []},
        ])
        (run,) = eval_index.summary(eval_root)["runs"]
        assert run["cases_total"] == 2  # skipped cases sit outside the count
        assert run["cases_ok"] == 1
        assert run["cases_skipped"] == 1
        assert run["cost_usd"] == pytest.approx(0.03)
        assert run["has_transcript"] is True

    def test_corrupt_artifacts_warn_and_never_crash(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [_case("who_is_mira", True)])
        (eval_root / "runs" / "bad-run").mkdir()
        (eval_root / "runs" / "bad-run" / "results.json").write_text("{nope")
        (eval_root / "runs" / "in-progress").mkdir()  # no results.json yet
        (eval_root / "cases" / "broken.toml").write_text("[nope", encoding="utf-8")
        data = eval_index.summary(eval_root)
        assert len(data["runs"]) == 1  # the good run still renders
        assert any("bad-run" in w for w in data["warnings"])
        assert any("in-progress" in w for w in data["warnings"])
        assert any("broken.toml" in w for w in data["warnings"])

    def test_duplicate_case_ids_across_files_warn(self, eval_root) -> None:
        (eval_root / "cases" / "twin.toml").write_text(
            '[suite]\nname = "twin"\nprofile = "fiction"\nembedder = "off"\n'
            '[[case]]\nid = "who_is_mira"\n', encoding="utf-8",
        )
        data = eval_index.summary(eval_root)
        assert any("duplicate case id" in w for w in data["warnings"])

    def test_a_missing_eval_root_is_the_empty_state(self, tmp_path) -> None:
        data = eval_index.summary(tmp_path / "absent")
        assert data["cases"] == [] and data["runs"] == []


class TestRunDetail:
    def test_format_1_trials_get_synthesized_sessions(self, eval_root) -> None:
        # Pre-format-2 runs never wrote the session key; the runner's
        # formula has been `<case>#t<trial>` since the harness landed.
        _write_run(eval_root, "20260710T000000Z",
                   [_case("who_is_mira", True, trials=2)], fmt=None)
        detail = eval_index.run_detail(eval_root, "20260710T000000Z")
        sessions = [t["session"]
                    for t in detail["results"]["cases"][0]["trials"]]
        assert sessions == ["who_is_mira#t1", "who_is_mira#t2"]

    def test_unknown_or_unsafe_ids_are_none(self, eval_root) -> None:
        assert eval_index.run_detail(eval_root, "absent") is None
        assert eval_index.run_detail(eval_root, "../cases") is None


class TestCaseDetail:
    def test_definition_and_history_are_joined(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [_case("who_is_mira", True)])
        detail = eval_index.case_detail(eval_root, "who_is_mira")
        assert detail["turns"] == ["Who is Mira?"]
        assert detail["judge_rubric"].startswith("Pass only if")
        assert detail["expectations"]["reply"]["must_mention"] == ["Mira"]
        assert detail["seed_nodes"] == 1
        assert detail["has_script"] is True
        (entry,) = detail["history"]
        assert entry["id"] == "20260711T000000Z"
        assert entry["outcome"]["ok"] is True

    def test_an_entirely_unknown_case_is_none(self, eval_root) -> None:
        assert eval_index.case_detail(eval_root, "absent") is None

    def test_a_history_only_case_still_renders(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [_case("renamed", True)])
        detail = eval_index.case_detail(eval_root, "renamed")
        assert detail["defined"] is False
        assert len(detail["history"]) == 1


class TestRunLogPath:
    def test_resolves_inside_the_runs_directory(self, eval_root) -> None:
        _write_run(eval_root, "20260711T000000Z", [_case("who_is_mira", True)])
        path = eval_index.run_log_path(eval_root, "20260711T000000Z")
        assert path == eval_root / "runs" / "20260711T000000Z" / "turns.jsonl"

    def test_unsafe_and_absent_ids_are_none(self, eval_root) -> None:
        assert eval_index.run_log_path(eval_root, "../cases") is None
        assert eval_index.run_log_path(eval_root, "absent") is None
