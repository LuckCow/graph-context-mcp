"""Space-reflecting writes: type/relation resolution, reuse vs approval,
custom-relation and inline-link reads."""

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.errors import UnknownNodeType, UnknownRelationLabel
from graph_context.infrastructure.anytype import mapping

CHAR = NodeDraft("Character", name="Mira", summary="Engineer.")


class TestTypeResolution:
    async def test_create_targets_native_type(self, repo):
        node = await repo.create_node(CHAR)
        assert repo.graph.node(node.id).type_key == "character"
        assert repo.graph.node(node.id).type == "Character"

    async def test_unknown_type_surfaces_for_approval(self, repo):
        before = repo.graph.node_count()
        with pytest.raises(UnknownNodeType):
            await repo.create_node(NodeDraft("Realization", name="x", summary="y"))
        assert repo.graph.node_count() == before


class TestRelationReuseAndApproval:
    async def test_bootstrapped_relation_is_reused(self, repo):
        mira = await repo.create_node(CHAR)
        ally = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
        edge = await repo.add_link(mira.id, LinkSpec("knows", other=ally.id))
        assert edge.property_key == "gc_edge_knows"  # reused, not re-created

    async def test_unknown_relation_surfaces_for_approval(self, repo):
        await repo.create_node(CHAR)
        boss = await repo.create_node(NodeDraft("Character", name="Adnan", summary="Boss."))
        before = repo.graph.edge_count()
        with pytest.raises(UnknownRelationLabel):
            await repo.create_node(
                NodeDraft("Character", name="Greg", summary="Worker."),
                links=[LinkSpec("boss", other=boss.id)],
            )
        assert repo.graph.edge_count() == before

    async def test_create_missing_relations_creates_and_links(self, repo):
        await repo.create_node(CHAR)
        boss = await repo.create_node(NodeDraft("Character", name="Adnan", summary="Boss."))
        greg = await repo.create_node(
            NodeDraft("Character", name="Greg", summary="Worker."),
            links=[LinkSpec("boss", other=boss.id)],
            create_missing_relations=True,
        )
        edges = list(repo.graph.edges(greg.id))
        assert [(e.type, e.target) for e in edges] == [("boss", boss.id)]
        # the relation now exists and is reused without approval next time
        assert repo.registry.key_for_label("boss") is not None


class TestCustomAndInlineReads:
    async def test_custom_relation_on_single_object_reads_as_edge(self, repo, mock):
        """A relation living on ONE object (not the type) still reads."""
        mira = await repo.create_node(CHAR)
        adnan = await repo.create_node(NodeDraft("Character", name="Adnan", summary="Boss."))
        # Human adds a bespoke `boss` relation on Greg only, in the UI.
        greg_id = mock.seed_object("character", "Greg", properties=[
            mapping.property_entry("gc_summary", "text", "Worker."),
            mapping.property_entry("boss", "objects", [adnan.id]),
        ])
        await repo.hydrate()
        labels = {e.type for e in repo.graph.edges(greg_id)}
        assert "boss" in labels
        assert {n.id for _, n in repo.graph.neighbors(greg_id)} == {adnan.id}
        assert repo.graph.has_node(mira.id)

    async def test_inline_links_read_as_generic_edges_no_double_count(self, repo, mock):
        adnan = await repo.create_node(NodeDraft("Character", name="Adnan", summary="Founder."))
        # Inline link Famico -> Adnan, mirrored into `links`; reciprocal in
        # `backlinks` on both sides must NOT produce extra edges.
        famico_id = mock.seed_object("organization", "Famico", properties=[
            mapping.property_entry("gc_summary", "text", "A company."),
            mapping.property_entry("links", "objects", [adnan.id]),
            mapping.property_entry("backlinks", "objects", [adnan.id]),
        ])
        mock.edit_object_directly(adnan.id, set_property=mapping.property_entry(
            "backlinks", "objects", [famico_id]))
        await repo.hydrate()
        out = list(repo.graph.edges(famico_id))
        assert [(e.type, e.target) for e in out] == [("links", adnan.id)]
        # backlinks contributed no edges on either side
        assert repo.graph.edge_count() == 1
