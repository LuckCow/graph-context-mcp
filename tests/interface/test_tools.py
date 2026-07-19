"""Tool-layer tests (WP2/WP3): the guarded invariants and the tool policies.

These drive the plain async functions in ``interface/tools.py`` directly
(no MCP client), exactly as the demo script does. They pin the contracts
that the handoff worklist calls out: error messages that echo allowed
values, the default Prose/SessionContext exclusion and ``only_stale``
narrowing. (The per-response context header was removed 2026-07-06 as
token waste; responses now carry the payload alone.)
"""

from __future__ import annotations

import pytest

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.domain import rules as rules_domain
from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface import tools
from graph_context.interface.services import build_services
from tests.conftest import World

pytestmark = pytest.mark.usefixtures("world")


# -- responses are payload-only: no context-header echo ---------------------


async def test_no_context_header_on_success(
    services: tools.Services, world: World
) -> None:
    out = await tools.get_node_tool(services, node_id=world.mira.id)
    assert not out.startswith("[project:")
    assert out.startswith("Mira")


async def test_no_context_header_on_error(services: tools.Services) -> None:
    out = await tools.get_node_tool(services, node_id="does-not-exist")
    assert out.startswith("ERROR:")


# -- the context tool's curation surface (WP15) ------------------------------


class TestScratchpad:
    async def test_note_replaces_and_reports(self, services: tools.Services) -> None:
        out = await tools.context_tool(
            services, action="note", text="next: the gate standoff"
        )
        assert "scratchpad replaced" in out
        assert services.session.scratchpad == "next: the gate standoff"
        await tools.context_tool(services, action="note", text="new plan")
        assert services.session.scratchpad == "new plan"  # replace, not append

    async def test_empty_text_clears(self, services: tools.Services) -> None:
        services.session.scratchpad = "old"
        out = await tools.context_tool(services, action="note", text="")
        assert out == "scratchpad cleared."
        assert services.session.scratchpad == ""

    async def test_over_cap_error_teaches_condensing(
        self, services: tools.Services
    ) -> None:
        out = await tools.context_tool(services, action="note", text="x" * 2001)
        assert "ERROR:" in out
        assert "2000" in out and "graph" in out


class TestWorkingSetActions:
    async def test_hold_accepts_a_name_and_reports_the_level(
        self, services: tools.Services, world: World
    ) -> None:
        out = await tools.context_tool(
            services, action="hold", node_id="Mira", detail="full"
        )
        assert "holding Mira [full]" in out
        assert world.mira.id in services.session.working_set

    async def test_hold_overflow_reports_the_demotion(
        self, services: tools.Services, world: World
    ) -> None:
        for node in (world.mira, world.siege, world.undercroft):
            out = await tools.context_tool(
                services, action="hold", node_id=node.id, detail="full"
            )
        assert "demoted to summaries" in out and "Mira" in out

    async def test_bad_hold_detail_lists_allowed_levels(
        self, services: tools.Services, world: World
    ) -> None:
        out = await tools.context_tool(
            services, action="hold", node_id=world.mira.id, detail="everything"
        )
        assert "ERROR:" in out
        assert "summaries" in out and "full" in out

    async def test_release_and_clear_keep_the_scratchpad(
        self, services: tools.Services, world: World
    ) -> None:
        services.session.scratchpad = "kept"
        await tools.context_tool(services, action="hold", node_id=world.mira.id)
        out = await tools.context_tool(
            services, action="release", node_id=world.mira.id
        )
        assert "released Mira" in out
        await tools.context_tool(services, action="hold", node_id=world.siege.id)
        out = await tools.context_tool(services, action="clear")
        assert "working set cleared" in out
        assert services.session.working_set.entries == ()
        assert services.session.scratchpad == "kept"

    async def test_get_echoes_scratchpad_and_working_set(
        self, services: tools.Services, world: World
    ) -> None:
        await tools.context_tool(services, action="note", text="open thread: gate")
        await tools.context_tool(
            services, action="hold", node_id=world.mira.id, detail="full"
        )
        out = await tools.context_tool(services, action="get")
        assert "scratchpad: open thread: gate" in out
        assert "Mira" in out and "[full]" in out


# -- errors are prompts -- parse-level validation ----------------------------
# (The type/relation vocabulary is OPEN; "does this type/relation exist in
# the space?" is enforced by the Anytype repository and tested in
# tests/anytype/test_repository.py. The tool layer validates shape only.)


async def test_empty_node_type_errors(services: tools.Services) -> None:
    out = await tools.create_node_tool(services, type="   ", name="x", summary="y")
    assert "ERROR:" in out
    assert "type" in out


async def test_empty_edge_label_errors(
    services: tools.Services, world: World
) -> None:
    out = await tools.create_node_tool(
        services, type="Item", name="Relic", summary="s",
        links=[{"edge_type": "", "other": world.mira.id}],
    )
    assert "ERROR:" in out
    assert "edge_type" in out


