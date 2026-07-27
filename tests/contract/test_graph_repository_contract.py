"""The GraphRepository contract: one behavioral spec, every implementation.

Each concrete repository inherits the contract class and provides a
``repo`` fixture. A behavior that cannot be expressed by ALL
implementations means the port is wrong -- fix the port, not the adapter.

When live-server access exists, add a third subclass gated behind
``ANYTYPE_E2E=1`` pointing the same tests at a real space.
"""

import asyncio
import json

import pytest

from graph_context.domain import attribution
from graph_context.domain.graph import Direction
from graph_context.domain.models import (
    FieldSpec,
    LinkSpec,
    NodeDraft,
    PropertyDeclaration,
    PropertyDraft,
)
from graph_context.domain.query import NodeQuery, Op, Predicate, run_query
from graph_context.domain.schema import Role
from graph_context.errors import (
    GraphContextError,
    NodeNotFound,
    SchemaChangeConflict,
    UnknownFieldKey,
    UnknownNodeType,
    UnknownRelationLabel,
)
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from graph_context.infrastructure.memory.fake_repository import (
    FakeTemplate,
    InMemoryGraphRepository,
)
from tests.anytype.conftest import seed_native_types

SCAFFOLD = "## Template header"

CHAR = NodeDraft("Character", name="Mira", summary="Exiled siege engineer.")
PLACE = NodeDraft("Location", name="The Undercroft", summary="Vaults beneath Brakk.")


