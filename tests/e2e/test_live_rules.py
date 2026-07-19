"""Live rule-engine round trip (WP31, ADR 039), plus the R2 probe.

The canonical automation against a real server: a rule object, a task
whose checkbox a "human" (RawApi) flips, resync + tick, and the effect
readable back off the store. Also probes the open R2 question: does a
*date*-format property accept the engine's ISO stamp? (The repo's own
stamps are deliberately text; ``set-property-to-now`` against a native
date property is only claimed if this passes.)

The live server slugifies requested property keys its own way (see the
E2E Mood note in test_live_contract), so properties are addressed by
DISPLAY NAME throughout and resolved to keys via the registry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from graph_context.application.rule_engine import RuleEngine
from graph_context.domain import rules
from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.anytype import mapping


class Clock:
    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now


def _property_value(obj: dict, key: str) -> object:
    for entry in obj.get("properties", []):
        if entry.get("key") == key:
            return entry.get(mapping._VALUE_FIELD.get(entry.get("format", ""), ""))
    return None


async def _ensure_type(repo, name: str, properties: list[dict]) -> None:
    """Find-or-create a test type (reruns: the API cannot delete types).

    An existing type gets missing properties retrofitted (quirk A11:
    the type-update properties list replaces wholesale, so the current
    list rides along) -- a rerun after this suite grew a property must
    not silently run against the old shape.
    """
    client = repo._client  # E2E-only reach-in; the port has no type API
    types = {t["name"]: t async for t in client.list_types()}
    if name not in types:
        await client.create_type({
            "key": name.lower().replace(" ", "_"), "name": name,
            "plural_name": f"{name}s", "layout": "basic",
            "properties": properties,
        })
    else:
        fetched = await client.get_type(types[name]["id"])
        current = fetched.get("properties", [])
        have = {entry.get("name") for entry in current}
        missing = [p for p in properties if p["name"] not in have]
        if missing:
            await client.update_type(types[name]["id"], {
                "name": fetched["name"],
                "plural_name": fetched.get("plural_name") or f"{fetched['name']}s",
                "layout": fetched.get("layout", "basic"),
                "properties": [
                    {"key": e["key"], "name": e["name"], "format": e["format"]}
                    for e in current
                ] + missing,
            })
    await repo.hydrate()  # refresh registry + index


async def test_checkbox_flip_fires_the_rule_end_to_end(repo, raw_api):
    await _ensure_type(repo, "E2E Task", [
        {"key": "e2e_done", "name": "E2E Done", "format": "checkbox"},
        {"key": "e2e_completed", "name": "E2E Completed", "format": "text"},
    ])
    done_key = repo.registry.field_property("E2E Done").key
    completed_key = repo.registry.field_property("E2E Completed").key
    rule = await repo.create_node(NodeDraft(
        type="Automation Rule", name="e2e stamp completion", summary="s",
        fields={
            rules.FIELD_TARGET_TYPE: "E2E Task",
            rules.FIELD_WATCH_PROPERTY: "E2E Done",
            rules.FIELD_CONDITION: "Changed to true",
            rules.FIELD_ACTION: "Set property to now",
            rules.FIELD_ACTION_PROPERTY: "E2E Completed",
        },
    ))
    task = await repo.create_node(NodeDraft(
        type="E2E Task", name="ship it", summary="a task",
    ))
    clock = Clock("2026-07-19 10:00:05")
    engine = RuleEngine(repo, now=clock)
    await engine.run_tick()  # baseline

    # Same-second edits are indistinguishable (S3): let the flip land later.
    await asyncio.sleep(1.5)
    raw_api.set_property(
        task.id, mapping.property_entry(done_key, "checkbox", True)
    )
    assert task.id in await repo.resync()
    report = await engine.run_tick()

    assert [(f.rule_id, f.node_id) for f in report.fired] == [(rule.id, task.id)]
    stored = raw_api.get(task.id)
    assert _property_value(stored, completed_key) == "2026-07-19 10:00:05"
    rule_stored = raw_api.get(rule.id)
    assert _property_value(
        rule_stored, mapping.PROP_RULE_LAST_FIRED
    ) == "2026-07-19 10:00:05"
    # Self-write suppression: the engine's writes are not out-of-band.
    assert await repo.resync() == frozenset()
    assert (await engine.run_tick()).fired == ()


async def test_scripted_rule_round_trips_live(repo, raw_api):
    """WP32 (ADR 040): the run script action against the real server --
    fenced body in, sandbox subprocess fire, effect readable off the
    store, self-write suppression intact."""
    from graph_context.infrastructure.sandbox.runner import SubprocessScriptRunner

    # A type of its own: the stamp-completion test leaves an ACTIVE rule
    # watching "E2E Task", which would double-fire on a shared type.
    await _ensure_type(repo, "E2E Scripted", [
        {"key": "e2e_s_done", "name": "E2E S Done", "format": "checkbox"},
        {"key": "e2e_s_completed", "name": "E2E S Completed", "format": "text"},
    ])
    done_key = repo.registry.field_property("E2E S Done").key
    completed_key = repo.registry.field_property("E2E S Completed").key
    await repo.create_node(NodeDraft(
        type="Automation Rule", name="e2e scripted stamp", summary="s",
        fields={
            rules.FIELD_TARGET_TYPE: "E2E Scripted",
            rules.FIELD_WATCH_PROPERTY: "E2E S Done",
            rules.FIELD_CONDITION: "Changed to true",
            rules.FIELD_ACTION: "Run script",
        },
        body=(
            "```python\n"
            "set(trigger, 'E2E S Completed', 'scripted at ' + now)\n"
            "log('live fire')\n"
            "```"
        ),
    ))
    task = await repo.create_node(NodeDraft(
        type="E2E Scripted", name="scripted target", summary="a task",
    ))
    engine = RuleEngine(
        repo, now=Clock("2026-07-19 10:00:05"),
        script_runner=SubprocessScriptRunner(timeout_seconds=15.0),
    )
    await engine.run_tick()  # baseline

    await asyncio.sleep(1.5)  # S3: same-second edits are indistinguishable
    raw_api.set_property(
        task.id, mapping.property_entry(done_key, "checkbox", True)
    )
    assert task.id in await repo.resync()
    report = await engine.run_tick()

    assert [
        f.action for f in report.fired if f.rule_name == "e2e scripted stamp"
    ] == ["run script"]
    stored = raw_api.get(task.id)
    assert _property_value(stored, completed_key) == (
        "scripted at 2026-07-19 10:00:05"
    )
    assert await repo.resync() == frozenset()
    assert (await engine.run_tick()).fired == ()


async def test_r2_resolved_a_date_target_gets_the_bare_local_date(repo, raw_api):
    """R2 (ADR 039), answered by a live probe 2026-07-19: a native date
    property REJECTS naive timestamps (space- or T-separated) and accepts
    RFC 3339 only WITH a timezone, or a bare date. The engine's clock is
    naive local (the scheduling convention), so ``set-property-to-now``
    writes the bare LOCAL date to date-format targets -- this certifies
    that write shape end-to-end through a fired rule."""
    await _ensure_type(repo, "E2E Dated", [
        {"key": "e2e_flag", "name": "E2E Flag", "format": "checkbox"},
        {"key": "e2e_when", "name": "E2E When", "format": "date"},
    ])
    flag_key = repo.registry.field_property("E2E Flag").key
    when_key = repo.registry.field_property("E2E When").key
    await repo.create_node(NodeDraft(
        type="Automation Rule", name="e2e date stamp", summary="s",
        fields={
            rules.FIELD_TARGET_TYPE: "E2E Dated",
            rules.FIELD_WATCH_PROPERTY: "E2E Flag",
            rules.FIELD_CONDITION: "Changed to true",
            rules.FIELD_ACTION: "Set property to now",
            rules.FIELD_ACTION_PROPERTY: "E2E When",
        },
    ))
    node = await repo.create_node(NodeDraft(
        type="E2E Dated", name="probe", summary="s",
    ))
    engine = RuleEngine(repo, now=Clock("2026-07-19 10:00:05"))
    await engine.run_tick()  # baseline
    await asyncio.sleep(1.5)  # S3: same-second edits are indistinguishable
    raw_api.set_property(
        node.id, mapping.property_entry(flag_key, "checkbox", True)
    )
    assert node.id in await repo.resync()
    report = await engine.run_tick()
    assert len(report.fired) == 1
    assert report.errors == ()
    value = _property_value(raw_api.get(node.id), when_key)
    assert str(value).startswith("2026-07-19")  # reads back 2026-07-19T00:00:00Z