async def test_bad_detail_lists_allowed_levels(
    services: tools.Services, world: World
) -> None:
    out = await tools.explore_tool(services, start=world.mira.id, detail="terse")
    assert "ERROR:" in out
    assert "names" in out and "summaries" in out and "full" in out


async def test_malformed_link_names_required_keys(
    services: tools.Services, world: World
) -> None:
    out = await tools.create_node_tool(
        services, type="Item", name="Relic", summary="s",
        links=[{"edge_type": "possesses"}],  # missing 'other'
    )
    assert "ERROR:" in out
    assert "edge_type" in out and "other" in out


# -- invariant 2 / WP2 policy: Prose & SessionContext hidden by default ------


async def _record_prose_about(services: tools.Services, *node_ids: str) -> str:
    # Through the SERVICE: the record_prose tool was removed 2026-07-04
    # (capture is the orchestrator's job); the traversal-hiding policy
    # this section pins is about the nodes, not how they were made.
    recorder = CaptureRecorder(services.repository, now=lambda: "t")
    node = await recorder.record(
        text="A scene about the vaults.", summary="Scene.",
        references=list(node_ids),
    )
    return node.id


async def test_explore_excludes_prose_by_default(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(services, start=world.undercroft.id, depth=1)
    assert "(Capture" not in out


async def test_explore_includes_prose_when_requested(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(
        services, start=world.undercroft.id, depth=1,
        include_types=["Capture", "Location"],
    )
    assert "(Capture" in out


# -- WP3 stale-summary workflow: only_stale narrows (start exempt) -----------


async def test_only_stale_narrows_to_flagged_nodes(
    services: tools.Services, world: World
) -> None:
    # An update without a fresh summary flags the Event as stale.
    await tools.update_node_tool(services, node_id=world.fall.id, description="razed")
    out = await tools.explore_tool(
        services, start=world.mira.id, depth=2, only_stale=True, detail="names"
    )
    assert "Fall of Brakk" in out            # stale -> kept
    assert "The Undercroft" not in out       # not stale -> dropped
    assert "Mira" in out                     # start node -> always kept


# -- WP11 (ADRs 014/016): semantic tier + resolver suggestions ---------------


async def _semantic_services(world: World) -> tools.Services:
    """Services over the standard world with the hash embedder wired."""
    from graph_context.application.ranker import Ranker
    from graph_context.application.semantic_projector import SemanticProjector
    from graph_context.domain.session import SessionState
    from graph_context.infrastructure.memory.fake_repository import (
        InMemoryGraphRepository,
    )
    from graph_context.infrastructure.semantic.hashing_embedder import (
        HashingEmbedder,
    )
    from graph_context.infrastructure.semantic.memory_index import (
        InMemorySemanticIndex,
    )

    # Rebuild a fresh world so this helper controls the whole stack.
    repository = InMemoryGraphRepository()
    from graph_context.application.node_writer import NodeWriter
    from graph_context.domain.models import LinkSpec, NodeDraft

    writer = NodeWriter(repository, SessionState())
    mira = await writer.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer of Brakk.",
    ))
    await writer.create_node(
        NodeDraft("Item", name="Ashbrand", summary="A blade quenched in ash."),
        links=[LinkSpec("wielded_by", other=mira.id)],
    )
    embedder = HashingEmbedder()
    index = InMemorySemanticIndex()
    projector = SemanticProjector(repository, embedder, index)
    await projector.refresh()
    return build_services(
        repository, SessionState(project="Ashfall"),
        projector=projector, ranker=Ranker(repository, embedder, index),
    )


async def test_find_node_semantic_tier_labels_hits_with_evidence(
    world: World,
) -> None:
    svc = await _semantic_services(world)
    body = await tools.find_node_tool(
        svc, name="the exiled engineer of the siege"
    )
    assert "semantic match(es)" in body   # labelled: the LLM holds fuzzy hits
    assert "Mira" in body
    assert "why:" in body                 # evidence rides along


async def test_find_node_exact_name_never_goes_semantic(world: World) -> None:
    svc = await _semantic_services(world)
    body = await tools.find_node_tool(svc, name="Mira")
    assert "semantic" not in body         # tier 1 answered; tier 3 never ran


async def test_resolver_miss_suggests_closest_by_meaning(world: World) -> None:
    svc = await _semantic_services(world)
    out = await tools.get_node_tool(svc, node_id="the exiled siege engineer")
    assert "ERROR:" in out
    assert "Closest by meaning:" in out
    mira = svc.repository.graph.resolve("Mira")
    assert mira.id in out                 # id ready to copy into the retry


async def test_mutations_are_never_fuzzily_resolved(world: World) -> None:
    """ADR 014 non-feature: a description on update_node SUGGESTS, only."""
    svc = await _semantic_services(world)
    mira = svc.repository.graph.resolve("Mira")
    out = await tools.update_node_tool(
        svc, node_id="the exiled siege engineer", summary="clobbered!",
    )
    assert "ERROR:" in out and "Closest by meaning:" in out
    assert svc.repository.graph.node(mira.id).summary != "clobbered!"