class GraphRepositoryContract:
    """Inherit + provide a `repo` fixture to certify an implementation."""

    async def test_create_assigns_id_and_lands_in_graph(self, repo):
        node = await repo.create_node(CHAR)
        assert node.id
        assert repo.graph.node(node.id).name == "Mira"

    async def test_composite_create_writes_links_on_the_new_node(self, repo):
        # Links live on their SOURCE (ADR 042 retired incoming links): a
        # composite create's edges all run from the created node outward.
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        sword = await repo.create_node(
            NodeDraft("Item", name="Ashbrand", summary="A blade."),
        )
        faction = await repo.create_node(
            NodeDraft("Organization", name="Emberguard", summary="Defenders."),
            links=[
                LinkSpec("possesses", other=sword.id),
                LinkSpec("located_at", other=place.id),
                LinkSpec("member_of", other=mira.id),
            ],
        )
        assert {n.id for _, n in repo.graph.neighbors(faction.id)} == {
            sword.id, place.id, mira.id,
        }

    async def test_create_with_missing_link_target_rolls_back(self, repo):
        before = repo.graph.node_count()
        with pytest.raises(NodeNotFound):
            await repo.create_node(
                CHAR, links=[LinkSpec("knows", other="no-such-node")]
            )
        assert repo.graph.node_count() == before

    async def test_update_applies_only_provided_fields(self, repo):
        node = await repo.create_node(CHAR)
        updated = await repo.update_node(
            node.id, body="Leads the survivors.", summary_stale=True
        )
        assert updated.summary == "Exiled siege engineer."  # untouched
        assert updated.summary_stale is True

    async def test_body_round_trips_and_updates(self, repo):
        """ADR 010: the body is the node's description -- mutable, on-demand.

        Compared stripped: the live server normalizes markdown on store
        (S6 -- trailing whitespace changes), so byte equality is not part
        of the contract.
        """
        node = await repo.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.",
                      body="Born in the Undercroft."),
        )
        assert (await repo.fetch_body(node.id)).strip() == "Born in the Undercroft."
        await repo.update_node(node.id, body="Leads the survivors now.")
        assert (await repo.fetch_body(node.id)).strip() == "Leads the survivors now."

    async def test_fenced_jsonl_body_round_trips_intact(self, repo):
        """ADR 049's load-bearing storage assumption: the revision
        historian keeps its log as JSON lines inside one fence in a
        sidecar body. The store may normalize prose, but fence CONTENTS
        must survive create -> fetch_body byte-usable (parsed back as
        JSON) -- the ``ANYTYPE_E2E=1`` run of this test is the live
        spike (docs/spikes/node-history-body.md)."""
        lines = [
            json.dumps(
                {"seq": i, "at": f"T{i}", "kind": "delta",
                 "ops": [["equal", 0, 3]],
                 "new_blocks": {"abc123": 'text with "quotes" & *marks*'}},
                sort_keys=True, separators=(",", ":"),
            )
            for i in range(3)
        ]
        body = "A header sentence.\n\n```\n" + "\n".join(lines) + "\n```"
        node = await repo.create_node(
            NodeDraft("Character", name="Log", summary="Sidecar-shaped.",
                      body=body),
        )
        fetched = await repo.fetch_body(node.id)
        in_fence, parsed = False, []
        for line in fetched.splitlines():
            if line.strip().startswith("```"):
                in_fence = not in_fence
            elif in_fence and line.strip():
                parsed.append(json.loads(line))
        assert [json.dumps(p, sort_keys=True, separators=(",", ":"))
                for p in parsed] == lines

    async def test_update_without_body_leaves_body_alone(self, repo):
        node = await repo.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.",
                      body="Original description."),
        )
        await repo.update_node(node.id, summary="Fresh summary.")
        assert (await repo.fetch_body(node.id)).strip() == "Original description."

    async def test_empty_body_update_clears_it(self, repo):
        node = await repo.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.",
                      body="Disposable."),
        )
        await repo.update_node(node.id, body="")
        assert await repo.fetch_body(node.id) == ""

    async def test_update_unknown_node_raises(self, repo):
        with pytest.raises(NodeNotFound):
            await repo.update_node("ghost", name="?")

    async def test_add_and_remove_link_round_trip(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        edge = await repo.add_link(mira.id, LinkSpec("located_at", other=place.id))
        assert list(repo.graph.edges(mira.id)) == [edge]
        await repo.remove_link(edge)
        assert list(repo.graph.edges(mira.id)) == []

    async def test_update_bumps_modified_at(self, repo):
        """The store clock ticks on every write (ADR 042): the rule
        engine's built-in watch and ranking recency both read it.
        ``>=`` not ``>``: the LIVE clock has second resolution, so
        back-to-back writes may share a stamp -- the built-in watch
        targets human-timescale edits, which never do."""
        node = await repo.create_node(CHAR)
        assert node.modified_at
        updated = await repo.update_node(node.id, summary="Fresh.")
        assert updated.modified_at >= node.modified_at

    async def test_link_writes_refresh_the_source_modified_at(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(PLACE)
        before = repo.graph.node(mira.id).modified_at
        edge = await repo.add_link(mira.id, LinkSpec("located_at", other=place.id))
        after_add = repo.graph.node(mira.id).modified_at
        assert after_add and after_add >= before
        await repo.remove_link(edge)
        assert repo.graph.node(mira.id).modified_at >= after_add

    async def test_fields_round_trip(self, repo):
        # "fuel" is not a property anywhere; the declaration mints it as a
        # real one (ADR 023). In the live run this exercises scalar
        # create_property for real; reruns reuse the surviving property.
        node = await repo.create_node(
            NodeDraft("Technology", name="Ashforge", summary="A forge.",
                      fields={"fuel": "bonemeal"}),
            create_missing={"fuel": PropertyDeclaration("fuel", "text")},
        )
        assert repo.graph.node(node.id).fields == {"fuel": "bonemeal"}

    async def test_query_neq_true_on_absent_field_matches_unticked_objects(
        self, repo
    ):
        """The open-todos idiom end-to-end: a done-ness field is only
        present when set (an unticked Anytype checkbox is dropped as
        absence), and the query engine's ``neq`` matches that absence --
        whichever repository populated the index."""
        ticked = await repo.create_node(
            NodeDraft("Item", name="Ticked", summary="s.",
                      fields={"done": "true"}),
            create_missing={"done": PropertyDeclaration("done", "checkbox")},
        )
        unticked = await repo.create_node(
            NodeDraft("Item", name="Unticked", summary="s.")
        )
        result = run_query(
            repo.graph,
            NodeQuery(
                node_type="Item",
                predicates=(Predicate("done", Op.NEQ, "true"),),
                limit=100,
            ),
        )
        hit_ids = {node.id for node in result.hits}
        # Membership, not equality: the LIVE contract run shares one space
        # across the session, so unrelated Items may match too.
        assert unticked.id in hit_ids
        assert ticked.id not in hit_ids

    async def test_concurrent_link_mutations_on_one_node_all_take_effect(self, repo):
        """Port guarantee (ADR 009): overlapping link writes against one
        source node must ALL land in the store -- a stale read-modify-write
        of the relation list may not silently drop a sibling's update,
        however the event loop interleaves the calls. Asserted against the
        STORE (post-hydrate), not the index: the lost update only shows
        there."""
        mira = await repo.create_node(CHAR)
        sites = [
            await repo.create_node(
                NodeDraft("Location", name=f"Site {i}", summary="A place.")
            )
            for i in range(3)
        ]
        await asyncio.gather(
            *[
                repo.add_link(mira.id, LinkSpec("located_at", other=site.id))
                for site in sites
            ]
        )
        await repo.hydrate()  # rebuild the index from store truth
        assert {n.id for _, n in repo.graph.neighbors(mira.id)} == {
            site.id for site in sites
        }


class RoleOverrideContract:
    """Constructor ``role_overrides`` (WP5 domain profiles) shape role
    resolution identically in every implementation: the mapped type gains
    the role both on lookup and on the created node."""

    async def test_overridden_type_resolves_and_stamps_the_role(self, meeting_repo):
        assert meeting_repo.role_for("Meeting") is Role.EVENT
        node = await meeting_repo.create_node(
            NodeDraft(
                "Meeting", name="Standup", summary="Daily sync.",
                story_time=20260702,
            )
        )
        assert node.role is Role.EVENT
        assert meeting_repo.graph.node(node.id).role is Role.EVENT


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
        await seed_native_types(client)
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        yield repository
        await client.aclose()


class TemplateContract:
    """A type template applied on create shapes every implementation the same:
    its default property values land on the new node, caller-supplied fields
    override those defaults, and the template body precedes the caller's body.
    Seeded on ``Item`` with ``status`` defaulting to ``To Do``."""

    async def test_template_default_property_lands_on_create(self, template_repo):
        node = await template_repo.create_node(NodeDraft("Item", name="Relic", summary="s."))
        assert node.fields.get("status") == "To Do"

    async def test_explicit_field_overrides_template_default(self, template_repo):
        node = await template_repo.create_node(
            NodeDraft("Item", name="Relic", summary="s.", fields={"status": "In Progress"})
        )
        assert node.fields.get("status") == "In Progress"

    async def test_template_body_precedes_caller_body(self, template_repo):
        node = await template_repo.create_node(
            NodeDraft("Item", name="Relic", summary="s.", body="Caller body.")
        )
        body = await template_repo.fetch_body(node.id)
        assert "Template header" in body and "Caller body." in body
        assert body.index("Template header") < body.index("Caller body.")


class FieldCatalogContract:
    """ADR 023/047: story-node ``fields`` keys resolve against the target
    TYPE's attached properties identically in every implementation.
    Seeded on ``Item`` with "Due date" (date), "Status" (select: To Do,
    In Progress), an "Assignee" objects-format RELATION (an edge, never a
    fields key), and the attached "Linked Projects" relation; the space
    additionally holds UNATTACHED vocabulary -- the "Linked Project"
    relation (the ADR 047 incident's near-namesake), a "Priority" number
    -- and the seeded ``gc_edge_knows`` starter relation."""

    async def test_relation_label_for_matches_key_and_display_name(
        self, catalog_repo
    ):
        """The tool boundary routes a relation-named fields key into links
        (turn 1bb6286b0e21); this is the question it asks, and it must
        match exactly like fields-key resolution: key or display name,
        case-insensitive."""
        for spelling in ("assignee", "Assignee", "ASSIGNEE"):
            label = catalog_repo.relation_label_for(spelling, on_type="Item")
            assert label is not None and label.lower() == "assignee"

    async def test_relation_label_for_is_none_for_scalars_and_unknowns(
        self, catalog_repo
    ):
        assert catalog_repo.relation_label_for("Due date", on_type="Item") is None
        assert catalog_repo.relation_label_for("due_date", on_type="Item") is None
        assert catalog_repo.relation_label_for("nonesuch", on_type="Item") is None

    async def test_relation_label_for_is_none_for_unattached_space_relations(
        self, catalog_repo
    ):
        """ADR 047, the incident: the unattached near-namesake must NOT
        resolve bare -- only the type's own relation does."""
        assert (
            catalog_repo.relation_label_for("Linked Project", on_type="Item")
            is None
        )
        assert (
            catalog_repo.relation_label_for("Linked Projects", on_type="Item")
            is not None
        )

    async def test_relation_label_for_requires_exactly_one_scope(
        self, catalog_repo
    ):
        """An unscoped call must be impossible -- space-wide bare
        resolution is the bug ADR 047 removes."""
        with pytest.raises(ValueError):
            catalog_repo.relation_label_for("Assignee")
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Scoped", summary="s.")
        )
        with pytest.raises(ValueError):
            catalog_repo.relation_label_for(
                "Assignee", on_type="Item", on_node=node.id
            )

    async def test_relation_named_as_a_field_redirects_to_links(
        self, catalog_repo
    ):
        """Port-level backstop: the tool boundary coerces a relation-named
        fields key into a link, so a direct repository caller that skips
        that boundary still gets redirected -- the error must point at
        links, not at minting a shadowing scalar property."""
        with pytest.raises(UnknownFieldKey) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Assignee": "Nick"})
            )
        message = str(err.value)
        assert "RELATION" in message and "properties=" in message
        assert "create_missing" not in message

    async def test_a_relation_key_cannot_be_shadowed_by_declaration(
        self, catalog_repo
    ):
        """A declaration must not mint a scalar over a relation."""
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Assignee": "Nick"}),
                create_missing={
                    "Assignee": PropertyDeclaration("Assignee", "text")
                },
            )

    async def test_unknown_link_label_errors_with_existing_relations(
        self, catalog_repo
    ):
        """Live-caught (turn 1bb6286b0e21): a model invented 'assigned_to'
        where the space's relation is 'assignee'. The error must offer the
        existing vocabulary and the explicit opt-in, and the composite
        create must roll back -- no node, no junk-labelled edge."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Nick", summary="s.")
        )
        with pytest.raises(UnknownRelationLabel) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s."),
                links=[LinkSpec(edge_type="assigned_to", other=target.id)],
            )
        message = str(err.value)
        assert "create_missing_properties" in message
        assert "assignee" in message.lower()
        with pytest.raises(NodeNotFound):
            catalog_repo.graph.resolve("Ship it")

    async def test_link_label_matches_by_key_or_display_name(
        self, catalog_repo
    ):
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Nick", summary="s.")
        )
        for spelling in ("assignee", "Assignee"):
            node = await catalog_repo.create_node(
                NodeDraft("Item", name=f"Task via {spelling}", summary="s."),
                links=[LinkSpec(edge_type=spelling, other=target.id)],
            )
            edge = next(iter(catalog_repo.graph.edges(node.id, Direction.OUT)))
            # Both spellings canonicalize to the SAME relation.
            assert edge.type.lower() == "assignee"

    async def test_a_minted_relation_needs_redeclaring_on_another_object(
        self, catalog_repo
    ):
        """ADR 047 flip of the old mints-a-reusable-relation pin: a
        declaration mints SPACE-level vocabulary attached to no type, so
        a different object cannot use it bare -- the same declaration
        REUSES it (attach, never a twin)."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Nick", summary="s.")
        )
        first = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s."),
            links=[LinkSpec(edge_type="approved_by", other=target.id)],
            create_missing={
                "approved_by": PropertyDeclaration("approved_by", "objects")
            },
        )
        assert any(
            e.type.lower() == "approved_by"
            for e in catalog_repo.graph.edges(first.id, Direction.OUT)
        )
        with pytest.raises(UnknownRelationLabel):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Land it", summary="s."),
                links=[LinkSpec(edge_type="approved_by", other=target.id)],
            )
        labels_before = catalog_repo.known_edge_labels()
        second = await catalog_repo.create_node(
            NodeDraft("Item", name="Land it", summary="s."),
            links=[LinkSpec(edge_type="approved_by", other=target.id)],
            create_missing={
                "approved_by": PropertyDeclaration("approved_by", "objects")
            },
        )
        assert any(
            e.type.lower() == "approved_by"
            for e in catalog_repo.graph.edges(second.id, Direction.OUT)
        )
        # The redeclaration reused the existing relation: no new vocabulary.
        assert catalog_repo.known_edge_labels() == labels_before

    async def test_relations_stay_out_of_the_fields_catalog(
        self, catalog_repo
    ):
        rendered = {
            spec.name
            for specs in catalog_repo.field_catalog().values()
            for spec in specs
        }
        assert "Assignee" not in rendered

    async def test_unmatched_key_on_create_errors_with_guidance(self, catalog_repo):
        with pytest.raises(UnknownFieldKey) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"due": "2026-08-01"})
            )
        message = str(err.value)
        assert "Due date" in message and "(date)" in message
        assert "create_missing_properties" in message

    async def test_unmatched_key_on_update_errors_with_guidance(self, catalog_repo):
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.")
        )
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.update_node(node.id, fields={"due": "2026-08-01"})

    async def test_display_name_key_writes_the_property(self, catalog_repo):
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"Due date": "2026-08-01"})
        )
        # Read-back is under the property's raw key, both backends alike.
        assert catalog_repo.graph.node(node.id).fields["due_date"] == (
            "2026-08-01T00:00:00Z"  # live reads a bare date back as midnight UTC
        )

    async def test_an_offset_date_value_reads_back_as_the_utc_instant(
        self, catalog_repo
    ):
        """A15/R2: an RFC 3339 stamp WITH a timezone is accepted and
        reads back normalized to UTC -- local midnight keeps its
        calendar date (the rule engine's set-property-to-now shape)."""
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"Due date": "2026-08-01T00:00:00-04:00"})
        )
        assert catalog_repo.graph.node(node.id).fields["due_date"] == (
            "2026-08-01T04:00:00Z"
        )

    async def test_a_naive_timestamp_date_value_errors_with_the_fix(
        self, catalog_repo
    ):
        """A15/R2: the live store 400s naive timestamps on date
        properties; both backends must reject them BEFORE the wire with
        the self-correcting spelling hint instead."""
        with pytest.raises(GraphContextError, match="timezone"):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Due date": "2026-08-01 10:00:00"})
            )

    async def test_declared_key_matching_an_existing_property_reuses_it(
        self, catalog_repo
    ):
        """The de38192f56dc fix (ADR 042/D7): declaring a key that already
        matches a same-format space property is a harmless reuse -- the
        write lands; never a duplicate mint, never an opaque store error."""
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"Due date": "2026-08-01"}),
            create_missing={
                "Due date": PropertyDeclaration("Due date", "date")
            },
        )
        assert catalog_repo.graph.node(node.id).fields["due_date"] == (
            "2026-08-01T00:00:00Z"  # live reads a bare date back as midnight UTC
        )

    async def test_declared_format_mismatch_conflicts_loudly(
        self, catalog_repo
    ):
        """A12: the existing property's format wins; a mismatched
        declaration must stop the write with a self-correcting error."""
        with pytest.raises(SchemaChangeConflict, match="immutable"):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Due date": "soon"}),
                create_missing={
                    "Due date": PropertyDeclaration("Due date", "text")
                },
            )

    async def test_a_minted_property_is_not_bare_usable_on_another_object(
        self, catalog_repo
    ):
        """ADR 047 flip of the old mints-a-reusable-property pin: the mint
        is space-level, attached to no type -- another object needs the
        same declaration, which reuses the property instead of minting a
        twin."""
        first = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"effort": "3"}),
            create_missing={"effort": PropertyDeclaration("effort", "number")},
        )
        assert first.fields["effort"] == "3"
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Land it", summary="s.",
                          fields={"effort": "5"})
            )
        second = await catalog_repo.create_node(
            NodeDraft("Item", name="Land it", summary="s.",
                      fields={"effort": "5"}),
            create_missing={"effort": PropertyDeclaration("effort", "number")},
        )
        assert second.fields["effort"] == "5"

    async def test_an_unattached_space_property_does_not_resolve_bare(
        self, catalog_repo
    ):
        """ADR 047, the incident shape: space vocabulary no type claims
        must not resolve bare -- and the composite create rolls back."""
        with pytest.raises(UnknownFieldKey) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Priority": "3"})
            )
        assert "NOT attached" in str(err.value)
        with pytest.raises(NodeNotFound):
            catalog_repo.graph.resolve("Ship it")

    async def test_an_unattached_space_relation_does_not_resolve_bare(
        self, catalog_repo
    ):
        """The exact incident: 'Linked Project' exists in the space but
        not on the type -- a bare link must error while the type's own
        'Linked Projects' keeps working."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Thesis Revisions", summary="s.")
        )
        with pytest.raises(UnknownRelationLabel):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Fix margins", summary="s."),
                links=[LinkSpec(edge_type="Linked Project", other=target.id)],
            )
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Fix margins", summary="s."),
            links=[LinkSpec(edge_type="Linked Projects", other=target.id)],
        )
        edge = next(iter(catalog_repo.graph.edges(node.id, Direction.OUT)))
        assert edge.type.lower().replace(" ", "_") == "linked_projects"

    async def test_unattached_key_error_teaches_the_attach_path(
        self, catalog_repo
    ):
        """The error is a prompt: it names the exact space match, its
        format, and the create_missing_properties reuse-attach gesture."""
        with pytest.raises(UnknownFieldKey) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Linked Project": "Thesis"})
            )
        message = str(err.value)
        assert "Linked Project" in message and "NOT attached" in message
        assert "create_missing_properties" in message
        assert "objects" in message

    async def test_declaring_an_unattached_relation_reuses_it_without_minting(
        self, catalog_repo
    ):
        """The deliberate attach path: a declaration naming the existing
        unattached relation links through IT -- no twin joins the
        vocabulary."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Thesis Revisions", summary="s.")
        )
        labels_before = catalog_repo.known_edge_labels()
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Fix margins", summary="s."),
            links=[LinkSpec(edge_type="Linked Project", other=target.id)],
            create_missing={
                "Linked Project": PropertyDeclaration(
                    "Linked Project", "objects"
                )
            },
        )
        edge = next(iter(catalog_repo.graph.edges(node.id, Direction.OUT)))
        assert edge.type.lower().replace(" ", "_") == "linked_project"
        assert catalog_repo.known_edge_labels() == labels_before

    async def test_update_keeps_this_objects_local_properties_editable(
        self, catalog_repo
    ):
        """ADR 047 rule 2: an instance-attached (local) property stays
        editable on ITS object without redeclaring."""
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"Priority": "3"}),
            create_missing={
                "Priority": PropertyDeclaration("Priority", "number")
            },
        )
        updated = await catalog_repo.update_node(
            node.id, fields={"Priority": "5"}
        )
        assert updated.fields["priority"] == "5"

    async def test_another_objects_local_property_does_not_leak(
        self, catalog_repo
    ):
        """ADR 047 rule 2, the other half: one object's local property
        grants nothing to its neighbors."""
        await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"Priority": "3"}),
            create_missing={
                "Priority": PropertyDeclaration("Priority", "number")
            },
        )
        other = await catalog_repo.create_node(
            NodeDraft("Item", name="Land it", summary="s.")
        )
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.update_node(other.id, fields={"Priority": "1"})

    async def test_add_link_resolves_against_the_nodes_type_and_instance(
        self, catalog_repo
    ):
        """add_link admits the anchor's own relations (instance scope) but
        not another object's."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Thesis Revisions", summary="s.")
        )
        second_target = await catalog_repo.create_node(
            NodeDraft("Item", name="Side Project", summary="s.")
        )
        holder = await catalog_repo.create_node(
            NodeDraft("Item", name="Fix margins", summary="s."),
            links=[LinkSpec(edge_type="Linked Project", other=target.id)],
            create_missing={
                "Linked Project": PropertyDeclaration(
                    "Linked Project", "objects"
                )
            },
        )
        await catalog_repo.add_link(
            holder.id, LinkSpec(edge_type="Linked Project", other=second_target.id)
        )
        assert len(list(catalog_repo.graph.edges(holder.id, Direction.OUT))) == 2
        fresh = await catalog_repo.create_node(
            NodeDraft("Item", name="Other task", summary="s.")
        )
        with pytest.raises(UnknownRelationLabel):
            await catalog_repo.add_link(
                fresh.id, LinkSpec(edge_type="Linked Project", other=target.id)
            )

    async def test_seeded_edge_vocabulary_stays_bare_usable(
        self, catalog_repo
    ):
        """The ``gc_edge_*`` starter relations are deliberately type-less
        and exempt from type scoping (ADR 047 user decision)."""
        target = await catalog_repo.create_node(
            NodeDraft("Item", name="Nick", summary="s.")
        )
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s."),
            links=[LinkSpec(edge_type="knows", other=target.id)],
        )
        edge = next(iter(catalog_repo.graph.edges(node.id, Direction.OUT)))
        assert "knows" in edge.type.lower()

    async def test_catalog_is_exposed_for_guidance(self, catalog_repo):
        catalog = catalog_repo.field_catalog()
        rendered = {
            (spec.name, spec.format)
            for specs in catalog.values() for spec in specs
        }
        assert ("Due date", "date") in rendered
        assert ("Status", "select") in rendered

    async def test_infra_attribution_fields_resolve_natively(self, catalog_repo):
        """ADR 028: recorder stamps write the bootstrap-guaranteed
        attribution properties -- no infra exemption, no blob."""
        node = await catalog_repo.create_node(
            NodeDraft("gc_prose", name="Scene 1", summary="A capture.",
                      fields={attribution.FIELD_USER_ID: "u-1"})
        )
        stored = catalog_repo.graph.node(node.id).fields
        assert stored[attribution.FIELD_USER_ID] == "u-1"

    async def test_attribution_stamps_resolve_bare_on_native_types(
        self, catalog_repo
    ):
        """The turn-a0d7b7350c34 regression: a capture whose artifact
        type is NATIVE (ADR 015) still stamps the recorder's attribution
        keys -- bot-owned space vocabulary, exempt from type scoping like
        the ``gc_edge_*`` starter relations, with no declaration and no
        model in the loop to self-correct."""
        node = await catalog_repo.create_node(
            NodeDraft("Item", name="Tournament Morning", summary="A chapter.",
                      fields={attribution.FIELD_GENERATED_AT:
                              "2026-07-26T22:24:43+00:00"})
        )
        stored = catalog_repo.graph.node(node.id).fields
        assert stored[attribution.FIELD_GENERATED_AT] == (
            "2026-07-26T22:24:43+00:00"
        )

    async def test_infra_unmatched_field_errors_like_any_other(self, catalog_repo):
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.create_node(
                NodeDraft("gc_prose", name="Scene 1", summary="A capture.",
                          fields={"free_form": "nope"})
            )

    async def test_attribution_keys_stay_out_of_the_offered_catalog(
        self, catalog_repo
    ):
        """The stamps are recorder-owned (ADR 028): writable, but never
        offered as story-field vocabulary."""
        rendered = {
            spec.key
            for specs in catalog_repo.field_catalog().values()
            for spec in specs
        }
        assert not rendered & set(attribution.ATTRIBUTION_FIELDS)


class TestInMemoryRoleOverrides(RoleOverrideContract):
    @pytest.fixture
    def meeting_repo(self):
        return InMemoryGraphRepository(role_overrides={"meeting": Role.EVENT})


class TestAnytypeRoleOverrides(RoleOverrideContract):
    @pytest.fixture
    async def meeting_repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        # The space-reflecting model resolves types against the live space:
        # the type must exist for the override to have anything to bite on.
        await client.create_type(
            {"key": "meeting", "name": "Meeting",
             "plural_name": "Meetings", "layout": "basic"}
        )
        repository = AnytypeGraphRepository(
            client, role_overrides={"meeting": Role.EVENT}
        )
        await repository.hydrate()
        yield repository
        await client.aclose()


class TestInMemoryFieldCatalog(FieldCatalogContract):
    @pytest.fixture
    def catalog_repo(self):
        return InMemoryGraphRepository(
            field_catalog=[
                FieldSpec(name="Due date", format="date", key="due_date"),
                FieldSpec(name="Status", format="select", key="status",
                          options=("To Do", "In Progress")),
                FieldSpec(name="Assignee", format="objects", key="assignee"),
                # The ADR 047 incident pair: the attached relation and its
                # unattached near-namesake, plus an unattached scalar.
                FieldSpec(name="Linked Projects", format="objects",
                          key="linked_projects"),
                FieldSpec(name="Linked Project", format="objects",
                          key="linked_project"),
                FieldSpec(name="Priority", format="number", key="priority"),
                # Seeded starter vocabulary: bare-usable everywhere.
                FieldSpec(name="knows", format="objects", key="gc_edge_knows"),
            ],
            attachments={
                "Item": ("Due date", "Status", "Assignee", "Linked Projects"),
            },
        )


class TestAnytypeFieldCatalog(FieldCatalogContract):
    @pytest.fixture
    async def catalog_repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        await client.create_property(
            {"key": "due_date", "name": "Due date", "format": "date"}
        )
        status = await client.create_property(
            {"key": "status", "name": "Status", "format": "select"}
        )
        await client.create_tag(status["id"], {"name": "To Do", "color": "ice"})
        await client.create_tag(status["id"], {"name": "In Progress", "color": "lime"})
        await client.create_property(
            {"key": "assignee", "name": "Assignee", "format": "objects"}
        )
        # The ADR 047 incident pair (attached below) + an unattached scalar.
        await client.create_property(
            {"key": "linked_projects", "name": "Linked Projects",
             "format": "objects"}
        )
        await client.create_property(
            {"key": "linked_project", "name": "Linked Project",
             "format": "objects"}
        )
        await client.create_property(
            {"key": "priority", "name": "Priority", "format": "number"}
        )
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        # Attach the bare-usable vocabulary to Item through the WP33 port
        # surface -- the same reuse-attach path a confirmed proposal takes.
        await repository.add_type_properties("Item", (
            PropertyDraft(name="Due date", format="date"),
            PropertyDraft(name="Status", format="select"),
            PropertyDraft(name="Assignee", format="objects"),
            PropertyDraft(name="Linked Projects", format="objects"),
        ))
        yield repository
        await client.aclose()


class SchemaChangeContract:
    """WP33 (ADR 041): user-confirmed schema changes behave identically in
    every implementation. Seeded with a "Status" select property and an
    "Assignee" objects-format relation so the reuse/conflict semantics
    have something to bite on."""

    async def test_created_type_is_immediately_a_create_target(self, schema_repo):
        name = await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Motto", format="text"),)
        )
        assert name == "Faction"
        assert "Faction" in schema_repo.known_node_types()
        node = await schema_repo.create_node(
            NodeDraft("Faction", name="Iron Pact", summary="s.")
        )
        assert schema_repo.graph.node(node.id).name == "Iron Pact"

    async def test_created_type_offers_its_properties_as_fields(self, schema_repo):
        await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Motto", format="text"),)
        )
        specs = schema_repo.field_catalog().get("Faction", ())
        assert any(s.name == "Motto" and s.format == "text" for s in specs)

    async def test_create_type_conflicts_with_an_existing_type(self, schema_repo):
        with pytest.raises(SchemaChangeConflict, match="already exists"):
            await schema_repo.create_type("Item")

    async def test_added_properties_join_without_stripping_existing_ones(
        self, schema_repo
    ):
        """The A11 assertion: the type update replaces the property list
        wholesale on the wire, so additions must ride with the fetched
        list -- a careless adapter strips 'Motto' here."""
        await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Motto", format="text"),)
        )
        await schema_repo.add_type_properties(
            "Faction", (PropertyDraft(name="Influence", format="number"),)
        )
        names = {s.name for s in schema_repo.field_catalog().get("Faction", ())}
        assert {"Motto", "Influence"} <= names

    async def test_add_properties_to_unknown_type_raises(self, schema_repo):
        with pytest.raises(UnknownNodeType):
            await schema_repo.add_type_properties(
                "NoSuchType", (PropertyDraft(name="X", format="text"),)
            )

    async def test_existing_same_format_property_is_reused(self, schema_repo):
        """A draft naming the seeded "Status" select attaches the existing
        property; a subsequent write resolves it under its canonical key."""
        await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Status", format="select"),)
        )
        node = await schema_repo.create_node(
            NodeDraft("Faction", name="Iron Pact", summary="s.",
                      fields={"Status": "To Do"})
        )
        assert node.fields.get("status") == "To Do"

    async def test_existing_other_format_property_conflicts(self, schema_repo):
        with pytest.raises(SchemaChangeConflict, match="immutable"):
            await schema_repo.create_type(
                "Faction",
                properties=(PropertyDraft(name="Status", format="date"),),
            )

    async def test_relation_named_property_conflicts(self, schema_repo):
        with pytest.raises(SchemaChangeConflict, match="relation"):
            await schema_repo.create_type(
                "Faction",
                properties=(PropertyDraft(name="Assignee", format="text"),),
            )

    async def test_objects_draft_attaches_the_existing_relation(
        self, schema_repo
    ):
        """ADR 042: an objects draft naming the seeded "Assignee" relation
        is a reuse-attach, not a conflict -- and the label keeps resolving
        for links afterwards. (On the Anytype path this exercises the A11
        amendment: the reuse entry must carry the property id or the
        type PATCH 400s "already exists".)"""
        await schema_repo.create_type(
            "Faction",
            properties=(PropertyDraft(name="Assignee", format="objects"),),
        )
        assert (
            schema_repo.relation_label_for("Assignee", on_type="Faction")
            is not None
        )

    async def test_objects_draft_extends_an_existing_type(self, schema_repo):
        await schema_repo.create_type("Faction")
        await schema_repo.add_type_properties(
            "Faction", (PropertyDraft(name="Assignee", format="objects"),)
        )
        assert (
            schema_repo.relation_label_for("Assignee", on_type="Faction")
            is not None
        )

    async def test_a_minted_property_is_not_bare_usable_until_attached(
        self, schema_repo
    ):
        """ADR 047 requirement 4: a scope='type' mint creates the space
        property immediately, but until the drafted proposal is applied
        (``add_type_properties``) other objects cannot use it bare; the
        apply makes it bare-usable with no resync."""
        await schema_repo.create_node(
            NodeDraft("Item", name="One", summary="s.",
                      fields={"effort": "3"}),
            create_missing={"effort": PropertyDeclaration("effort", "number")},
        )
        with pytest.raises(UnknownFieldKey):
            await schema_repo.create_node(
                NodeDraft("Item", name="Two", summary="s.",
                          fields={"effort": "5"})
            )
        await schema_repo.add_type_properties(
            "Item", (PropertyDraft(name="effort", format="number"),)
        )
        node = await schema_repo.create_node(
            NodeDraft("Item", name="Three", summary="s.",
                      fields={"effort": "5"})
        )
        assert node.fields["effort"] == "5"

    async def test_readding_an_attached_property_is_a_noop(self, schema_repo):
        """Retry safety: a confirmed proposal applied twice changes nothing."""
        await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Motto", format="text"),)
        )
        await schema_repo.add_type_properties(
            "Faction", (PropertyDraft(name="Motto", format="text"),)
        )
        specs = schema_repo.field_catalog().get("Faction", ())
        assert sum(1 for s in specs if s.name == "Motto") == 1

    async def test_readding_under_another_format_conflicts(self, schema_repo):
        await schema_repo.create_type(
            "Faction", properties=(PropertyDraft(name="Motto", format="text"),)
        )
        with pytest.raises(SchemaChangeConflict, match="immutable"):
            await schema_repo.add_type_properties(
                "Faction", (PropertyDraft(name="Motto", format="number"),)
            )


class TestInMemorySchemaChanges(SchemaChangeContract):
    @pytest.fixture
    def schema_repo(self):
        # attachments={} turns on ADR 047 type scoping with nothing
        # attached yet -- exactly the adapter's posture, where the seeded
        # properties are space-level until a schema change attaches them.
        return InMemoryGraphRepository(
            field_catalog=[
                FieldSpec(name="Status", format="select", key="status",
                          options=("To Do", "In Progress")),
                FieldSpec(name="Assignee", format="objects", key="assignee"),
            ],
            attachments={},
        )


class TestAnytypeSchemaChanges(SchemaChangeContract):
    @pytest.fixture
    async def schema_repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        status = await client.create_property(
            {"key": "status", "name": "Status", "format": "select"}
        )
        await client.create_tag(status["id"], {"name": "To Do", "color": "ice"})
        await client.create_property(
            {"key": "assignee", "name": "Assignee", "format": "objects"}
        )
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        yield repository
        await client.aclose()


class MembersContract:
    """S11: space members are first-class, linkable nodes in every
    implementation -- seeded with one member named "Luckcow". Search/list
    never return participants live, so reflection is what makes an
    assignee-style edge possible at all."""

    def _member(self, repo):
        return next(
            n for n in repo.graph.nodes() if n.type == "Space member"
        )

    async def test_members_are_reflected_as_nodes(self, members_repo):
        member = self._member(members_repo)
        assert member.name == "Luckcow"
        assert member.type_key == "participant"

    async def test_a_created_node_can_link_to_a_member(self, members_repo):
        """The whole point (live-caught): 'assign the task to the
        requester' needs the member as an edge target."""
        member = self._member(members_repo)
        task = await members_repo.create_node(
            NodeDraft("Item", name="Take a shower", summary="s."),
            links=[LinkSpec("assignee", other=member.id)],
        )
        assert {n.id for _, n in members_repo.graph.neighbors(task.id)} == {
            member.id
        }


