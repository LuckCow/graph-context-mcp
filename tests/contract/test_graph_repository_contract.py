"""The GraphRepository contract: one behavioral spec, every implementation.

Each concrete repository inherits the contract class and provides a
``repo`` fixture. A behavior that cannot be expressed by ALL
implementations means the port is wrong -- fix the port, not the adapter.

When live-server access exists, add a third subclass gated behind
``ANYTYPE_E2E=1`` pointing the same tests at a real space.
"""

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.errors import NodeNotFound, SchemaViolation
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository

CHAR = NodeDraft(NodeType.CHARACTER, name="Mira", summary="Exiled siege engineer.")
PLACE = NodeDraft(NodeType.LOCATION, name="The Undercroft", summary="Vaults beneath Brakk.")


class GraphRepositoryContract:
    """Inherit + provide a `repo` fixture to certify an implementation."""

    async def test_create_assigns_id_and_lands_in_graph(self, repo):
        node = await repo.create_node(CHAR)
        assert node.id
        assert repo.graph.node(node.id).name == "Mira"

    async def test_composite_create_writes_outgoing_and_incoming_links(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(
            PLACE, links=[LinkSpec(EdgeType.LOCATED_AT, other=mira.id, outgoing=False)]
        )
        # incoming: mira -located_at-> place
        assert {n.id for _, n in repo.graph.neighbors(mira.id)} == {place.id}
        sword = await repo.create_node(
            NodeDraft(NodeType.ITEM, name="Ashbrand", summary="A blade."),
        )
        faction = await repo.create_node(
            NodeDraft(NodeType.FACTION, name="Emberguard", summary="Defenders."),
            links=[LinkSpec(EdgeType.POSSESSES, other=sword.id, outgoing=True)],
        )
        assert {n.id for _, n in repo.graph.neighbors(faction.id)} == {sword.id}

    async def test_create_with_missing_link_target_rolls_back(self, repo):
        before = repo.graph.node_count()
        with pytest.raises(NodeNotFound):
            await repo.create_node(
                CHAR, links=[LinkSpec(EdgeType.KNOWS, other="no-such-node")]
            )
        assert repo.graph.node_count() == before

    async def test_create_with_illegal_link_rolls_back(self, repo):
        place = await repo.create_node(PLACE)
        before = repo.graph.node_count()
        with pytest.raises(SchemaViolation):
            await repo.create_node(
                PLACE,  # Location knows Location: illegal source for `knows`
                links=[LinkSpec(EdgeType.KNOWS, other=place.id, outgoing=True)],
            )
        assert repo.graph.node_count() == before

    async def test_update_applies_only_provided_fields(self, repo):
        node = await repo.create_node(CHAR)
        updated = await repo.update_node(
            node.id, description="Leads the survivors.", summary_stale=True
        )
        assert updated.description == "Leads the survivors."
        assert updated.summary == "Exiled siege engineer."  # untouched
        assert updated.summary_stale is True

    async def test_update_unknown_node_raises(self, repo):
        with pytest.raises(NodeNotFound):
            await repo.update_node("ghost", name="?")

    async def test_add_and_remove_link_round_trip(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        edge = await repo.add_link(mira.id, LinkSpec(EdgeType.LOCATED_AT, other=place.id))
        assert list(repo.graph.edges(mira.id)) == [edge]
        await repo.remove_link(edge)
        assert list(repo.graph.edges(mira.id)) == []

    async def test_add_illegal_link_is_rejected_cleanly(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        with pytest.raises(SchemaViolation):
            await repo.add_link(place.id, LinkSpec(EdgeType.KNOWS, other=mira.id))
        assert list(repo.graph.edges(place.id)) == []

    async def test_fields_round_trip(self, repo):
        node = await repo.create_node(
            NodeDraft(NodeType.TECHNOLOGY, name="Ashforge", summary="A forge.",
                      fields={"fuel": "bonemeal"})
        )
        assert repo.graph.node(node.id).fields == {"fuel": "bonemeal"}


class TestInMemoryRepository(GraphRepositoryContract):
    @pytest.fixture
    def repo(self):
        return InMemoryGraphRepository()


class TestAnytypeRepository(GraphRepositoryContract):
    @pytest.fixture
    async def repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        yield repository
        await client.aclose()
