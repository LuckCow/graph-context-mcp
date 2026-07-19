"""RuleEngine service (WP31, ADR 039): the tick contract.

The invariants under test: the first tick only baselines (nothing fires
on restart), transitions fire exactly once, the engine's own writes
never read as transitions (no cascades, no loops), and rule bookkeeping
(status / last error / last fired) is written change-only.
"""

from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from graph_context.application.rule_engine import RuleEngine
from graph_context.domain import rules
from graph_context.domain.models import NodeDraft, NodeId
from graph_context.errors import GraphContextError
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.ports.script_runner import ScriptEffect, ScriptOutcome


class Clock:
    """A settable now() so tests move time explicitly."""

    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment)


class RecordingRepository(InMemoryGraphRepository):
    """The fake plus write observability: which nodes were updated, an
    injectable per-node write failure, and a body-fetch counter (the
    script cache's observable)."""

    def __init__(self) -> None:
        super().__init__()
        self.updated: list[NodeId] = []
        self.fail_for: set[NodeId] = set()
        self.body_fetches: list[NodeId] = []

    async def update_node(self, node_id: NodeId, **kwargs: object):  # type: ignore[no-untyped-def, override]
        if node_id in self.fail_for:
            raise GraphContextError("the store rejected the write")
        self.updated.append(node_id)
        return await super().update_node(node_id, **kwargs)  # type: ignore[arg-type]

    async def fetch_body(self, node_id: NodeId) -> str:
        self.body_fetches.append(node_id)
        return await super().fetch_body(node_id)


