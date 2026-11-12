"""SnapshotImporter (WP54, ADR 062): the reconcile contract, on every
repository the system runs on.

Invariants: types mint once and retrofit; the first pass creates every
node the space lacks; an unchanged export writes NOTHING; a changed
leaf updates only that node's differing field; a human edit to an
imported node is written back (the game is the ground truth); a
rejected document is skipped without blocking the others; a holding
that left the inventory goes to zero; one failed write never stops the
pass. Plus the reason it exists: an import write is a transition the
rule engine fires on, with before/after from the game.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from graph_context.application.rule_engine import RuleEngine
from graph_context.application.snapshot_importer import SnapshotImporter
from graph_context.domain import rules
from graph_context.domain.imports import (
    DocumentNodes,
    ImportDocument,
    ImportDocumentError,
    ImportNode,
    ImportType,
)
from graph_context.domain.models import FieldSpec, NodeDraft, PropertyDraft
from graph_context.domain.runescape import RuneScapeExports
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.ports.script_runner import ScriptOutcome

FIXTURES = Path(__file__).parent.parent / "fixtures" / "runescape"


def _fixture(kind: str) -> ImportDocument:
    return ImportDocument(kind, json.loads((FIXTURES / f"{kind}.json").read_text()))


class ListSource:
    """Port fake: whatever documents the test says, mutable between ticks."""

    def __init__(self, *documents: ImportDocument) -> None:
        self.documents = list(documents)

    async def read(self) -> tuple[ImportDocument, ...]:
        return tuple(self.documents)


class SkillFormat:
    """A two-type format small enough to reason about: Skill (stable
    membership) and Holding (zeroes when unmentioned)."""

    types = (
        ImportType(
            "Skill", "Skills",
            (PropertyDraft("Level", "number"), PropertyDraft("Maxed", "checkbox")),
            summary="A skill.",
        ),
        ImportType(
            "Holding", "Holdings", (PropertyDraft("Quantity", "number"),),
            absent_fields={"Quantity": "0"},
        ),
    )

    def nodes_for(self, document: ImportDocument) -> DocumentNodes:
        if document.kind == "skills":
            nodes = tuple(
                ImportNode("Skill", name, {
                    "Level": str(level), "Maxed": "true" if level >= 99 else "false",
                })
                for name, level in document.payload["levels"].items()
            )
            return DocumentNodes(nodes)
        if document.kind == "bag":
            nodes = tuple(
                ImportNode("Holding", name, {"Quantity": str(n)})
                for name, n in document.payload["items"].items()
            )
            return DocumentNodes(nodes, covers=("Holding",))
        raise ImportDocumentError(f"{document.kind}: not a format I know")


def _skills(**levels: int) -> ImportDocument:
    return ImportDocument("skills", {"levels": levels})


def _bag(**items: int) -> ImportDocument:
    return ImportDocument("bag", {"items": items})


async def _noop_sleep(_: float) -> None: ...


@pytest.fixture(params=["memory-open", "memory-scoped", "anytype-mock"])
async def repo(request: pytest.FixtureRequest):
    """Every backend the importer runs on: the open-vocabulary fake
    (dev/CLI), the type-scoped fake (ADR 047 mode), and the mock-backed
    Anytype adapter (raw store keys, checkbox/empty dropping on read)."""
    if request.param == "memory-open":
        yield InMemoryGraphRepository()
        return
    if request.param == "memory-scoped":
        yield InMemoryGraphRepository(field_catalog=[], attachments={})
        return
    mock = MockAnytype()
    config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
    client = AnytypeClient(config, transport=mock.transport, sleep=_noop_sleep)
    await ensure_schema(client)
    repository = AnytypeGraphRepository(client)
    await repository.hydrate()
    yield repository
    await client.aclose()


def _skill(repo: Any, name: str) -> Any:
    (node,) = repo.graph.find_by_name(name, node_type="Skill")
    return node


def _field(repo: Any, node: Any, name: str) -> str:
    """Read back through the catalog like the planner does."""
    specs: Sequence[FieldSpec] = repo.field_catalog().get(node.type, ())
    key = next((s.key or s.name for s in specs if s.name == name), name)
    return node.fields.get(key, node.fields.get(name, ""))


class TestEnsureTypes:
    async def test_missing_types_mint_once_then_retrofit_silently(self, repo: Any) -> None:
        importer = SnapshotImporter(repo, ListSource(), SkillFormat())
        assert await importer.ensure_types() == ("Skill", "Holding")
        assert await importer.ensure_types() == ()
        assert {"Skill", "Holding"} <= set(repo.known_node_types())

    async def test_a_new_property_in_a_later_format_lands_on_the_existing_type(
        self, repo: Any
    ) -> None:
        importer = SnapshotImporter(repo, ListSource(), SkillFormat())
        await importer.ensure_types()
        grown = replace(
            SkillFormat.types[0],
            properties=SkillFormat.types[0].properties + (PropertyDraft("Experience", "number"),),
        )

        class Grown(SkillFormat):
            types = (grown, SkillFormat.types[1])

        await SnapshotImporter(repo, ListSource(), Grown()).ensure_types()
        names = {s.name for s in repo.field_catalog()["Skill"]}
        assert {"Level", "Maxed", "Experience"} <= names


class TestReconcile:
    async def test_the_first_pass_creates_every_node(self, repo: Any) -> None:
        source = ListSource(_skills(Attack=82, Magic=86))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        report = await importer.run_tick()
        assert (report.created, report.updated, report.failed) == (2, 0, ())
        attack = _skill(repo, "Attack")
        assert _field(repo, attack, "Level") == "82"
        assert attack.summary == "A skill."

    async def test_an_unchanged_export_writes_nothing(self, repo: Any) -> None:
        source = ListSource(_skills(Attack=82, Magic=99))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        stamps = {n.id: n.modified_at for n in repo.graph.nodes()}
        report = await importer.run_tick()
        assert not report.wrote
        assert {n.id: n.modified_at for n in repo.graph.nodes()} == stamps

    async def test_a_changed_leaf_updates_that_node_only(self, repo: Any) -> None:
        source = ListSource(_skills(Attack=82, Magic=86))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        magic_before = _skill(repo, "Magic").modified_at
        source.documents = [_skills(Attack=83, Magic=86)]
        report = await importer.run_tick()
        assert (report.created, report.updated) == (0, 1)
        assert _field(repo, _skill(repo, "Attack"), "Level") == "83"
        assert _skill(repo, "Magic").modified_at == magic_before

    async def test_a_human_edit_is_written_back(self, repo: Any) -> None:
        source = ListSource(_skills(Attack=82))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        await repo.update_node(_skill(repo, "Attack").id, fields={"Level": "99"})
        report = await importer.run_tick()
        assert report.updated == 1
        assert _field(repo, _skill(repo, "Attack"), "Level") == "82"

    async def test_a_checkbox_that_stays_false_never_rewrites(self, repo: Any) -> None:
        """The adapter drops an unticked checkbox on read; the planner
        must not see absence as a change every tick."""
        source = ListSource(_skills(Attack=82))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        assert not (await importer.run_tick()).wrote

    async def test_a_holding_that_left_the_bag_goes_to_zero(self, repo: Any) -> None:
        source = ListSource(_bag(Coins=100, Lobster=3))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        source.documents = [_bag(Coins=100)]
        report = await importer.run_tick()
        assert report.updated == 1
        (lobster,) = repo.graph.find_by_name("Lobster", node_type="Holding")
        assert _field(repo, lobster, "Quantity") == "0"
        assert repo.graph.has_node(lobster.id)  # never deleted

    async def test_a_missing_bag_document_does_not_zero_holdings(self, repo: Any) -> None:
        source = ListSource(_bag(Coins=100))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()
        source.documents = []  # the file is mid-write this tick
        assert not (await importer.run_tick()).wrote

    async def test_a_rejected_document_is_skipped_not_fatal(self, repo: Any) -> None:
        source = ListSource(ImportDocument("bank", {}), _skills(Attack=82))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        report = await importer.run_tick()
        assert report.created == 1
        assert len(report.skipped) == 1 and "bank" in report.skipped[0]


class TestReusedSelectOptions:
    async def test_a_pre_existing_title_cased_option_does_not_rewrite_every_tick(
        self,
    ) -> None:
        """The live incident (2026-09-06): the RuneScape space already had
        a Status select with 'Not Started'; create_type reused it, the
        write resolved to that tag, and 83 quests were rewritten on every
        tick because the planner compared 'Not started' != 'Not Started'."""
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
        client = AnytypeClient(config, transport=mock.transport, sleep=_noop_sleep)
        await ensure_schema(client)
        status = await client.create_property(
            {"key": "status", "name": "Status", "format": "select"}
        )
        await client.create_tag(status["id"], {"name": "Not Started", "color": "grey"})
        repo = AnytypeGraphRepository(client)
        await repo.hydrate()

        class QuestFormat:
            types = (ImportType("Quest", "Quests", (PropertyDraft("Status", "select"),)),)

            def nodes_for(self, document: ImportDocument) -> DocumentNodes:
                return DocumentNodes((ImportNode("Quest", "Q", {"Status": "Not started"}),))

        importer = SnapshotImporter(repo, ListSource(ImportDocument("q", {})), QuestFormat())
        await importer.ensure_types()
        assert (await importer.run_tick()).created == 1
        (quest,) = repo.graph.find_by_name("Q", node_type="Quest")
        assert quest.fields["status"] == "Not Started"  # the existing tag won
        assert not (await importer.run_tick()).wrote
        await client.aclose()


class TestFailures:
    async def test_one_failed_write_does_not_stop_the_pass(self) -> None:
        class Flaky(InMemoryGraphRepository):
            async def create_node(self, draft: NodeDraft, links=(), *, create_missing=None):  # type: ignore[no-untyped-def, override]
                if draft.name == "Magic":
                    raise GraphContextError("the store rejected Magic")
                return await super().create_node(draft, links, create_missing=create_missing)

        repo = Flaky()
        source = ListSource(_skills(Attack=82, Magic=86, Prayer=69))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        report = await importer.run_tick()
        assert report.created == 2
        assert len(report.failed) == 1 and "Magic" in report.failed[0]
        # The plan is recomputed from the index: the next tick retries.
        assert (await importer.run_tick()).failed == report.failed


class TestRealExports:
    async def test_the_plugin_export_set_seeds_once_and_then_idles(self, repo: Any) -> None:
        source = ListSource(*(_fixture(k) for k in (
            "character", "quests", "diaries", "combat_achievements",
            "equipment", "inventory",
        )))
        importer = SnapshotImporter(repo, source, RuneScapeExports())
        await importer.ensure_types()
        report = await importer.run_tick()
        assert (report.created, report.failed, report.skipped) == (315, (), ())
        assert not (await importer.run_tick()).wrote
        (character,) = repo.graph.find_by_name("LuckCow", node_type="Character")
        assert _field(repo, character, "Total level") == "1634"


class FakeRunner:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def run(self, script: str, payload: Any) -> ScriptOutcome:
        self.payloads.append(dict(payload))
        return ScriptOutcome()


class TestRulesFireOnImports:
    async def test_an_import_write_is_a_transition_with_the_games_values(self) -> None:
        """The reason the feature exists: Skill · Level changed -> the
        script sees before/after from the game, nothing on the seed."""
        repo = InMemoryGraphRepository()
        runner = FakeRunner()
        engine = RuleEngine(repo, script_runner=runner)
        await repo.create_node(NodeDraft(
            type="gc_rule", name="on level", summary="s",
            fields={
                rules.FIELD_TARGET_TYPE: "Skill",
                rules.FIELD_WATCH_PROPERTY: "Level",
                rules.FIELD_CONDITION: "Changed",
                rules.FIELD_ACTION: "Run script",
            },
            body="```python\nlog('hi')\n```",
        ))
        source = ListSource(_skills(Attack=82))
        importer = SnapshotImporter(repo, source, SkillFormat())
        await importer.ensure_types()
        await importer.run_tick()  # the seed ...
        await engine.run_tick()    # ... baselines silently
        assert runner.payloads == []
        source.documents = [_skills(Attack=83)]
        await importer.run_tick()
        report = await engine.run_tick()
        assert [f.rule_name for f in report.fired] == ["on level"]
        (payload,) = runner.payloads
        assert (payload["before"], payload["after"]) == ("82", "83")
        assert payload["trigger"] == _skill(repo, "Attack").id
        # A human edit never reaches the engine: reconciled first.
        await repo.update_node(_skill(repo, "Attack").id, fields={"Level": "99"})
        await importer.run_tick()
        assert (await engine.run_tick()).fired == ()
