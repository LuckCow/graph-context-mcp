"""The eval harness's own plumbing, proven in CI without a model.

Drives the real runner over ``evals/cases/smoke.toml`` with the scripted
driver: artifacts appear, passing cases pass, the deliberately failing
case is REPORTED failed (grader honesty), and the scripted path never
imports claude-agent-sdk (CI installs only the [dev] extra, so an import
would crash there anyway -- this pins it before CI has to).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from evals.dataset import DatasetError, load_suite, load_suites
from evals.report import RunResult
from evals.runner import RunConfig, RunnerError, run_evals

SMOKE_CASES = Path(__file__).parents[2] / "evals" / "cases" / "smoke.toml"


@pytest.fixture
async def smoke_run(tmp_path: Path) -> tuple[RunResult, bool]:
    sdk_loaded_before = "claude_agent_sdk" in sys.modules
    result = await run_evals(RunConfig(
        cases=str(SMOKE_CASES), driver="scripted", out_root=str(tmp_path)
    ))
    return result, sdk_loaded_before


class TestScriptedSmokeRun:
    async def test_run_produces_replayable_artifacts(self, smoke_run) -> None:
        result, _ = smoke_run
        assert (result.run_dir / "results.json").exists()
        assert (result.run_dir / "report.md").exists()
        turn_lines = (
            (result.run_dir / "turns.jsonl").read_text().strip().splitlines()
        )
        events = [json.loads(line)["event"] for line in turn_lines]
        # The viewer replays the same record shapes the production bots log.
        assert {"user", "llm_turn", "tool_result", "turn_end"} <= set(events)

    async def test_passing_cases_pass(self, smoke_run) -> None:
        result, _ = smoke_run
        by_id = {case.case_id: case for case in result.cases}
        assert by_id["smoke_scripted_create"].pass_all
        assert by_id["smoke_mode_boundary"].pass_all
        # Field-assertion graders: truthy/falsy on exists and absent refs.
        assert by_id["smoke_field_assertions"].pass_all
        # [[case.modes]] end-to-end: the case mode entered the registry,
        # /mode switched into it, and its non-mutating binding rejected
        # the scripted create.
        assert by_id["smoke_case_mode_read_only"].pass_all

    async def test_must_fail_case_is_reported_failed(self, smoke_run) -> None:
        """Grader honesty: a grader that stops failing would pass this case
        -- and that inversion is exactly what must show up as NOT ok."""
        result, _ = smoke_run
        must_fail = next(c for c in result.cases if c.case_id == "smoke_must_fail")
        assert not must_fail.pass_any  # the graders did fail it
        assert must_fail.ok            # ...which is what must_fail expects
        assert result.ok

    async def test_scripted_path_never_imports_the_sdk(self, smoke_run) -> None:
        _, sdk_loaded_before = smoke_run
        if sdk_loaded_before:
            pytest.skip("another test already imported claude_agent_sdk")
        assert "claude_agent_sdk" not in sys.modules

    async def test_mode_boundary_rejection_is_not_an_execution(self, smoke_run) -> None:
        result, _ = smoke_run
        boundary = next(c for c in result.cases if c.case_id == "smoke_mode_boundary")
        trial = boundary.trials[0]
        # 3 decisions (update attempt, read, reply); only the read executed.
        assert trial.decisions == 3
        assert trial.executed_calls == 1


class TestRunConfigErrors:
    async def test_unknown_driver_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(RunnerError, match="unknown driver"):
            await run_evals(RunConfig(
                cases=str(SMOKE_CASES), driver="gpt", out_root=str(tmp_path)
            ))

    async def test_empty_selection_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(RunnerError, match="no cases matched"):
            await run_evals(RunConfig(
                cases=str(SMOKE_CASES), case_filter="no_such_case",
                driver="scripted", out_root=str(tmp_path),
            ))


class TestDatasetValidation:
    def test_unknown_case_key_names_file_and_case(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(
            '[suite]\nname = "bad"\nprofile = "fiction"\n'
            '[[case]]\nid = "typo_case"\nmoode = "authoring"\n'
            '[[case.turn]]\nuser = "hi"\n'
        )
        with pytest.raises(DatasetError, match=r"typo_case.*moode|moode.*typo_case"):
            load_suite(path)

    def test_case_without_turns_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(
            '[suite]\nname = "bad"\nprofile = "fiction"\n[[case]]\nid = "empty"\n'
        )
        with pytest.raises(DatasetError, match="at least one"):
            load_suite(path)

    def test_a_key_cannot_be_both_truthy_and_falsy(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(
            '[suite]\nname = "bad"\nprofile = "fiction"\n'
            '[[case]]\nid = "contradiction"\n'
            '[[case.turn]]\nuser = "hi"\n'
            "[case.expect.graph]\n"
            'node_exists = [{ name = "X", fields_truthy = ["k"], fields_falsy = ["k"] }]\n'
        )
        with pytest.raises(DatasetError, match="both fields_truthy and fields_falsy"):
            load_suite(path)

    def test_unknown_case_mode_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(
            '[suite]\nname = "bad"\nprofile = "fiction"\n'
            '[[case]]\nid = "bad_mode"\n'
            '[[case.modes]]\nname = "M"\ngoal = "g"\nmutatng = true\n'
            '[[case.turn]]\nuser = "hi"\n'
        )
        with pytest.raises(DatasetError, match=r"bad_mode.*mutatng|mutatng.*bad_mode"):
            load_suite(path)

    def test_all_checked_in_case_files_load(self) -> None:
        """Every shipped case file parses; a broken dataset fails CI here."""
        suites = load_suites(SMOKE_CASES.parent)
        assert suites
        for suite in suites:
            assert suite.cases


class TestReferenceSolutions:
    async def test_every_scripted_case_is_solvable(self, tmp_path: Path) -> None:
        """A case's [[case.script]] is its reference solution: replaying it
        must satisfy the case's own graders (except must_fail fixtures).
        An unsolvable case would only fail live runs -- expensively."""
        result = await run_evals(RunConfig(
            cases=str(SMOKE_CASES.parent), driver="scripted",
            trials_override=1, out_root=str(tmp_path),
        ))
        broken = [
            case.case_id
            for case in result.cases
            if not (case.ok or case.skipped)
        ]
        assert not broken, f"unsolvable or dishonest cases: {broken}"