async def test_without_ranker_behavior_is_unchanged(
    services: tools.Services, world: World
) -> None:
    body = await tools.find_node_tool(services, name="nobody here")
    assert "no match" in body             # the tier degrades away (off)
    out = await tools.get_node_tool(services, node_id="nobody here")
    assert "ERROR:" in out and "Closest by meaning" not in out


# -- find_node: a name miss pulls out-of-band edits before answering --------


class CountingRepository(InMemoryGraphRepository):
    """The fake plus a resync call counter, to pin when the retry spends."""

    def __init__(self) -> None:
        super().__init__()
        self.resync_calls = 0

    async def resync(self) -> frozenset[str]:
        self.resync_calls += 1
        return await super().resync()


class TestFindNodeResyncOnMiss:
    """The duplicate-Garden failure (2026-07-12): a user creates a node in
    the Anytype UI, immediately asks the bot to use it by name, and the
    stale index answers "no match" -- one create later there are two."""

    async def test_a_miss_resyncs_and_finds_the_just_created_node(self) -> None:
        repo = CountingRepository()
        repo.stage_out_of_band(
            NodeDraft("Project", name="Garden", summary="Yard work.")
        )
        services = build_services(repo, SessionState(project="Todo"))
        out = await tools.find_node_tool(services, name="Garden")
        assert "no match" not in out
        assert "Garden" in out

    async def test_a_hit_never_spends_a_resync(self) -> None:
        repo = CountingRepository()
        await repo.create_node(
            NodeDraft("Project", name="Garden", summary="Yard work.")
        )
        services = build_services(repo, SessionState(project="Todo"))
        out = await tools.find_node_tool(services, name="Garden")
        assert "Garden" in out
        assert repo.resync_calls == 0

    async def test_a_true_miss_answers_no_match_after_one_resync(self) -> None:
        repo = CountingRepository()
        services = build_services(repo, SessionState(project="Todo"))
        out = await tools.find_node_tool(services, name="Garden")
        assert "no match" in out
        assert repo.resync_calls == 1


# -- query: the Set-style attribute scan (filter, order, cap) ---------------


class TestQueryTool:
    async def _seed_todos(self, services: tools.Services) -> None:
        for name, fields, description in (
            ("Pay taxes", {"done": "true", "due_date": "2026-07-01"}, ""),
            ("Buy milk", {"due_date": "2026-07-10", "priority": "2"}, "Oat milk."),
            ("Write report", {"due_date": "2026-07-09", "priority": "1"}, ""),
        ):
            out = await tools.create_node_tool(
                services, type="Todo", name=name, summary=f"{name}.",
                description=description, fields=fields,
            )
            assert out.startswith("created:")

    async def test_filter_order_and_annotation_end_to_end(
        self, services: tools.Services
    ) -> None:
        await self._seed_todos(services)
        out = await tools.query_tool(
            services,
            type="Todo",
            where=[{"field": "done", "op": "neq", "value": "true"}],
            order_by=["due_date", "priority desc"],
        )
        assert out.startswith("query: 2 of 2 match(es).")
        lines = out.splitlines()
        assert "Write report" in lines[1] and "[due_date=2026-07-09" in lines[1]
        assert "Buy milk" in lines[2] and "[due_date=2026-07-10" in lines[2]
        assert "Pay taxes" not in out

    async def test_boolean_json_value_is_lowercased_to_match_checkbox_fields(
        self, services: tools.Services
    ) -> None:
        await self._seed_todos(services)
        out = await tools.query_tool(
            services, type="Todo",
            where=[{"field": "done", "op": "eq", "value": True}],
        )
        assert "Pay taxes" in out and "Buy milk" not in out

    async def test_unknown_type_error_lists_known_types(
        self, services: tools.Services
    ) -> None:
        out = await tools.query_tool(services, type="Todoo")
        assert out.startswith("ERROR:") and "'Todoo'" in out
        assert "Known types:" in out

    async def test_unknown_op_error_lists_the_ops(
        self, services: tools.Services
    ) -> None:
        out = await tools.query_tool(
            services, where=[{"field": "done", "op": "equals", "value": "x"}]
        )
        assert out.startswith("ERROR:") and "'equals'" in out
        assert "eq, neq, lt, lte, gt, gte, contains, exists, missing" in out

    async def test_bad_order_by_entry_errors_with_the_grammar(
        self, services: tools.Services
    ) -> None:
        out = await tools.query_tool(services, order_by=["due_date descending"])
        assert out.startswith("ERROR:") and "'field desc'" in out

    async def test_unknown_field_error_lists_real_fields(
        self, services: tools.Services
    ) -> None:
        await self._seed_todos(services)
        out = await tools.query_tool(
            services, type="Todo",
            where=[{"field": "deu_date", "op": "exists"}],
        )
        assert out.startswith("ERROR:") and "due_date" in out

    async def test_infra_roles_hidden_unless_type_names_them(
        self, services: tools.Services
    ) -> None:
        from graph_context.domain.models import NodeDraft

        await self._seed_todos(services)
        await services.repository.create_node(
            NodeDraft("gc_prose", name="Scene 1", summary="Captured text.")
        )
        everything = await tools.query_tool(services, order_by=["modified_at"])
        assert "Scene 1" not in everything
        explicit = await tools.query_tool(services, type="Capture")
        assert "Scene 1" in explicit

    async def test_character_timeline_via_linked_to_name(
        self, services: tools.Services
    ) -> None:
        out = await tools.query_tool(
            services, type="Event", linked_to="Mira", order_by=["story_time"]
        )
        assert out.startswith("query: 2 of 2 match(es).")
        assert out.index("Siege of Brakk") < out.index("Fall of Brakk")

    async def test_detail_full_attaches_bodies(
        self, services: tools.Services
    ) -> None:
        await self._seed_todos(services)
        out = await tools.query_tool(
            services, type="Todo",
            where=[{"field": "name", "op": "eq", "value": "Buy milk"}],
            detail="full",
        )
        assert "Oat milk." in out


