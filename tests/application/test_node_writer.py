"""NodeWriter: composite writes, validation, and the staleness rule."""

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.errors import NodeNotFound, SchemaViolation
from tests.conftest import World


class TestCreateNode:
    async def test_composite_create_writes_node_and_links(self, writer, repository, world: World):
        faction = await writer.create_node(
            NodeDraft("Organization", name="Emberguard", summary="Brakk's last defenders."),
            links=[LinkSpec("member_of", other=world.mira.id, outgoing=False)],
        )
        neighbors = {n.name for _, n in repository.graph.neighbors(faction.id)}
        assert neighbors == {"Mira"}

    async def test_summaryless_create_is_rejected_before_any_write(self, writer, repository):
        before = repository.graph.node_count()
        with pytest.raises(SchemaViolation):
            await writer.create_node(NodeDraft("Character", name="Ghost", summary=""))
        assert repository.graph.node_count() == before

    async def test_failed_link_rolls_back_the_created_node(self, writer, repository, world: World):
        before = repository.graph.node_count()
        with pytest.raises(NodeNotFound):
            await writer.create_node(
                NodeDraft("Character", name="Orla", summary="A smuggler."),
                links=[LinkSpec("knows", other="no-such-node")],
            )
        assert repository.graph.node_count() == before

    async def test_created_node_lands_on_focus_top(self, writer, session, world: World):
        node = await writer.create_node(
            NodeDraft("Location", name="Brakk Gate", summary="The city gate.")
        )
        assert session.focus.top == node.id


class TestUpdateNode:
    async def test_update_without_summary_flags_stale(self, writer, repository, world: World):
        await writer.update_node(world.mira.id, description="Now leads the survivors.")
        assert repository.graph.node(world.mira.id).summary_stale is True

    async def test_update_with_summary_clears_stale(self, writer, repository, world: World):
        await writer.update_node(world.mira.id, description="Leads the survivors.")
        await writer.update_node(world.mira.id, summary="Engineer turned survivor-leader.")
        node = repository.graph.node(world.mira.id)
        assert node.summary_stale is False
        assert node.summary == "Engineer turned survivor-leader."

    async def test_update_can_add_and_remove_links(self, writer, repository, world: World):
        edge = await repository.add_link(
            world.mira.id, LinkSpec("located_at", other=world.undercroft.id)
        )
        await writer.update_node(world.mira.id, remove_links=[edge])
        located = list(
            repository.graph.edges(world.mira.id, edge_types=["located_at"])
        )
        assert located == []

    async def test_unknown_node_fails_fast(self, writer):
        with pytest.raises(NodeNotFound):
            await writer.update_node("ghost", description="?")
