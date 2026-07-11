"""The eval runner: one fresh runtime per trial, graded at the end.

Isolation is the non-negotiable (correlated failures from shared state are
infrastructure noise, not model signal): every trial builds its own
in-memory runtime through :func:`composition.build_runtime`, seeds the
case's world through the repository port, runs the conversation through
the real ``Orchestrator.handle_message``, and tears the runtime down. The
run's single shared artifact is its ``turns.jsonl`` -- the same TurnLog
the production bots write, so the existing viewer replays eval transcripts
unchanged.

Driver selection is per RUN, not per case: ``scripted`` plays back each
case's ``[[case.script]]`` (cases without one are skipped -- that's the CI
smoke path) and ``claude`` runs the real model over the user's Claude
subscription (the [orchestrator] extra; never the anthropic API).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.dataset import EvalCase, EvalSuite, load_suites
from evals.graders import GradeResult, grade
from evals.judge import judge_trial
from evals.recording import RecordingDriver, TrialRecord
from evals.report import CaseOutcome, RunResult, TrialOutcome, write_run_artifacts
from evals.seeding import SeedError, seed_world
from graph_context import composition
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.errors import GraphContextError
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.drivers import DecideUsage, LLMDriver, ScriptedDriver
from graph_context.orchestrator.modes import binding_for, load_registry
from graph_context.orchestrator.pipeline import Orchestrator
from graph_context.orchestrator.turn_log import TurnLog

logger = logging.getLogger(__name__)

DRIVERS = ("scripted", "claude")


class RunnerError(Exception):
    """The run itself is misconfigured (unknown driver, empty selection)."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    cases: str = "evals/cases"
    case_filter: str = ""  # exact case id; "" runs everything
    driver: str = "scripted"
    model: str = ""  # claude driver only; "" = the account's CLI default
    effort: str = ""  # claude driver only; "" = the CLI default
    trials_override: int = 0  # 0 = each case's own trial count
    judge: bool = False
    label: str = ""
    out_root: str = "evals/runs"


async def run_evals(config: RunConfig) -> RunResult:
    if config.driver not in DRIVERS:
        raise RunnerError(
            f"unknown driver {config.driver!r}; allowed: {', '.join(DRIVERS)}"
        )
    suites = load_suites(config.cases)
    run_dir = _run_dir(Path(config.out_root), config.label)
    turn_log = TurnLog(run_dir / "turns.jsonl")
    started = datetime.now(UTC)
    outcomes: list[CaseOutcome] = []
    for suite in suites:
        for case in suite.cases:
            if config.case_filter and case.id != config.case_filter:
                continue
            outcomes.append(await _run_case(suite, case, config, turn_log))
    if not outcomes:
        raise RunnerError(
            f"no cases matched (cases={config.cases!r}, "
            f"filter={config.case_filter!r})"
        )
    result = RunResult(
        run_dir=run_dir,
        driver=config.driver,
        model=config.model or ("(scripted)" if config.driver == "scripted" else "(cli default)"),
        label=config.label,
        started=started,
        finished=datetime.now(UTC),
        cases=tuple(outcomes),
    )
    write_run_artifacts(result)
    return result


async def _run_case(
    suite: EvalSuite, case: EvalCase, config: RunConfig, turn_log: TurnLog
) -> CaseOutcome:
    if config.driver == "scripted" and not case.script:
        logger.info("case %s: no [[case.script]]; skipped under --driver scripted",
                    case.id)
        return CaseOutcome(case_id=case.id, suite=suite.name,
                           must_fail=case.must_fail, skipped=True)
    trials = config.trials_override or case.trials
    results = []
    for trial in range(1, trials + 1):
        record = await _run_trial(suite, case, config, turn_log, trial)
        grades = grade(case, record)
        verdict = None
        judged = case.judge is not None and (config.judge or case.judge.required)
        if judged and config.driver != "scripted" and not record.harness_error:
            # Judged while the trial's graph/session are still alive -- the
            # runtime is per-trial and about to be dropped. A REQUIRED
            # judge runs on every live run: the case declared its code
            # graders insufficient (e.g. fabricated success is invisible
            # to them), so skipping the judge would grade the wrong thing.
            verdict = await judge_trial(case, record)
            if case.judge is not None and case.judge.required and not verdict.passed:
                grades.append(GradeResult(
                    "judge.required", False,
                    verdict.error or f"score {verdict.score:.2f}: {verdict.reasoning}",
                ))
        results.append(TrialOutcome(
            trial=trial,
            passed=all(g.passed for g in grades),
            grades=tuple(grades),
            judge=verdict,
            decisions=len(record.decisions),
            executed_calls=len(record.executed_calls),
            latency_s=record.total_latency_s,
            cost_usd=record.total_cost_usd,
            output_tokens=record.total_output_tokens,
            final_reply=record.final_reply,
        ))
        logger.info(
            "case %s trial %d/%d: %s", case.id, trial, trials,
            "pass" if results[-1].passed else "FAIL",
        )
    return CaseOutcome(
        case_id=case.id, suite=suite.name, must_fail=case.must_fail,
        trials=tuple(results),
    )


