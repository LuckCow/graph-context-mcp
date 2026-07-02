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
            mapping.property_entry(mapping.PROP_SUMMARY, "text", "Worker."),
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
            mapping.property_entry(mapping.PROP_SUMMARY, "text", "A company."),
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
            mapping.property_entry(mapping.PROP_SUMMARY, "text", "Worker."),
            mapping.property_entry("boss", "objects", [adnan.id]),
            # `links` mirrors the semantic `boss` target AND carries a bare
            # body mention of Mira.
            mapping.property_entry("links", "objects", [adnan.id, mira.id]),
        ])
        await repo.hydrate()
        out = {(e.type, e.target) for e in repo.graph.edges(greg_id)}
        assert out == {("boss", adnan.id), ("links", mira.id)}


class TestSingleWriterFreshReads:
    """ADR 009: relation writes build on store truth read inside the
    critical section, so both bot-vs-bot and human-vs-bot overwrites of
    relation lists are prevented / detected. The bot-vs-bot guarantee is
    port-level (contract suite); the human-edit behavior is adapter-only
    (the fake has no out-of-band editors), so it lives here."""

    async def test_out_of_band_relation_edit_survives_a_link_write(
        self, repo, client, caplog
    ):
        import logging

        mira = await repo.create_node(CHAR)
        orla = await repo.create_node(
            NodeDraft("Character", name="Orla", summary="Ally.")
        )
        adnan = await repo.create_node(
            NodeDraft("Character", name="Adnan", summary="Boss.")
        )
        # A human adds mira -knows-> adnan in the Anytype UI: store changes,
        # the repository's index does not.
        await client.update_object(
            mira.id, mapping.relation_patch_payload("gc_edge_knows", [adnan.id])
        )
        # The bot links mira -knows-> orla. Before ADR 009 the PATCH payload
        # came from the index view ([]) and clobbered the human's edit.
        with caplog.at_level(logging.WARNING):
            edge = await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
        assert edge.target == orla.id
        assert any("out-of-band edit" in r.getMessage() for r in caplog.records)
        await repo.hydrate()  # store truth: both targets present
        assert {n.id for _, n in repo.graph.neighbors(mira.id)} == {orla.id, adnan.id}
        assert repo.pending_writes == 0  # depth surface idles at zero


class TestSummaryChannel:
    """ADR 011: the summary is stored in Anytype's BUILT-IN description
    property (UI-featured, present in list/search -- so it hydrates), not
    in a gc_ key. Pinned against the mock's store, not just round-tripped
    through our own mapping."""

    async def test_summary_lands_in_the_builtin_description_property(
        self, repo, mock
    ):
        node = await repo.create_node(CHAR)
        stored = {
            p["key"]: p.get("text")
            for p in mock.object(node.id)["properties"]
            if p.get("format") == "text"
        }
        assert stored["description"] == "Engineer."
        assert "gc_summary" not in stored

    async def test_summary_update_patches_the_builtin_property(self, repo, mock):
        node = await repo.create_node(CHAR)
        await repo.update_node(node.id, summary="Leads the survivors.")
        stored = {p["key"]: p.get("text") for p in mock.object(node.id)["properties"]}
        assert stored["description"] == "Leads the survivors."

    async def test_human_edit_to_builtin_description_reaches_summary_on_resync(
        self, repo, mock
    ):
        """The point of ADR 011: humans now edit summaries in the UI; the
        edit arrives like any out-of-band property change."""
        node = await repo.create_node(CHAR)
        mock.edit_object_directly(node.id, set_property=mapping.property_entry(
            "description", "text", "Human-sharpened one-liner."
        ))
        changed = await repo.resync()
        assert node.id in changed
        assert repo.graph.node(node.id).summary == "Human-sharpened one-liner."

    async def test_retired_gc_summary_is_invisible(self, repo, mock):
        """Only the built-in description is the summary channel (ADR 011).
        An unmigrated object's gc_summary is not read -- the migration
        script (scripts/migrate_summary_to_description.py) is the one
        converter."""
        legacy_id = mock.seed_object("character", "Orla", properties=[
            mapping.property_entry(mapping.PROP_LEGACY_SUMMARY, "text", "A smuggler."),
        ])
        await repo.hydrate()
        assert repo.graph.node(legacy_id).summary == ""
