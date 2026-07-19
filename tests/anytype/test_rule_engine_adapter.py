"""The rule engine over the real adapter + mock server (WP31, ADR 039).

The full loop the Anytype bot runs: a human edits an object in the UI
(``edit_object_directly``), ``resync`` pulls the change into the index,
``run_tick`` fires the rule, and the effect lands in the STORE as native
properties -- while the engine's own writes stay invisible to the next
resync (self-write suppression intact).
"""

from datetime import datetime

import pytest

from graph_context.application.rule_engine import RuleEngine
from graph_context.domain import rules
from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.anytype import mapping


class Clock:
    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment)


@pytest.fixture
def clock() -> Clock:
    return Clock("2026-07-19 10:00:00")


async def seed_task_type(client, repo) -> None:
    """A user's Task type with a Done checkbox and a completion field."""
    await client.create_type({
        "key": "task", "name": "Task", "plural_name": "Tasks",
        "layout": "basic",
        "properties": [
            {"key": "done", "name": "Done", "format": "checkbox"},
            {"key": "completion_date", "name": "Completion date", "format": "text"},
            {"key": "default_flag", "name": "Default", "format": "checkbox"},
        ],
    })
    await repo.hydrate()  # pick up the new type + properties


async def stored_properties(client, object_id: str) -> dict[str, object]:
    obj = await client.get_object(object_id)
    return {
        entry["key"]: entry.get(mapping._VALUE_FIELD.get(entry.get("format", ""), ""))
        for entry in obj.get("properties", [])
    }


class TestStampCompletion:
    async def test_a_ui_checkbox_flip_lands_a_completion_stamp_in_the_store(
        self, mock, client, repo, clock
    ) -> None:
        await seed_task_type(client, repo)
        rule = await repo.create_node(NodeDraft(
            type="Automation Rule", name="stamp completion",
            summary="Done -> completion date",
            fields={
                rules.FIELD_TARGET_TYPE: "Task",
                rules.FIELD_WATCH_PROPERTY: "Done",
                rules.FIELD_CONDITION: "Changed to true",
                rules.FIELD_ACTION: "Set property to now",
                rules.FIELD_ACTION_PROPERTY: "Completion date",
            },
        ))
        task = await repo.create_node(NodeDraft(
            type="Task", name="ship it", summary="a task",
        ))
        engine = RuleEngine(repo, now=clock)
        await engine.run_tick()  # baseline

        # The human ticks Done in the Anytype UI.
        mock.edit_object_directly(task.id, set_property=mapping.property_entry(
            "done", "checkbox", True,
        ))
        assert task.id in await repo.resync()
        clock.advance_to("2026-07-19 10:00:05")
        report = await engine.run_tick()

        assert [(f.rule_id, f.node_id) for f in report.fired] == [(rule.id, task.id)]
        stored = await stored_properties(client, task.id)
        assert stored["completion_date"] == "2026-07-19 10:00:05"
        rule_stored = await stored_properties(client, rule.id)
        assert rule_stored[mapping.PROP_RULE_LAST_FIRED] == "2026-07-19 10:00:05"

    async def test_engine_writes_are_invisible_to_the_next_resync(
        self, mock, client, repo, clock
    ) -> None:
        await seed_task_type(client, repo)
        await repo.create_node(NodeDraft(
            type="Automation Rule", name="stamp completion",
            summary="Done -> completion date",
            fields={
                rules.FIELD_TARGET_TYPE: "Task",
                rules.FIELD_WATCH_PROPERTY: "Done",
                rules.FIELD_CONDITION: "Changed to true",
                rules.FIELD_ACTION: "Set property to now",
                rules.FIELD_ACTION_PROPERTY: "Completion date",
            },
        ))
        task = await repo.create_node(NodeDraft(
            type="Task", name="ship it", summary="a task",
        ))
        engine = RuleEngine(repo, now=clock)
        await engine.run_tick()
        mock.edit_object_directly(task.id, set_property=mapping.property_entry(
            "done", "checkbox", True,
        ))
        await repo.resync()
        report = await engine.run_tick()
        assert report.fired
        # The action write and the rule bookkeeping both went through the
        # repository: the watermark tracked them, so nothing reads as an
        # out-of-band change -- and the next tick sees no transition.
        assert await repo.resync() == frozenset()
        assert (await engine.run_tick()).fired == ()