class TestQueryViewParam:
    """WP13/ADR 018: saved Set views run through the same engine."""

    def _services_with_view(self) -> tools.Services:
        from graph_context.domain.query import NodeQuery, Op, Predicate, SortKey
        from graph_context.domain.session import SessionState
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )
        from graph_context.infrastructure.memory.fake_view_catalog import (
            InMemoryViewCatalog,
        )
        from graph_context.ports.view_catalog import SavedView

        saved = SavedView(
            set_name="Open Tasks", view_name="All",
            query=NodeQuery(
                node_type="Todo",
                predicates=(Predicate("done", Op.NEQ, "true"),),
                order_by=(SortKey("due_date"),),
            ),
        )
        return build_services(
            InMemoryGraphRepository(), SessionState(project="x"),
            views=InMemoryViewCatalog([saved]),
        )

    async def _seed(self, services: tools.Services) -> None:
        for name, fields in (
            ("Pay taxes", {"done": "true", "due_date": "2026-07-01"}),
            ("Buy milk", {"due_date": "2026-07-10"}),
            ("Write report", {"due_date": "2026-07-09"}),
        ):
            await tools.create_node_tool(
                services, type="Todo", name=name, summary=f"{name}.",
                fields=fields,
            )

    async def test_a_saved_view_runs_with_its_own_filters_and_order(self) -> None:
        services = self._services_with_view()
        await self._seed(services)
        out = await tools.query_tool(services, view="Open Tasks")
        assert out.startswith("view 'Open Tasks/All':")
        assert "Pay taxes" not in out  # the view's done-filter applied
        assert out.index("Write report") < out.index("Buy milk")  # its sort too
        assert "[due_date=" in out  # sort keys echoed like ad-hoc queries

    async def test_view_is_mutually_exclusive_with_adhoc_parameters(self) -> None:
        services = self._services_with_view()
        out = await tools.query_tool(services, view="Open Tasks", type="Todo")
        assert out.startswith("ERROR:") and "cannot be combined" in out

    async def test_an_unknown_view_lists_the_runnable_ones(self) -> None:
        services = self._services_with_view()
        out = await tools.query_tool(services, view="Closed Tasks")
        assert out.startswith("ERROR:")
        assert "Open Tasks/All" in out  # what IS runnable, for the retry

    async def test_no_catalog_degrades_to_an_actionable_error(
        self, services: tools.Services
    ) -> None:
        out = await tools.query_tool(services, view="Open Tasks")
        assert out.startswith("ERROR:") and "(none)" in out


# -- native-only fields end-to-end (ADR 023) ---------------------------------


class TestFieldCatalogSurface:
    """The tool layer over a catalog-strict fake: the overview teaches the
    vocabulary, the unmatched-key error self-corrects, and the opt-in
    creates a real property."""

    def _services(self) -> tools.Services:
        from graph_context.domain.models import FieldSpec
        from graph_context.domain.session import SessionState
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )

        repository = InMemoryGraphRepository(field_catalog=[
            FieldSpec(name="Due date", format="date", key="due_date"),
        ])
        return build_services(repository, SessionState(project="t"))

    async def test_overview_lists_each_types_properties(self) -> None:
        services = self._services()
        out = await tools.context_tool(services, action="overview")
        assert "properties by type" in out
        assert "Due date (date)" in out

    async def test_unmatched_field_key_errors_with_guidance(self) -> None:
        services = self._services()
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"due": "2026-08-01"},
        )
        assert out.startswith("ERROR:")
        assert "Due date (date)" in out and "create_missing_fields" in out

    async def test_matching_by_display_name_writes_the_property(self) -> None:
        services = self._services()
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Due date": "2026-08-01"},
        )
        assert out.startswith("created:")
        assert "due_date: 2026-08-01" in out

    async def test_create_missing_fields_creates_and_writes(self) -> None:
        services = self._services()
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"effort": "3"}, create_missing_fields={"effort": "Number"},
        )
        assert out.startswith("created:")
        assert "effort: 3" in out

    async def test_bad_declared_format_errors_with_the_menu(self) -> None:
        services = self._services()
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"due": "soon"}, create_missing_fields={"due": "datetime"},
        )
        assert out.startswith("ERROR:") and "formats:" in out