class TestInMemoryMembers(MembersContract):
    @pytest.fixture
    def members_repo(self):
        return InMemoryGraphRepository(members=["Luckcow"])


class TestAnytypeMembers(MembersContract):
    @pytest.fixture
    async def members_repo(self):
        mock = MockAnytype()
        mock.seed_member("Luckcow", role="owner")
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        await client.create_property(
            {"key": "assignee", "name": "Assignee", "format": "objects"}
        )
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        await repository.add_type_properties(
            "Item", (PropertyDraft(name="Assignee", format="objects"),)
        )
        yield repository
        await client.aclose()


class TestInMemoryTemplates(TemplateContract):
    @pytest.fixture
    def template_repo(self):
        return InMemoryGraphRepository(
            templates={"Item": FakeTemplate(default_fields={"status": "To Do"}, body=SCAFFOLD)}
        )


class TestAnytypeTemplates(TemplateContract):
    @pytest.fixture
    async def template_repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        # A human-authored select property + its options, and a template on the
        # Item type defaulting status -> To Do (with a scaffold body).
        status = await client.create_property(
            {"key": "status", "name": "status", "format": "select"}
        )
        to_do = await client.create_tag(status["id"], {"name": "To Do", "color": "ice"})
        await client.create_tag(status["id"], {"name": "In Progress", "color": "yellow"})
        mock.seed_template(
            "item", body=SCAFFOLD,
            default_properties=[{"key": "status", "format": "select", "select": to_do}],
        )
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        await repository.add_type_properties(
            "Item", (PropertyDraft(name="status", format="select"),)
        )
        yield repository
        await client.aclose()