async def _run_trial(
    suite: EvalSuite,
    case: EvalCase,
    config: RunConfig,
    turn_log: TurnLog,
    trial: int,
) -> TrialRecord:
    session_id = f"{case.id}#t{trial}"
    profile = get_profile(suite.profile)
    journal = MutationJournal() if case.provenance else None
    with _suite_env(suite):
        built = await composition.build_runtime(profile, journal=journal)
    try:
        seed = await seed_world(built.services.repository, case)
        services = await built.services_for(session_id)
        # Case modes ride the same in-space seam real space modes use
        # (modes._parse_in_space validates and slugifies); they override
        # same-named profile modes, so a case can shadow a built-in.
        registry = load_registry(profile, in_space=[
            {"name": m.name, "goal": m.goal, "mutating": m.mutating,
             "origin": f"case {case.id} mode {i}"}
            for i, m in enumerate(case.modes, 1)
        ])
        mode = case.mode or registry.default
        spec = registry.get(mode)
        if spec is None:
            raise RunnerError(
                f"case {case.id!r}: mode {mode!r} is not in profile "
                f"{profile.name!r}; loaded: {', '.join(registry.names())}"
            )
        usage_sink: list[DecideUsage] = []
        recorder = RecordingDriver(_build_inner_driver(case, config, usage_sink.append))
        orchestrator = Orchestrator(
            services=built.services,
            driver=recorder,
            profile=profile,
            registry=registry,
            provenance=(
                IntentRecorder(built.services.repository) if case.provenance else None
            ),
            model_name=config.model or config.driver,
            turn_log=turn_log,
            services_for=built.services_for,
        )
        record = TrialRecord(
            case_id=case.id,
            trial=trial,
            session_id=session_id,
            graph=built.services.repository.graph,
            session=services.session,
            seed_ids=seed.ids,
            baseline_nodes=seed.node_count,
            baseline_edges=seed.edge_count,
            bound_tools=frozenset(binding_for(spec)),
        )
        if case.mode:
            switch = await orchestrator.handle_message(
                session_id, "eval", f"/mode {case.mode}"
            )
            errors = [e.text for e in switch if e.kind == "error"]
            if errors:
                record.harness_error = f"/mode {case.mode} failed: {errors[0]}"
                return record
        if case.turns and case.turns[0].seed_memory:
            await orchestrator.seed_memory(session_id, case.turns[0].seed_memory)
        for turn in case.turns:
            events = await orchestrator.handle_message(session_id, "eval", turn.user)
            record.replies.extend((e.kind, e.text) for e in events)
        record.final_reply = next(
            (text for kind, text in reversed(record.replies) if kind == "reply"), ""
        )
        record.decisions = list(recorder.decisions)
        record.usages = usage_sink
        return record
    except (SeedError, GraphContextError) as err:
        # A trial the harness could not stage is a harness failure with
        # evidence, never a silent skip -- the report shows it as such.
        return TrialRecord(
            case_id=case.id, trial=trial, session_id=session_id,
            graph=built.services.repository.graph,
            session=built.services.session, seed_ids={},
            baseline_nodes=0, baseline_edges=0,
            harness_error=str(err),
        )
    finally:
        await composition.run_teardown(built.teardown)


def _build_inner_driver(
    case: EvalCase,
    config: RunConfig,
    usage_sink: Callable[[DecideUsage], None],
) -> LLMDriver:
    if config.driver == "scripted":
        return ScriptedDriver(case.script)
    # Lazy import: the [orchestrator] extra is absent in CI, and the
    # scripted path must never require it.
    from graph_context.orchestrator.claude_driver import ClaudeAgentDriver

    return ClaudeAgentDriver(
        model=config.model or None,
        effort=config.effort or None,  # type: ignore[arg-type]
        on_result=usage_sink,
    )


@contextmanager
def _suite_env(suite: EvalSuite) -> Iterator[None]:
    """Pin the composition env for one runtime build, then restore it.

    ``build_runtime`` reads its backend and embedder from the environment;
    an eval must get the memory backend and the suite's declared embedder
    no matter what shell exported. Restored afterwards so an in-process
    caller (the smoke test) keeps its environment.
    """
    pinned = {"GC_BACKEND": "memory", "GC_EMBEDDER": suite.embedder}
    saved = {key: os.environ.get(key) for key in pinned}
    os.environ.update(pinned)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_dir(out_root: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{label}" if label else stamp
    path = out_root / name
    path.mkdir(parents=True, exist_ok=False)
    return path