class TestRelationFieldCoercion:
    """Live-caught (turn 1bb6286b0e21): Anytype shows an objects-format
    relation ("Assignee") as a property of the type, so a model wrote it
    as a fields key -- and the write failed twice. The tool boundary now
    accepts that spelling and routes it as the edge it is (ADR 006)."""

    def _services(self) -> tools.Services:
        from graph_context.domain.models import FieldSpec

        repository = InMemoryGraphRepository(field_catalog=[
            FieldSpec(name="Due date", format="date", key="due_date"),
            FieldSpec(name="Assignee", format="objects", key="assignee"),
        ])
        return build_services(repository, SessionState(project="t"))

    async def _seed_member(self, services: tools.Services):
        return await services.writer.create_node(
            NodeDraft("Person", name="Luckcow", summary="moo")
        )

    def _edges_to(self, services: tools.Services, name: str, target_id: str):
        from graph_context.domain.graph import Direction

        graph = services.repository.graph
        node = graph.resolve(name)
        return node, [
            e for e in graph.edges(node.id, Direction.OUT)
            if e.target == target_id
        ]

    async def test_relation_named_field_becomes_a_link(self) -> None:
        services = self._services()
        member = await self._seed_member(services)
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Assignee": "Luckcow"},
        )
        assert out.startswith("created:")
        node, edges = self._edges_to(services, "Ship it", member.id)
        assert len(edges) == 1
        assert not node.fields  # an edge landed, never a scalar shadow

    async def test_field_and_link_naming_the_same_edge_land_once(self) -> None:
        """The exact failing call: fields={'Assignee': ...} AND the same
        edge in links. One edge results; nothing errors."""
        services = self._services()
        member = await self._seed_member(services)
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Assignee": "Luckcow"},
            links=[{"edge_type": "assignee", "other": member.id}],
        )
        assert out.startswith("created:")
        _, edges = self._edges_to(services, "Ship it", member.id)
        assert len(edges) == 1

    async def test_update_relation_field_adds_the_link(self) -> None:
        services = self._services()
        member = await self._seed_member(services)
        await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
        )
        out = await tools.update_node_tool(
            services, node_id="Ship it", fields={"Assignee": "Luckcow"},
        )
        assert out.startswith("updated:")
        _, edges = self._edges_to(services, "Ship it", member.id)
        assert len(edges) == 1

    async def test_unresolvable_relation_value_errors_actionably(self) -> None:
        services = self._services()
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Assignee": "Nobody"},
        )
        assert out.startswith("ERROR:") and "Nobody" in out

    async def test_declaration_for_a_relation_key_is_dropped(self) -> None:
        """A create_missing_fields declaration must not mint a scalar
        shadow of the relation -- the entry still becomes the edge."""
        services = self._services()
        member = await self._seed_member(services)
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Assignee": "Luckcow"},
            create_missing_fields={"Assignee": "text"},
        )
        assert out.startswith("created:")
        node, edges = self._edges_to(services, "Ship it", member.id)
        assert len(edges) == 1
        assert not node.fields
        catalog = services.repository.field_catalog()
        assert "Assignee" not in {
            spec.name for specs in catalog.values() for spec in specs
        }

    async def test_scalar_fields_pass_through_beside_a_relation_key(self) -> None:
        services = self._services()
        member = await self._seed_member(services)
        out = await tools.create_node_tool(
            services, type="Item", name="Ship it", summary="s.",
            fields={"Assignee": "Luckcow", "Due date": "2026-08-01"},
        )
        assert out.startswith("created:")
        node, edges = self._edges_to(services, "Ship it", member.id)
        assert len(edges) == 1
        assert node.fields["due_date"] == "2026-08-01"


# -- the schedule tool (WP18, ADR 027) ---------------------------------------