class TestScriptedRule:
    """WP32 (ADR 040): the run script action over the real adapter and
    the REAL subprocess runner -- no fakes below the mock server."""

    def _runner(self):  # type: ignore[no-untyped-def]
        from graph_context.infrastructure.sandbox.runner import (
            SubprocessScriptRunner,
        )
        return SubprocessScriptRunner(timeout_seconds=10.0)

    async def _scripted_rule(self, repo, script: str):  # type: ignore[no-untyped-def]
        return await repo.create_node(NodeDraft(
            type="Automation Rule", name="scripted rollup",
            summary="counts open tasks",
            fields={
                rules.FIELD_TARGET_TYPE: "Task",
                rules.FIELD_WATCH_PROPERTY: "Done",
                rules.FIELD_CONDITION: "Changed",
                rules.FIELD_ACTION: "Run script",
            },
            body=f"```python\n{script}\n```",
        ))

    async def test_a_script_fire_lands_writes_in_the_store(
        self, mock, client, repo, clock
    ) -> None:
        await seed_task_type(client, repo)
        await self._scripted_rule(repo, (
            "open_tasks = [t for t in objects(type='Task')\n"
            "              if field(t, 'Done') != 'true']\n"
            "set(trigger, 'Completion date', 'open: %d' % len(open_tasks))\n"
            "log('counted')"
        ))
        task = await repo.create_node(NodeDraft(
            type="Task", name="ship it", summary="a task",
        ))
        await repo.create_node(NodeDraft(
            type="Task", name="still open", summary="a task",
        ))
        engine = RuleEngine(repo, now=clock, script_runner=self._runner())
        await engine.run_tick()  # baseline

        mock.edit_object_directly(task.id, set_property=mapping.property_entry(
            "done", "checkbox", True,
        ))
        await repo.resync()
        report = await engine.run_tick()

        assert [f.action for f in report.fired] == ["run script"]
        stored = await stored_properties(client, task.id)
        assert stored["completion_date"] == "open: 1"
        # Self-write suppression holds for script writes too.
        assert await repo.resync() == frozenset()
        assert (await engine.run_tick()).fired == ()

    async def test_a_body_edit_swaps_in_the_new_script(
        self, mock, client, repo, clock
    ) -> None:
        await seed_task_type(client, repo)
        rule = await self._scripted_rule(
            repo, "set(trigger, 'Completion date', 'v1')"
        )
        task = await repo.create_node(NodeDraft(
            type="Task", name="ship it", summary="a task",
        ))
        engine = RuleEngine(repo, now=clock, script_runner=self._runner())
        await engine.run_tick()

        # The human rewrites the script in the Anytype editor.
        mock.edit_object_directly(
            rule.id,
            markdown="```python\nset(trigger, 'Completion date', 'v2')\n```",
        )
        await repo.resync()
        mock.edit_object_directly(task.id, set_property=mapping.property_entry(
            "done", "checkbox", True,
        ))
        await repo.resync()
        report = await engine.run_tick()
        assert len(report.fired) == 1
        assert (await stored_properties(client, task.id))[
            "completion_date"
        ] == "v2"


class TestUncheckOthers:
    async def test_exclusivity_across_store_objects(
        self, mock, client, repo, clock
    ) -> None:
        await seed_task_type(client, repo)
        await repo.create_node(NodeDraft(
            type="Automation Rule", name="one default",
            summary="Default stays exclusive",
            fields={
                rules.FIELD_TARGET_TYPE: "Task",
                rules.FIELD_WATCH_PROPERTY: "Default",
                rules.FIELD_ACTION: "Uncheck others of type",
            },
        ))
        alpha = await repo.create_node(NodeDraft(
            type="Task", name="alpha", summary="t",
            fields={"Default": "true"},
        ))
        beta = await repo.create_node(NodeDraft(
            type="Task", name="beta", summary="t",
        ))
        engine = RuleEngine(repo, now=clock)
        await engine.run_tick()  # baseline: alpha true, beta absent

        mock.edit_object_directly(beta.id, set_property=mapping.property_entry(
            "default_flag", "checkbox", True,
        ))
        await repo.resync()
        report = await engine.run_tick()

        assert len(report.fired) == 1
        assert (await stored_properties(client, alpha.id))["default_flag"] is False
        assert (await stored_properties(client, beta.id))["default_flag"] is True
        assert (await engine.run_tick()).fired == ()  # the dust settles