class FakeScriptRunner:
    """Port fake: records (script, payload) calls, returns a canned
    outcome or raises a canned error."""

    def __init__(
        self,
        outcome: ScriptOutcome | None = None,
        error: GraphContextError | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.outcome = outcome or ScriptOutcome()
        self.error = error

    async def run(self, script: str, payload: Any) -> ScriptOutcome:
        self.calls.append((script, dict(payload)))
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture
def repository() -> RecordingRepository:
    return RecordingRepository()


@pytest.fixture
def clock() -> Clock:
    return Clock("2026-07-19 10:00:00")


@pytest.fixture
def engine(repository: RecordingRepository, clock: Clock) -> RuleEngine:
    return RuleEngine(repository, now=clock)


def rule_draft(name: str = "stamp completion", **overrides: str) -> NodeDraft:
    fields = {
        rules.FIELD_TARGET_TYPE: "Task",
        rules.FIELD_WATCH_PROPERTY: "Done",
        rules.FIELD_CONDITION: "Changed to true",
        rules.FIELD_ACTION: "Set property to now",
        rules.FIELD_ACTION_PROPERTY: "Completion date",
    }
    fields.update(overrides)
    return NodeDraft(
        type="gc_rule", name=name, summary="an automation",
        fields={k: v for k, v in fields.items() if v},
    )


def task_draft(name: str, **fields: str) -> NodeDraft:
    return NodeDraft(type="Task", name=name, summary="a task", fields=fields)


class TestBaseline:
    async def test_the_first_tick_never_fires_even_on_true_values(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft())
        task = await repository.create_node(task_draft("ship it", Done="true"))
        report = await engine.run_tick()
        assert report.fired == ()
        assert "Completion date" not in repository.graph.node(task.id).fields

    async def test_an_object_created_mid_run_baselines_silently(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft())
        await engine.run_tick()
        # Born with Done already ticked: a state, not a transition.
        born_done = await repository.create_node(task_draft("old", Done="true"))
        report = await engine.run_tick()
        assert report.fired == ()
        assert "Completion date" not in repository.graph.node(born_done.id).fields


class TestFiring:
    async def test_false_to_true_stamps_completion_and_last_fired(
        self, engine: RuleEngine, repository: RecordingRepository, clock: Clock,
    ) -> None:
        rule = await repository.create_node(rule_draft())
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        clock.advance_to("2026-07-19 10:00:05")
        report = await engine.run_tick()
        assert [(f.rule_id, f.node_id) for f in report.fired] == [(rule.id, task.id)]
        assert repository.graph.node(task.id).fields["Completion date"] == (
            "2026-07-19 10:00:05"
        )
        stored = repository.graph.node(rule.id)
        assert stored.fields[rules.FIELD_LAST_FIRED] == "2026-07-19 10:00:05"
        assert report.errors == ()

    async def test_a_transition_fires_exactly_once(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft())
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        assert len((await engine.run_tick()).fired) == 1
        assert (await engine.run_tick()).fired == ()  # steady state: no re-fire

    async def test_the_engines_own_write_never_reads_as_a_transition(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        # A second rule watches the very property the first one writes:
        # the classic cascade/loop shape. The baseline absorbs the
        # engine's write, so it must never fire.
        await repository.create_node(rule_draft())
        await repository.create_node(rule_draft(
            "cascade probe",
            **{
                rules.FIELD_WATCH_PROPERTY: "Completion date",
                rules.FIELD_CONDITION: "Changed",
                rules.FIELD_ACTION: "Set property value",
                rules.FIELD_ACTION_PROPERTY: "Flag",
                rules.FIELD_ACTION_VALUE: "cascaded",
            },
        ))
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        report = await engine.run_tick()
        assert [f.rule_name for f in report.fired] == ["stamp completion"]
        for _ in range(3):
            assert (await engine.run_tick()).fired == ()
        assert "Flag" not in repository.graph.node(task.id).fields

    async def test_watch_property_matches_case_insensitively(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft(
            **{rules.FIELD_WATCH_PROPERTY: "done"}
        ))
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        assert len((await engine.run_tick()).fired) == 1

    async def test_a_date_format_target_gets_the_bare_local_date(
        self, engine: RuleEngine, repository: RecordingRepository, clock: Clock,
    ) -> None:
        """R2 (ADR 039, live-probed): date properties reject naive
        timestamps, so set-property-to-now writes YYYY-MM-DD there."""
        from graph_context.domain.models import FieldSpec

        rule_fields = {
            rules.FIELD_TARGET_TYPE: "text",
            rules.FIELD_WATCH_PROPERTY: "text",
            rules.FIELD_CONDITION: "select",
            rules.FIELD_ACTION: "select",
            rules.FIELD_ACTION_PROPERTY: "text",
            rules.FIELD_LAST_FIRED: "text",
            rules.FIELD_LAST_ERROR: "text",
            rules.FIELD_STATUS: "select",
        }
        repository.stage_space_vocabulary(field_catalog=[
            FieldSpec(name="Done", format="checkbox", key="done"),
            FieldSpec(name="Completion date", format="date", key="completion_date"),
            *(
                FieldSpec(name=key, format=fmt, key=key)
                for key, fmt in rule_fields.items()
            ),
        ])
        await repository.create_node(rule_draft(
            **{rules.FIELD_TARGET_TYPE: "Character"}  # a catalog-known type
        ))
        task = await repository.create_node(NodeDraft(
            type="Character", name="ship it", summary="s",
        ))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        clock.advance_to("2026-07-19 23:59:59")
        report = await engine.run_tick()
        assert len(report.fired) == 1
        assert repository.graph.node(task.id).fields["completion_date"] == "2026-07-19"

    async def test_set_property_value_writes_the_configured_value(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft(
            "archive done",
            **{
                rules.FIELD_WATCH_PROPERTY: "Status",
                rules.FIELD_CONDITION: "Changed",
                rules.FIELD_ACTION: "Set property value",
                rules.FIELD_ACTION_PROPERTY: "Stage",
                rules.FIELD_ACTION_VALUE: "Review",
            },
        ))
        task = await repository.create_node(task_draft("ship it", Status="Todo"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Status": "Doing"})
        report = await engine.run_tick()
        assert len(report.fired) == 1
        assert repository.graph.node(task.id).fields["Stage"] == "Review"


class TestUncheckOthers:
    def exclusive_rule(self) -> NodeDraft:
        return rule_draft(
            "one default",
            **{
                rules.FIELD_TARGET_TYPE: "Project",
                rules.FIELD_WATCH_PROPERTY: "Default",
                rules.FIELD_CONDITION: "",
                rules.FIELD_ACTION: "Uncheck others of type",
                rules.FIELD_ACTION_PROPERTY: "",
            },
        )

    async def project(
        self, repository: RecordingRepository, name: str, default: str = ""
    ) -> NodeId:
        fields = {"Default": default} if default else {}
        node = await repository.create_node(NodeDraft(
            type="Project", name=name, summary="a project", fields=fields,
        ))
        return node.id

    async def test_checking_one_unchecks_the_others(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(self.exclusive_rule())
        first = await self.project(repository, "alpha", default="true")
        second = await self.project(repository, "beta")
        await engine.run_tick()
        await repository.update_node(second, fields={"Default": "true"})
        report = await engine.run_tick()
        assert len(report.fired) == 1
        assert repository.graph.node(first).fields["Default"] == "false"
        assert repository.graph.node(second).fields["Default"] == "true"

    async def test_already_exclusive_state_writes_nothing(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(self.exclusive_rule())
        winner = await self.project(repository, "alpha")
        await self.project(repository, "beta")
        await engine.run_tick()
        await repository.update_node(winner, fields={"Default": "true"})
        repository.updated.clear()
        await engine.run_tick()
        # One write flips no sibling (none were true); the only other
        # write is the rule's own last-fired stamp.
        assert winner not in repository.updated

    async def test_same_tick_double_flip_is_deterministic_last_writer_wins(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(self.exclusive_rule())
        first = await self.project(repository, "alpha")
        second = await self.project(repository, "beta")  # higher node id
        await engine.run_tick()
        await repository.update_node(first, fields={"Default": "true"})
        await repository.update_node(second, fields={"Default": "true"})
        await engine.run_tick()
        # Node-id order: the later object is the last writer and wins.
        assert repository.graph.node(first).fields["Default"] == "false"
        assert repository.graph.node(second).fields["Default"] == "true"
        assert (await engine.run_tick()).fired == ()  # and the dust settles


def script_rule_draft(script: str = "set(trigger, 'Note', 'fired')") -> NodeDraft:
    return NodeDraft(
        type="gc_rule", name="scripted", summary="an automation",
        fields={
            rules.FIELD_TARGET_TYPE: "Task",
            rules.FIELD_WATCH_PROPERTY: "Done",
            rules.FIELD_CONDITION: "Changed to true",
            rules.FIELD_ACTION: "Run script",
        },
        body=f"```python\n{script}\n```",
    )


class TestScriptAction:
    """WP32 (ADR 040): the run script action through the engine."""

    def engine_with(
        self, repository: RecordingRepository, clock: Clock,
        runner: FakeScriptRunner,
    ) -> RuleEngine:
        return RuleEngine(repository, now=clock, script_runner=runner)

    async def fire(
        self, repository: RecordingRepository, engine: RuleEngine
    ) -> NodeId:
        """Baseline, then flip a fresh task's Done -- one pending fire."""
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        return task.id

    async def test_the_payload_carries_the_world_and_the_transition(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        runner = FakeScriptRunner()
        engine = self.engine_with(repository, clock, runner)
        rule = await repository.create_node(script_rule_draft("pass"))
        other = await repository.create_node(NodeDraft(
            type="Project", name="Roadmap", summary="p",
        ))
        task_id = await self.fire(repository, engine)
        clock.advance_to("2026-07-19 10:00:05")
        report = await engine.run_tick()
        assert [f.action for f in report.fired] == [rules.ACTION_RUN_SCRIPT]
        script, payload = runner.calls[0]
        assert script == "pass"
        assert payload["trigger"] == task_id
        assert (payload["before"], payload["after"]) == ("", "true")
        assert payload["now"] == "2026-07-19 10:00:05"
        assert payload["rule"]["name"] == "scripted"
        exported = {n["id"] for n in payload["nodes"]}
        assert task_id in exported and other.id in exported
        assert rule.id not in exported  # infra stays out of the snapshot

    async def test_queued_effects_apply_through_the_repository(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        project = await repository.create_node(NodeDraft(
            type="Project", name="Roadmap", summary="p",
        ))
        runner = FakeScriptRunner(ScriptOutcome(sets=(
            ScriptEffect(node_id=project.id, property="Open tasks", value="3"),
        )))
        engine = self.engine_with(repository, clock, runner)
        await repository.create_node(script_rule_draft())
        await self.fire(repository, engine)
        report = await engine.run_tick()
        assert len(report.fired) == 1
        assert repository.graph.node(project.id).fields["Open tasks"] == "3"

    async def test_a_bad_effect_applies_nothing_and_records_the_error(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        project = await repository.create_node(NodeDraft(
            type="Project", name="Roadmap", summary="p",
        ))
        runner = FakeScriptRunner(ScriptOutcome(sets=(
            ScriptEffect(node_id=project.id, property="Open tasks", value="3"),
            ScriptEffect(node_id="ghost", property="x", value="1"),
        )))
        engine = self.engine_with(repository, clock, runner)
        await repository.create_node(script_rule_draft())
        await self.fire(repository, engine)
        report = await engine.run_tick()
        assert report.fired == ()
        assert "unknown object" in report.errors[0].message
        # All-or-nothing: the valid first effect was NOT applied.
        assert "Open tasks" not in repository.graph.node(project.id).fields

    async def test_an_infra_target_is_rejected(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        rule = await repository.create_node(script_rule_draft())
        runner = FakeScriptRunner(ScriptOutcome(sets=(
            ScriptEffect(
                node_id=rule.id, property=rules.FIELD_STATUS, value="Paused",
            ),
        )))
        engine = self.engine_with(repository, clock, runner)
        await self.fire(repository, engine)
        report = await engine.run_tick()
        assert "system objects" in report.errors[0].message

    async def test_the_effect_cap_is_enforced_parent_side(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        task = await repository.create_node(task_draft("target"))
        runner = FakeScriptRunner(ScriptOutcome(sets=tuple(
            ScriptEffect(node_id=task.id, property=f"p{i}", value="1")
            for i in range(21)
        )))
        engine = self.engine_with(repository, clock, runner)
        await repository.create_node(script_rule_draft())
        await self.fire(repository, engine)
        report = await engine.run_tick()
        assert "cap" in report.errors[0].message

    async def test_a_runner_failure_consumes_the_transition(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        runner = FakeScriptRunner(
            error=GraphContextError("the script exceeded its 5s time limit")
        )
        engine = self.engine_with(repository, clock, runner)
        rule = await repository.create_node(script_rule_draft())
        await self.fire(repository, engine)
        report = await engine.run_tick()
        assert report.fired == ()
        stored = repository.graph.node(rule.id)
        assert "time limit" in stored.fields[rules.FIELD_LAST_ERROR]
        runner.error = None
        assert (await engine.run_tick()).fired == ()  # consumed, no retry

    async def test_no_runner_means_a_teaching_error(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        engine = RuleEngine(repository, now=clock)  # no script_runner
        rule = await repository.create_node(script_rule_draft())
        report = await engine.run_tick()
        assert "not available" in report.errors[0].message
        assert repository.graph.node(rule.id).fields[
            rules.FIELD_STATUS
        ] == rules.STATUS_ERROR

    async def test_a_missing_fence_errors_then_heals_when_added(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        runner = FakeScriptRunner()
        engine = self.engine_with(repository, clock, runner)
        rule = await repository.create_node(replace(
            script_rule_draft(), body="just prose, no code",
        ))
        report = await engine.run_tick()
        assert "```python" in report.errors[0].message
        await repository.update_node(rule.id, body="```python\npass\n```")
        report = await engine.run_tick()
        assert report.healed == (rule.id,)

    async def test_script_writes_never_fire_rules(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        # The script writes the very property the rule watches -- the
        # self-loop shape. The rebaseline absorbs it.
        task = await repository.create_node(task_draft("ship it"))
        runner = FakeScriptRunner(ScriptOutcome(sets=(
            ScriptEffect(node_id=task.id, property="Done", value="false"),
        )))
        engine = self.engine_with(repository, clock, runner)
        await repository.create_node(script_rule_draft())
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        assert len((await engine.run_tick()).fired) == 1
        for _ in range(3):
            assert (await engine.run_tick()).fired == ()
        assert repository.graph.node(task.id).fields["Done"] == "false"

    async def test_the_snapshot_cap_errors_loudly(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        from graph_context.domain.models import Node

        for i in range(2001):
            repository.graph.upsert_node(Node(
                id=f"bulk-{i:05d}", type="Task", name=f"t{i}", summary="s",
            ))
        runner = FakeScriptRunner()
        engine = self.engine_with(repository, clock, runner)
        await repository.create_node(script_rule_draft())
        await engine.run_tick()
        await repository.update_node("bulk-00000", fields={"Done": "true"})
        report = await engine.run_tick()
        assert "too large" in report.errors[0].message

    async def test_the_script_cache_keys_on_the_modified_stamp(
        self, repository: RecordingRepository, clock: Clock,
    ) -> None:
        from graph_context.domain.models import Node

        runner = FakeScriptRunner()
        engine = self.engine_with(repository, clock, runner)
        rule = await repository.create_node(script_rule_draft("pass"))

        def stamp(value: str) -> None:
            node = repository.graph.node(rule.id)
            repository.graph.upsert_node(Node(
                id=node.id, type=node.type, name=node.name,
                summary=node.summary, fields=node.fields,
                type_key=node.type_key, role=node.role, modified_at=value,
            ))

        # The fake's "" stamp -> refetch every tick (fetch is free there).
        await engine.run_tick()
        await engine.run_tick()
        assert repository.body_fetches.count(rule.id) == 2
        # A real stamp -> one fetch, then cache hits...
        stamp("2026-07-19 10:00:00")
        repository.body_fetches.clear()
        await engine.run_tick()
        await engine.run_tick()
        assert repository.body_fetches.count(rule.id) == 1
        # ...until the stamp moves (a body edit).
        stamp("2026-07-19 11:00:00")
        await engine.run_tick()
        assert repository.body_fetches.count(rule.id) == 2


class TestLifecycle:
    async def test_a_paused_rule_is_inert(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        await repository.create_node(rule_draft(
            **{rules.FIELD_STATUS: "Paused"}
        ))
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        assert (await engine.run_tick()).fired == ()

    async def test_an_unconfigured_template_is_skipped_silently(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        template = await repository.create_node(NodeDraft(
            type="gc_rule", name="Example Automation Rule",
            summary="the explainer", fields={},
        ))
        report = await engine.run_tick()
        assert report.errors == ()
        assert rules.FIELD_STATUS not in repository.graph.node(template.id).fields

    async def test_a_broken_rule_records_error_once_and_never_crashes(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        rule = await repository.create_node(rule_draft(
            "broken", **{rules.FIELD_CONDITION: "on tuesdays"}
        ))
        report = await engine.run_tick()
        assert [p.rule_id for p in report.errors] == [rule.id]
        stored = repository.graph.node(rule.id)
        assert stored.fields[rules.FIELD_STATUS] == rules.STATUS_ERROR
        assert "on tuesdays" in stored.fields[rules.FIELD_LAST_ERROR]
        repository.updated.clear()
        second = await engine.run_tick()
        assert second.errors == ()  # change-only: no re-report...
        assert rule.id not in repository.updated  # ...and no re-write

    async def test_a_fixed_rule_heals_to_active_and_then_fires(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        rule = await repository.create_node(rule_draft(
            "broken", **{rules.FIELD_CONDITION: "on tuesdays"}
        ))
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()  # records the Error
        stored = repository.graph.node(rule.id)
        fixed = {**dict(stored.fields), rules.FIELD_CONDITION: "Changed to true"}
        await repository.update_node(rule.id, fields=fixed)
        report = await engine.run_tick()
        assert report.healed == (rule.id,)
        healed = repository.graph.node(rule.id)
        assert healed.fields[rules.FIELD_STATUS] == rules.STATUS_ACTIVE
        assert healed.fields.get(rules.FIELD_LAST_ERROR, "") == ""
        # And the healed rule works: the first tick after healing
        # baselined the task, so a fresh flip fires.
        await repository.update_node(task.id, fields={"Done": "true"})
        assert len((await engine.run_tick()).fired) == 1

    async def test_a_failed_action_consumes_the_transition(
        self, engine: RuleEngine, repository: RecordingRepository,
    ) -> None:
        rule = await repository.create_node(rule_draft())
        task = await repository.create_node(task_draft("ship it"))
        await engine.run_tick()
        await repository.update_node(task.id, fields={"Done": "true"})
        repository.fail_for.add(task.id)
        report = await engine.run_tick()
        assert report.fired == ()
        assert [p.rule_id for p in report.errors] == [rule.id]
        stored = repository.graph.node(rule.id)
        assert "rejected" in stored.fields[rules.FIELD_LAST_ERROR]
        assert stored.fields.get(rules.FIELD_STATUS, "") != rules.STATUS_ERROR
        # The transition was consumed: no retry once writes work again.
        repository.fail_for.clear()
        assert (await engine.run_tick()).fired == ()
        assert "Completion date" not in repository.graph.node(task.id).fields