class ScheduledEventContract:
    """ADR 027: Scheduled Event nodes round-trip identically on both
    backends -- infra-hidden from name search, schedule fields readable
    from the index, and partial field rewrites keep the rest."""

    async def _create(self, repo):
        from graph_context.domain import scheduling

        return await repo.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="tax reminder",
            summary="fires once at 2027-04-08 09:00",
            fields={
                scheduling.FIELD_SCHEDULE: "2027-04-08T09:00",
                scheduling.FIELD_PROMPT: "Remind Nick about taxes.",
                scheduling.FIELD_STATUS: scheduling.STATUS_PENDING,
                scheduling.FIELD_SESSION_KEY: "anytype:chat-1",
            },
        ))

    async def test_fields_round_trip_and_the_role_resolves(self, repo):
        from graph_context.domain import scheduling

        node = await self._create(repo)
        stored = repo.graph.node(node.id)
        assert stored.role is Role.SCHEDULED
        assert stored.fields[scheduling.FIELD_SCHEDULE] == "2027-04-08T09:00"
        assert stored.fields[scheduling.FIELD_PROMPT] == "Remind Nick about taxes."
        assert stored.fields[scheduling.FIELD_SESSION_KEY] == "anytype:chat-1"
        # The status is a SELECT on the Anytype backend: the write
        # auto-creates the option tag (ADR 012) and reads back as its
        # display name -- identical to the fake's verbatim round-trip.
        assert stored.fields[scheduling.FIELD_STATUS] == "Pending"

    async def test_status_select_transitions_round_trip(self, repo):
        from graph_context.domain import scheduling

        node = await self._create(repo)
        stored = repo.graph.node(node.id)
        await repo.update_node(node.id, fields={
            **dict(stored.fields),
            scheduling.FIELD_STATUS: scheduling.STATUS_CANCELLED,
        })
        after = repo.graph.node(node.id)
        assert after.fields[scheduling.FIELD_STATUS] == "Cancelled"

    async def test_a_bare_name_never_resolves_to_a_scheduled_event(self, repo):
        await self._create(repo)
        assert repo.graph.find_by_name("tax reminder") == []

    async def test_merged_field_update_keeps_the_other_fields(self, repo):
        from graph_context.domain import scheduling

        node = await self._create(repo)
        stored = repo.graph.node(node.id)
        merged = {**dict(stored.fields),
                  scheduling.FIELD_LAST_FIRED: "2027-04-08 09:00:30"}
        await repo.update_node(node.id, fields=merged)
        after = repo.graph.node(node.id)
        assert after.fields[scheduling.FIELD_LAST_FIRED] == "2027-04-08 09:00:30"
        assert after.fields[scheduling.FIELD_PROMPT] == "Remind Nick about taxes."
        assert after.fields[scheduling.FIELD_SCHEDULE] == "2027-04-08T09:00"