class TestScheduleTool:
    """The tool surface over the Scheduler: echoes that let an LLM verify
    its date math, errors that teach the schedule syntax, and the infra
    hiding that keeps events out of story traversal."""

    def _services(self, session_key: str = "anytype:chat-1") -> tools.Services:
        return build_services(
            InMemoryGraphRepository(), SessionState(project="t"),
            session_key=session_key,
        )

    async def test_set_echoes_next_fire_and_server_time(self) -> None:
        services = self._services()
        out = await tools.schedule_tool(
            services, action="set", name="tax reminder",
            schedule="2199-04-08T09:00", prompt="Remind Nick.",
        )
        assert out.startswith("scheduled 'tax reminder'")
        assert "next fire: 2199-04-08 09:00" in out
        assert "server local time:" in out

    async def test_set_stamps_the_sessions_key_on_the_node(self) -> None:
        services = self._services(session_key="anytype:chat-1")
        await tools.schedule_tool(
            services, action="set", name="ping",
            schedule="2199-01-01T09:00", prompt="p",
        )
        out = await tools.schedule_tool(services, action="list")
        assert "chat=anytype:chat-1" in out

    async def test_past_time_error_carries_the_current_time(self) -> None:
        services = self._services()
        out = await tools.schedule_tool(
            services, action="set", name="late",
            schedule="1999-01-01T09:00", prompt="p",
        )
        assert out.startswith("ERROR:") and "past" in out

    async def test_bad_schedule_error_teaches_both_formats(self) -> None:
        services = self._services()
        out = await tools.schedule_tool(
            services, action="set", name="x", schedule="whenever", prompt="p",
        )
        assert out.startswith("ERROR:")
        assert "ISO" in out and "cron" in out

    async def test_empty_list_guides_and_shows_the_clock(self) -> None:
        services = self._services()
        out = await tools.schedule_tool(services, action="list")
        assert "no scheduled events" in out
        assert "server local time:" in out

    async def test_list_shows_schedule_prompt_and_target(self) -> None:
        services = self._services()
        await tools.schedule_tool(
            services, action="set", name="standup", schedule="0 9 * * 1",
            prompt="Post the weekly summary.",
        )
        out = await tools.schedule_tool(services, action="list")
        assert "scheduled events (1):" in out
        assert "standup" in out and "repeating '0 9 * * 1'" in out
        assert "prompt: Post the weekly summary." in out

    async def test_cancel_by_name_reports_and_disables(self) -> None:
        services = self._services()
        await tools.schedule_tool(
            services, action="set", name="tax reminder",
            schedule="2199-04-08T09:00", prompt="p",
        )
        out = await tools.schedule_tool(
            services, action="cancel", node_id="tax reminder",
        )
        assert out.startswith("cancelled 'tax reminder'")
        assert "re-enable" in out  # the human's Pending flip is taught
        assert "cancelled" in await tools.schedule_tool(services, action="list")


# -- the automation tool (WP32, ADR 040) -------------------------------------


class _EchoScriptRunner:
    """A ScriptRunner fake for the tool surface: parrots one canned
    effect derived from the payload's trigger."""

    async def run(self, script, payload):  # type: ignore[no-untyped-def]
        from graph_context.ports.script_runner import ScriptEffect, ScriptOutcome
        return ScriptOutcome(
            sets=(ScriptEffect(
                node_id=payload["trigger"], property="Note", value="scripted",
            ),),
            logs=("ran",),
        )


