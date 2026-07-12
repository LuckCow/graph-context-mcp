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
from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface import tools
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
    return tools.build_services(
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
        services = tools.build_services(repo, SessionState(project="Todo"))
        out = await tools.find_node_tool(services, name="Garden")
        assert "no match" not in out
        assert "Garden" in out

    async def test_a_hit_never_spends_a_resync(self) -> None:
        repo = CountingRepository()
        await repo.create_node(
            NodeDraft("Project", name="Garden", summary="Yard work.")
        )
        services = tools.build_services(repo, SessionState(project="Todo"))
        out = await tools.find_node_tool(services, name="Garden")
        assert "Garden" in out
        assert repo.resync_calls == 0

    async def test_a_true_miss_answers_no_match_after_one_resync(self) -> None:
        repo = CountingRepository()
        services = tools.build_services(repo, SessionState(project="Todo"))
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
        return tools.build_services(
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
        return tools.build_services(repository, SessionState(project="t"))

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