class TestInMemoryScheduledEvents(ScheduledEventContract):
    @pytest.fixture
    def repo(self):
        return InMemoryGraphRepository()


class TestAnytypeScheduledEvents(ScheduledEventContract):
    @pytest.fixture
    async def repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        yield repository
        await client.aclose()

    async def test_values_land_in_native_properties_not_the_blob(self, repo):
        """The whole point of the human-facing surface (ADR 027 amendment):
        a person opening the object in Anytype sees real, editable fields
        -- never a JSON side-channel (the blob is retired, ADR 028)."""
        from graph_context.domain import scheduling

        node = await self._create(repo)
        raw = await repo._client.get_object(node.id)
        properties = {
            entry["key"]: entry for entry in raw.get("properties", [])
        }
        assert properties[scheduling.FIELD_SCHEDULE]["text"] == "2027-04-08T09:00"
        assert properties[scheduling.FIELD_STATUS]["format"] == "select"
        assert "gc_fields" not in properties  # nothing fell through


class MetaInspectionContract:
    """ADR 045: the catalog surfaces hide infra types unless the caller's
    privilege includes the role, and a privileged mode-object write
    round-trips its ``gc_mode_*`` config through ``Node.fields``."""

    MODE_NAMES = {"activity mode", "activitymode"}

    async def test_known_node_types_admits_modes_only_with_privilege(
        self, repo
    ):
        assert not {
            t.lower() for t in repo.known_node_types()
        } & self.MODE_NAMES
        privileged = {
            t.lower()
            for t in repo.known_node_types(frozenset({Role.MODE}))
        }
        assert privileged & self.MODE_NAMES

    async def test_field_catalog_admits_the_mode_type_only_with_privilege(
        self, repo
    ):
        assert not {
            name.lower() for name in repo.field_catalog()
        } & self.MODE_NAMES
        privileged = {
            name.lower()
            for name in repo.field_catalog(frozenset({Role.MODE}))
        }
        assert privileged & self.MODE_NAMES

    async def test_mode_object_config_reflects_into_fields(self, repo):
        node = await repo.create_node(NodeDraft(
            type="Activity Mode", name="Recipe Mode",
            summary="Cooks recipes.",
            fields={"gc_mode_mutating": "true"},
        ))
        stored = repo.graph.node(node.id)
        assert stored.role is Role.MODE
        assert stored.fields["gc_mode_mutating"] == "true"


class TestInMemoryMetaInspection(MetaInspectionContract):
    @pytest.fixture
    def repo(self):
        # Catalog mode: the fake's field_catalog is keyed by its known
        # type names, which is where the privilege parameter bites (the
        # open-vocabulary mode has no catalog to filter at all).
        return InMemoryGraphRepository(field_catalog=[
            FieldSpec(name="Rank", format="text", key="rank"),
        ])


class TestAnytypeMetaInspection(MetaInspectionContract):
    @pytest.fixture
    async def repo(self):
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await seed_native_types(client)
        repository = AnytypeGraphRepository(client)
        await repository.hydrate()
        yield repository
        await client.aclose()
