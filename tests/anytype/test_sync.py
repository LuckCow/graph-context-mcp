"""Hydrate & resync: out-of-band human edits, deletions, lenient reads."""

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository

CHAR = NodeDraft(NodeType.CHARACTER, name="Mira", summary="Engineer.")
PLACE = NodeDraft(NodeType.LOCATION, name="Undercroft", summary="Vaults.")


def _snapshot(graph):
    nodes = tuple(sorted((n.id, n.name, n.summary) for n in graph.nodes()))
    edges = tuple(sorted(
        (e.source, e.type.value, e.target)
        for n in graph.nodes() for e in graph.edges(n.id)
    ))
    return nodes, edges


class TestHydrate:
    async def test_restart_hydrate_reproduces_identical_graph(self, mock, client, repo):
        mira = await repo.create_node(CHAR)
        await repo.create_node(
            PLACE, links=[LinkSpec(EdgeType.LOCATED_AT, other=mira.id, outgoing=False)]
        )
        original = _snapshot(repo.graph)

        # "restart": brand-new client + repository over the same store
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        fresh_client = AnytypeClient(config, transport=mock.transport)
        fresh = AnytypeGraphRepository(fresh_client)
        await fresh.hydrate()
        assert _snapshot(fresh.graph) == original
        await fresh_client.aclose()

    async def test_hydrate_call_budget_is_one_sweep(self, mock, client, repo):
        for i in range(30):  # 3 pages at page_limit=10
            mock.seed_object("gc_character", f"c{i}",
                             properties=[mapping.property_entry("gc_summary", "text", "x")])
        mock.request_log.clear()
        await repo.hydrate()
        get_calls = [p for m, p in mock.request_log if m == "GET"]
        assert len(get_calls) <= 5  # paged sweep only -- no N+1 per-object GETs

    async def test_hydrate_skips_dangling_and_illegal_edges(self, mock, client, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        # Human drags an illegal edge (Location knows ...) and a dangling one in the UI
        mock.edit_object_directly(place.id, set_property=mapping.property_entry(
            "gc_edge_knows", "objects", [mira.id]))
        mock.edit_object_directly(mira.id, set_property=mapping.property_entry(
            "gc_edge_knows", "objects", ["deleted-elsewhere"]))
        await repo.hydrate()  # must not raise
        assert repo.graph.edge_count() == 0


class TestResync:
    async def test_resync_picks_up_field_edit(self, mock, repo):
        mira = await repo.create_node(CHAR)
        mock.edit_object_directly(mira.id, name="Mira of Brakk")
        changed = await repo.resync()
        assert changed == frozenset({mira.id})
        assert repo.graph.node(mira.id).name == "Mira of Brakk"

    async def test_resync_picks_up_human_created_node_and_edge(self, mock, repo):
        mira = await repo.create_node(CHAR)
        new_id = mock.seed_object("gc_character", "Orla", properties=[
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
            PLACE, links=[LinkSpec(EdgeType.LOCATED_AT, other=mira.id, outgoing=False)]
        )
        assert await repo.resync() == frozenset()

    async def test_own_link_ops_are_not_reported_by_resync(self, mock, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        edge = await repo.add_link(mira.id, LinkSpec(EdgeType.LOCATED_AT, other=place.id))
        await repo.remove_link(edge)
        assert await repo.resync() == frozenset()

    async def test_resync_removes_archived_when_visible_in_lists(self):
        mock = MockAnytype(archived_visible_in_lists=True)  # spike S4: answer "yes"
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
        await ensure_schema(client)
        repo = AnytypeGraphRepository(client)
        await repo.hydrate()
        mira = await repo.create_node(CHAR)
        mock.archive_directly(mira.id)
        changed = await repo.resync()
        assert changed == frozenset({mira.id})
        assert not repo.graph.has_node(mira.id)
        await client.aclose()

    async def test_resync_misses_deletions_when_archived_hidden_but_hydrate_reconciles(
        self, mock, repo
    ):
        """Documents the S4 blind spot: with archived objects hidden from
        lists, resync cannot see deletions; the next full hydrate does."""
        mira = await repo.create_node(CHAR)
        mock.archive_directly(mira.id)
        assert await repo.resync() == frozenset()      # blind spot, by design
        assert repo.graph.has_node(mira.id)            # stale until...
        await repo.hydrate()                           # ...full reconciliation
        assert not repo.graph.has_node(mira.id)