class TestAutomationTool:
    """The tool surface over the RuleEngine: creation-time validation,
    lifecycle, and the dry run that applies nothing."""

    def _services(self) -> tools.Services:
        return build_services(
            InMemoryGraphRepository(), SessionState(project="t"),
            script_runner=_EchoScriptRunner(),
        )

    async def _create(self, services: tools.Services) -> str:
        return await tools.automation_tool(
            services, action="create", name="stamp completion",
            target_type="Task", watch_property="Done",
            condition="changed to true", rule_action="set property to now",
            action_property="Completion date",
        )

    async def test_create_validates_and_reports_the_rule(self) -> None:
        services = self._services()
        out = await self._create(services)
        assert out.startswith("created automation rule 'stamp completion'")
        assert "action='test'" in out  # teaches the next step
        stored = next(
            n for n in services.repository.graph.nodes()
            if n.name == "stamp completion"
        )
        assert stored.fields[rules_domain.FIELD_CONDITION] == "Changed to true"
        assert stored.fields[rules_domain.FIELD_STATUS] == "Active"

    async def test_create_with_a_bad_action_word_teaches_the_vocabulary(
        self,
    ) -> None:
        services = self._services()
        out = await tools.automation_tool(
            services, action="create", name="x", target_type="Task",
            watch_property="Done", condition="changed",
            rule_action="explode",
        )
        assert out.startswith("ERROR:")
        assert "run script" in out  # the allowed words are echoed
        assert not [
            n for n in services.repository.graph.nodes() if n.name == "x"
        ]  # nothing written

    async def test_create_script_rule_stores_the_fenced_body(self) -> None:
        services = self._services()
        out = await tools.automation_tool(
            services, action="create", name="rollup", target_type="Task",
            watch_property="Done", condition="changed",
            rule_action="run script", script="log('hi')",
        )
        assert out.startswith("created automation rule 'rollup'")
        stored = next(
            n for n in services.repository.graph.nodes() if n.name == "rollup"
        )
        body = await services.repository.fetch_body(stored.id)
        assert body == "```python\nlog('hi')\n```"

    async def test_create_script_rule_without_script_errors(self) -> None:
        services = self._services()
        out = await tools.automation_tool(
            services, action="create", name="rollup", target_type="Task",
            watch_property="Done", condition="changed",
            rule_action="run script",
        )
        assert out.startswith("ERROR:") and "'script'" in out

    async def test_list_shows_status_and_config(self) -> None:
        services = self._services()
        await self._create(services)
        out = await tools.automation_tool(services, action="list")
        assert "automation rules (1):" in out
        assert "stamp completion" in out and "active" in out
        assert "when 'Done' on 'Task'" in out

    async def test_empty_list_guides_creation(self) -> None:
        out = await tools.automation_tool(self._services(), action="list")
        assert "no automation rules" in out and "action='create'" in out

    async def test_pause_and_resume_flip_the_status(self) -> None:
        services = self._services()
        await self._create(services)
        out = await tools.automation_tool(
            services, action="pause", rule="stamp completion",
        )
        assert out.startswith("paused")
        assert "paused" in await tools.automation_tool(services, action="list")
        out = await tools.automation_tool(
            services, action="resume", rule="stamp completion",
        )
        assert out.startswith("resumed")
        assert "active" in await tools.automation_tool(services, action="list")

    async def test_update_replaces_config_and_revalidates(self) -> None:
        services = self._services()
        await self._create(services)
        out = await tools.automation_tool(
            services, action="update", rule="stamp completion",
            condition="changed",
        )
        assert out.startswith("updated")
        assert "changed ->" in await tools.automation_tool(
            services, action="list",
        )

    async def test_test_dry_runs_a_builtin_without_writing(self) -> None:
        services = self._services()
        await self._create(services)
        task = await services.repository.create_node(
            NodeDraft(type="Task", name="ship it", summary="s")
        )
        out = await tools.automation_tool(
            services, action="test", rule="stamp completion",
        )
        assert "dry run against 'ship it'" in out
        assert "would set 'ship it'.Completion date" in out
        assert "nothing was applied" in out
        assert "Completion date" not in services.repository.graph.node(
            task.id
        ).fields

    async def test_test_dry_runs_a_script_draft_before_creation(self) -> None:
        services = self._services()
        await services.repository.create_node(
            NodeDraft(type="Task", name="ship it", summary="s")
        )
        out = await tools.automation_tool(
            services, action="test", target_type="Task",
            watch_property="Done", condition="changed to true",
            rule_action="run script", script="set(trigger, 'Note', 'x')",
        )
        assert "script log: ran" in out
        assert "would set 'ship it'.Note = 'scripted'" in out
        assert "nothing was applied" in out
        # And truly nothing was: the task carries no Note.
        stored = next(
            n for n in services.repository.graph.nodes()
            if n.name == "ship it"
        )
        assert "Note" not in stored.fields

    async def test_test_without_targets_teaches_the_fix(self) -> None:
        services = self._services()
        await self._create(services)
        out = await tools.automation_tool(
            services, action="test", rule="stamp completion",
        )
        assert out.startswith("ERROR:") and "no objects of type" in out

    async def test_unknown_action_lists_the_verbs(self) -> None:
        out = await tools.automation_tool(self._services(), action="destroy")
        assert out.startswith("ERROR:") and "test" in out and "create" in out

    async def test_unknown_action_lists_the_allowed_ones(self) -> None:
        services = self._services()
        out = await tools.schedule_tool(services, action="fire")
        assert out.startswith("ERROR:")
        assert "set, list, cancel" in out

    async def test_events_hide_from_query_unless_named(self) -> None:
        services = self._services()
        await tools.schedule_tool(
            services, action="set", name="tax reminder",
            schedule="2199-04-08T09:00", prompt="p",
        )
        everything = await tools.query_tool(services)
        assert "tax reminder" not in everything
        named = await tools.query_tool(services, type="ScheduledEvent")
        assert "tax reminder" in named

    async def test_events_hide_from_find_node(self) -> None:
        services = self._services()
        await tools.schedule_tool(
            services, action="set", name="tax reminder",
            schedule="2199-04-08T09:00", prompt="p",
        )
        out = await tools.find_node_tool(services, name="tax reminder")
        assert "tax reminder" not in out

    async def test_build_services_pins_the_schedulers_timezone(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        services = build_services(
            InMemoryGraphRepository(), SessionState(project="t"),
            timezone="Pacific/Kiritimati",  # UTC+14: unmistakably not UTC
        )
        expected = datetime.now(
            ZoneInfo("Pacific/Kiritimati")
        ).replace(tzinfo=None)
        assert abs(
            (services.scheduler.now() - expected).total_seconds()
        ) < 5


class TestSendFileTool:
    """WP23 (ADR 032): send_file queues into the turn-scoped outbox; the
    transport uploads after the reply -- the tool itself does no I/O."""

    async def test_a_valid_file_queues_and_confirms(
        self, services: tools.Services
    ) -> None:
        out = await tools.send_file_tool(
            services, name="report.md", content="# Report"
        )
        assert "report.md" in out and "queued" in out
        (queued,) = services.outbox
        assert queued.name == "report.md"
        assert queued.content == "# Report"

    async def test_path_segments_are_stripped_from_the_name(
        self, services: tools.Services
    ) -> None:
        await tools.send_file_tool(
            services, name="../../etc/notes.txt", content="x"
        )
        assert services.outbox[-1].name == "notes.txt"

    async def test_validation_errors_echo_the_fix(
        self, services: tools.Services
    ) -> None:
        out = await tools.send_file_tool(services, name="", content="x")
        assert out.startswith("ERROR:") and "filename" in out
        out = await tools.send_file_tool(services, name="noext", content="x")
        assert out.startswith("ERROR:") and "extension" in out
        out = await tools.send_file_tool(services, name="a.md", content="")
        assert out.startswith("ERROR:") and "empty" in out
        assert services.outbox == []

    async def test_oversize_and_per_turn_caps(
        self, services: tools.Services
    ) -> None:
        out = await tools.send_file_tool(
            services, name="big.md",
            content="x" * (tools.MAX_OUTBOUND_FILE_CHARS + 1),
        )
        assert out.startswith("ERROR:") and "cap" in out
        for i in range(tools.MAX_OUTBOUND_FILES_PER_TURN):
            await tools.send_file_tool(
                services, name=f"f{i}.md", content="x"
            )
        out = await tools.send_file_tool(services, name="one-more.md", content="x")
        assert out.startswith("ERROR:") and "next turn" in out


class TestSchemaTool:
    """WP33 (ADR 041 v2): the model DRAFTS schema changes; the confirm
    message + 👍 reaction belong to the harness. The tool surface has no
    apply -- that absence is the guarantee."""

    def _services(self) -> tools.Services:
        return build_services(InMemoryGraphRepository(), SessionState(project="t"))

    async def _propose_faction(self, services: tools.Services) -> str:
        return await tools.schema_tool(
            services, action="propose_type", type="Faction",
            properties=[
                {"name": "Motto", "format": "text"},
                {"name": "Alignment", "format": "select",
                 "options": ["Good", "Evil"]},
            ],
            reason="track allegiances",
        )

    async def test_propose_renders_the_draft_and_the_contract(self) -> None:
        services = self._services()
        out = await self._propose_faction(services)
        assert "NEW TYPE 'Faction'" in out
        assert "Motto (text)" in out
        assert "Alignment (select: Good, Evil)" in out
        assert "reacts \N{THUMBS UP SIGN}" in out  # the contract is taught
        assert "cannot apply it yourself" in out
        # Nothing touched the space, and the draft awaits the harness.
        assert "Faction" not in services.repository.known_node_types()
        assert [p.id for p in services.proposals.drafted] == ["p1"]

    async def test_apply_is_not_a_model_action(self) -> None:
        services = self._services()
        await self._propose_faction(services)
        out = await tools.schema_tool(services, action="apply", proposal_id="p1")
        assert out.startswith("ERROR:")
        assert "not a model action" in out
        assert "Faction" not in services.repository.known_node_types()
        assert services.proposals.pending()  # the draft survives

    async def test_propose_fields_drafts_against_an_existing_type(self) -> None:
        services = self._services()
        out = await tools.schema_tool(
            services, action="propose_fields", type="Character",
            properties=[{"name": "Influence", "format": "number"}],
        )
        assert "NEW PROPERTIES on existing type 'Character'" in out
        assert not out.startswith("ERROR:")

    async def test_malformed_property_entry_is_an_actionable_error(self) -> None:
        services = self._services()
        out = await tools.schema_tool(
            services, action="propose_type", type="Faction",
            properties=[{"name": "HQ", "format": "banana"}],
        )
        assert out.startswith("ERROR:")
        assert "formats:" in out  # the menu is echoed

    async def test_list_and_cancel_manage_the_ledger(self) -> None:
        services = self._services()
        assert "no pending schema proposals" in await tools.schema_tool(
            services, action="list"
        )
        await self._propose_faction(services)
        listed = await tools.schema_tool(services, action="list")
        assert "[p1]" in listed and "Faction" in listed
        assert "you cannot apply them" in listed
        out = await tools.schema_tool(services, action="cancel", proposal_id="p1")
        assert out.startswith("cancelled proposal p1")
        assert "no pending schema proposals" in await tools.schema_tool(
            services, action="list"
        )

    async def test_unknown_action_lists_the_menu(self) -> None:
        out = await tools.schema_tool(self._services(), action="destroy")
        assert out.startswith("ERROR:")
        assert "propose_type" in out and "cancel" in out
