"""Space-reflecting writes: type/relation resolution, reuse vs approval,
custom-relation and inline-link reads."""

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.errors import UnknownNodeType, UnknownRelationLabel
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.config import AnytypeApiError
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository

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


class TestFreshRelationSettleWindow:
    """Live finding (2026-07): a just-created relation 400s ("unknown
    property key") in PATCHes for a short settle window. The repository
    must retry those PATCHes -- but only for keys it created itself."""

    async def _settling_repo(self, mock, client):
        mock.property_settle_patches = 2
        sleeps: list[float] = []

        async def instant(delay: float) -> None:
            sleeps.append(delay)

        repository = AnytypeGraphRepository(client, sleep=instant)
        await repository.hydrate()
        return repository, sleeps

    async def test_create_with_fresh_relation_survives_settle_window(
        self, mock, client, repo
    ) -> None:
        repository, sleeps = await self._settling_repo(mock, client)
        target = await repository.create_node(
            NodeDraft("Character", name="Adnan", summary="Boss.")
        )
        node = await repository.create_node(
            NodeDraft("Character", name="Mary", summary="Marketer."),
            links=[LinkSpec("inspired_by", other=target.id)],
            create_missing_relations=True,
        )
        edges = [(e.type, e.target) for e in repository.graph.edges(node.id)]
        assert ("inspired_by", target.id) in edges
        assert len(sleeps) == 2  # two rejected PATCHes, two backoffs

    async def test_settle_window_longer_than_budget_still_rolls_back(
        self, mock, client, repo
    ) -> None:
        repository, _ = await self._settling_repo(mock, client)
        mock.property_settle_patches = 10  # never settles within the budget
        target = await repository.create_node(
            NodeDraft("Character", name="Adnan", summary="Boss.")
        )
        before = repository.graph.node_count()
        with pytest.raises(AnytypeApiError):
            await repository.create_node(
                NodeDraft("Character", name="Mary", summary="Marketer."),
                links=[LinkSpec("inspired_by", other=target.id)],
                create_missing_relations=True,
            )
        assert repository.graph.node_count() == before  # rolled back


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

    async def test_links_mirroring_a_semantic_relation_is_suppressed(self, repo, mock):
        """Adapter-read behavior, not port-level: the in-memory fake has no
        generic `links` concept, so this lives here rather than the contract
        suite. Anytype mirrors semantic connections into `links`; the mirror
        must not double the edge, while a `links`-only target (a bare body
        mention) still reads as an edge."""
        adnan = await repo.create_node(NodeDraft("Character", name="Adnan", summary="Boss."))
        mira = await repo.create_node(CHAR)
        greg_id = mock.seed_object("character", "Greg", properties=[
            mapping.property_entry("gc_summary", "text", "Worker."),
            mapping.property_entry("boss", "objects", [adnan.id]),
            # `links` mirrors the semantic `boss` target AND carries a bare
            # body mention of Mira.
            mapping.property_entry("links", "objects", [adnan.id, mira.id]),
        ])
        await repo.hydrate()
        out = {(e.type, e.target) for e in repo.graph.edges(greg_id)}
        assert out == {("boss", adnan.id), ("links", mira.id)}
