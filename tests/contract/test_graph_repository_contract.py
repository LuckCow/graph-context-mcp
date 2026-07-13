"""The GraphRepository contract: one behavioral spec, every implementation.

Each concrete repository inherits the contract class and provides a
``repo`` fixture. A behavior that cannot be expressed by ALL
implementations means the port is wrong -- fix the port, not the adapter.

When live-server access exists, add a third subclass gated behind
``ANYTYPE_E2E=1`` pointing the same tests at a real space.
"""

import asyncio

import pytest

from graph_context.domain import attribution
from graph_context.domain.models import FieldSpec, LinkSpec, NodeDraft
from graph_context.domain.query import NodeQuery, Op, Predicate, run_query
from graph_context.domain.schema import Role
from graph_context.errors import NodeNotFound, UnknownFieldKey
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

    async def test_composite_create_writes_outgoing_and_incoming_links(self, repo):
        mira = await repo.create_node(CHAR)
        place = await repo.create_node(
            PLACE, links=[LinkSpec("located_at", other=mira.id, outgoing=False)]
        )
        # incoming: mira -located_at-> place
        assert {n.id for _, n in repo.graph.neighbors(mira.id)} == {place.id}
        sword = await repo.create_node(
            NodeDraft("Item", name="Ashbrand", summary="A blade."),
        )
        faction = await repo.create_node(
            NodeDraft("Organization", name="Emberguard", summary="Defenders."),
            links=[LinkSpec("possesses", other=sword.id, outgoing=True)],
        )
        assert {n.id for _, n in repo.graph.neighbors(faction.id)} == {sword.id}

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

    async def test_fields_round_trip(self, repo):
        # "fuel" is not a property anywhere; the declaration mints it as a
        # real one (ADR 023). In the live run this exercises scalar
        # create_property for real; reruns reuse the surviving property.
        node = await repo.create_node(
            NodeDraft("Technology", name="Ashforge", summary="A forge.",
                      fields={"fuel": "bonemeal"}),
            create_missing_fields={"fuel": "text"},
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
            create_missing_fields={"done": "checkbox"},
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
    """ADR 023: story-node ``fields`` keys resolve against the space's
    scalar properties identically in every implementation. Seeded with
    "Due date" (date), "Status" (select: To Do, In Progress), and an
    "Assignee" objects-format RELATION (an edge, never a fields key)."""

    async def test_relation_named_as_a_field_redirects_to_links(
        self, catalog_repo
    ):
        """Live-caught: a model wrote fields={'Assignee': ...} because the
        relation is invisible in the scalar catalog; the error must point
        at links, not at minting a shadowing scalar property."""
        with pytest.raises(UnknownFieldKey) as err:
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Assignee": "Nick"})
            )
        message = str(err.value)
        assert "RELATION" in message and "'edge_type'" in message
        assert "create_missing_fields" not in message

    async def test_a_relation_key_cannot_be_shadowed_by_declaration(
        self, catalog_repo
    ):
        """create_missing_fields must not mint a scalar over a relation."""
        with pytest.raises(UnknownFieldKey):
            await catalog_repo.create_node(
                NodeDraft("Item", name="Ship it", summary="s.",
                          fields={"Assignee": "Nick"}),
                create_missing_fields={"Assignee": "text"},
            )

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
        assert "create_missing_fields" in message

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
        assert catalog_repo.graph.node(node.id).fields["due_date"] == "2026-08-01"

    async def test_declared_key_mints_a_reusable_property(self, catalog_repo):
        first = await catalog_repo.create_node(
            NodeDraft("Item", name="Ship it", summary="s.",
                      fields={"effort": "3"}),
            create_missing_fields={"effort": "number"},
        )
        assert first.fields["effort"] == "3"
        # Now part of the space's vocabulary: reusable without the opt-in.
        second = await catalog_repo.create_node(
            NodeDraft("Item", name="Land it", summary="s.",
                      fields={"effort": "5"})
        )
        assert second.fields["effort"] == "5"

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
        return InMemoryGraphRepository(field_catalog=[
            FieldSpec(name="Due date", format="date", key="due_date"),
            FieldSpec(name="Status", format="select", key="status",
                      options=("To Do", "In Progress")),
            FieldSpec(name="Assignee", format="objects", key="assignee"),
        ])


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
