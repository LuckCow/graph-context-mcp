"""Hydrate & resync: out-of-band human edits, deletions, lenient reads."""

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository

CHAR = NodeDraft("Character", name="Mira", summary="Engineer.")
PLACE = NodeDraft("Location", name="Undercroft", summary="Vaults.")


def _snapshot(graph):
    nodes = tuple(sorted((n.id, n.name, n.summary) for n in graph.nodes()))
    edges = tuple(sorted(
        (e.source, e.type, e.target)
        for n in graph.nodes() for e in graph.edges(n.id)
    ))
    return nodes, edges


class TestHydrate:
    async def test_restart_hydrate_reproduces_identical_graph(self, mock, client, repo):
        mira = await repo.create_node(CHAR)
        await repo.create_node(
            PLACE, links=[LinkSpec("located_at", other=mira.id, outgoing=False)]
        )
        original = _snapshot(repo.graph)

        # "restart": brand-new client + repository over the same store
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        fresh_client = AnytypeClient(config, transport=mock.transport)
        fresh = AnytypeGraphRepository(fresh_client)
        await fresh.hydrate()
        assert _snapshot(fresh.graph) == original
        await fresh_client.aclose()

    async def test_hydrate_call_budget_has_no_per_object_gets(self, mock, client, repo):
        for i in range(30):  # 3 pages at page_limit=10
            mock.seed_object("character", f"c{i}",
                             properties=[mapping.property_entry("gc_summary", "text", "x")])
        mock.request_log.clear()
        await repo.hydrate()
        get_calls = [p for m, p in mock.request_log if m == "GET"]
        # paged sweeps only (objects + types + properties) -- no N+1 per-object GETs
        assert len(get_calls) <= 8

    async def test_hydrate_keeps_open_edges_but_skips_dangling(self, mock, client, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        # Human drags a (now perfectly legal) edge and a dangling one in the UI.
        mock.edit_object_directly(place.id, set_property=mapping.property_entry(
            "gc_edge_knows", "objects", [mira.id]))
        mock.edit_object_directly(mira.id, set_property=mapping.property_entry(
            "gc_edge_knows", "objects", ["deleted-elsewhere"]))
        await repo.hydrate()  # must not raise
        # the open edge survives; only the dangling target is dropped
        assert repo.graph.edge_count() == 1
        assert {n.id for _, n in repo.graph.neighbors(place.id)} == {mira.id}


class TestResync:
    async def test_resync_picks_up_field_edit(self, mock, repo):
        mira = await repo.create_node(CHAR)
        mock.edit_object_directly(mira.id, name="Mira of Brakk")
        changed = await repo.resync()
        assert changed == frozenset({mira.id})
        assert repo.graph.node(mira.id).name == "Mira of Brakk"

    async def test_resync_picks_up_human_created_node_and_edge(self, mock, repo):
        mira = await repo.create_node(CHAR)
        new_id = mock.seed_object("character", "Orla", properties=[
            mapping.property_entry("gc_summary", "text", "A smuggler."),
            mapping.property_entry("gc_edge_knows", "objects", [mira.id]),
        ])
        changed = await repo.resync()
        assert new_id in changed
        assert {n.id for _, n in repo.graph.neighbors(mira.id)} == {new_id}

    async def test_resync_is_a_noop_without_changes(self, mock, repo):
        await repo.create_node(CHAR)
        assert await repo.resync() == frozenset()

    async def test_own_composite_writes_are_not_reported_by_resync(self, mock, repo):
        """Self-write suppression must also cover the PATCHes a composite
        create issues against *other* objects (incoming links)."""
        mira = await repo.create_node(CHAR)
        await repo.create_node(
            PLACE, links=[LinkSpec("located_at", other=mira.id, outgoing=False)]
        )
        assert await repo.resync() == frozenset()

    async def test_own_link_ops_are_not_reported_by_resync(self, mock, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        edge = await repo.add_link(mira.id, LinkSpec("located_at", other=place.id))
        await repo.remove_link(edge)
        assert await repo.resync() == frozenset()

    async def test_removing_semantic_relation_resurrects_links_edge(self, mock, repo):
        """The `links` mirror of a semantic relation is suppressed on read;
        when the human deletes the semantic relation in the UI, the
        `links`-only connection must reappear on the next resync (edges are
        re-derived per object, no suppress/resurrect bookkeeping)."""
        mira = await repo.create_node(CHAR)
        orla_id = mock.seed_object("character", "Orla", properties=[
            mapping.property_entry("gc_summary", "text", "A smuggler."),
            mapping.property_entry("gc_edge_knows", "objects", [mira.id]),
            mapping.property_entry("links", "objects", [mira.id]),  # the mirror
        ])
        await repo.resync()
        assert {e.type for e in repo.graph.edges(orla_id)} == {"knows"}

        # Human deletes the semantic relation in the UI; the mirror stays.
        mock.edit_object_directly(orla_id, set_property=mapping.property_entry(
            "gc_edge_knows", "objects", []))
        changed = await repo.resync()
        assert orla_id in changed
        assert {e.type for e in repo.graph.edges(orla_id)} == {"links"}

    async def test_resync_misses_deletions_but_hydrate_reconciles(self, mock, repo):
        """The confirmed S4 behavior: archived objects are invisible to both
        list and search, so resync cannot see deletions; the next full
        hydrate rebuilds from the live set and drops them."""
        mira = await repo.create_node(CHAR)
        mock.archive_directly(mira.id)
        assert await repo.resync() == frozenset()      # blind spot, by design
        assert repo.graph.has_node(mira.id)            # stale until...
        await repo.hydrate()                           # ...full reconciliation
        assert not repo.graph.has_node(mira.id)
