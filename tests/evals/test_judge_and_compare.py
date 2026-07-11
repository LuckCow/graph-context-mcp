"""The judge's SDK-free seams and the run comparator.

The judge's model call is subscription-gated and lives behind
``_ask_claude``; everything testable without it -- verdict parsing (model
text, so leniency is the contract) and prompt rendering -- is pinned
here. ``compare_runs`` is exercised over two scripted runs of the same
smoke suite: identical runs must show no regression, and a doctored
baseline must flag one.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.compare import compare_runs
from evals.judge import parse_verdict, render_judge_prompt
from evals.runner import RunConfig, run_evals

from tests.evals.test_harness_smoke import SMOKE_CASES


class TestVerdictParsing:
    def test_a_clean_json_verdict_parses(self) -> None:
        verdict = parse_verdict(
            '{"reasoning": "Looked her up, created nothing.", "pass": true, "score": 0.9}'
        )
        assert verdict.passed and verdict.score == 0.9 and not verdict.error
        assert "created nothing" in verdict.reasoning

    def test_json_wrapped_in_prose_still_parses(self) -> None:
        verdict = parse_verdict(
            'Here is my grading:\n{"reasoning": "no", "pass": false}\nDone.'
        )
        assert not verdict.passed
        assert verdict.score == 0.0  # defaults from the boolean

    def test_scores_clamp_into_the_unit_interval(self) -> None:
        assert parse_verdict('{"pass": true, "score": 7}').score == 1.0

    def test_garbage_becomes_an_errored_fail_never_a_crash(self) -> None:
        for text in ("no json here", '{"pass": "yes"}', "{broken"):
            verdict = parse_verdict(text)
            assert not verdict.passed and verdict.error


class TestJudgePrompt:
    async def test_prompt_carries_rubric_conversation_and_end_state(
        self, tmp_path: Path
    ) -> None:
        # A real scripted trial provides the TrialRecord shape; re-grading
        # it into a prompt must surface every section the rubric needs.
        from evals.dataset import load_suite
        from evals.runner import _run_trial

        suite = load_suite(SMOKE_CASES.parent / "world_building.toml")
        case = next(c for c in suite.cases if c.id == "no_duplicate_on_existing_character")
        from graph_context.orchestrator.turn_log import TurnLog

        record = await _run_trial(
            suite, case, RunConfig(driver="scripted"),
            TurnLog(tmp_path / "turns.jsonl"), trial=1,
        )
        prompt = render_judge_prompt(case, record)
        assert "<rubric>" in prompt and "duplicate" in prompt
        assert "user: Add Mira, the exiled siege engineer" in prompt
        assert "find_node (executed)" in prompt
        assert "Character 'Mira': Exiled siege engineer of Brakk." in prompt


class TestCompareRuns:
    async def test_identical_runs_show_no_regression(self, tmp_path: Path) -> None:
        a = await run_evals(RunConfig(
            cases=str(SMOKE_CASES), driver="scripted", out_root=str(tmp_path / "a")
        ))
        b = await run_evals(RunConfig(
            cases=str(SMOKE_CASES), driver="scripted", out_root=str(tmp_path / "b")
        ))
        report, regressed = compare_runs(str(a.run_dir), str(b.run_dir))
        assert not regressed
        assert "No regressions." in report

    async def test_a_pass_rate_drop_is_flagged(self, tmp_path: Path) -> None:
        a = await run_evals(RunConfig(
            cases=str(SMOKE_CASES), driver="scripted", out_root=str(tmp_path / "a")
        ))
        b = await run_evals(RunConfig(
            cases=str(SMOKE_CASES), driver="scripted", out_root=str(tmp_path / "b")
        ))
        doctored = json.loads((a.run_dir / "results.json").read_text())
        for case in doctored["cases"]:
            if case["id"] == "smoke_scripted_create":
                case["pass_rate"] = 1.0  # baseline passed...
        candidate = json.loads((b.run_dir / "results.json").read_text())
        for case in candidate["cases"]:
            if case["id"] == "smoke_scripted_create":
                case["pass_rate"] = 0.0  # ...candidate dropped to zero
        (a.run_dir / "results.json").write_text(json.dumps(doctored))
        (b.run_dir / "results.json").write_text(json.dumps(candidate))
        report, regressed = compare_runs(str(a.run_dir), str(b.run_dir))
        assert regressed
        assert "REGRESSED: smoke_scripted_create" in report
