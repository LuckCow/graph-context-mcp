"""The GraphRepository contract: one behavioral spec, every implementation.

Each concrete repository inherits the contract class and provides a
``repo`` fixture. A behavior that cannot be expressed by ALL
implementations means the port is wrong -- fix the port, not the adapter.

When live-server access exists, add a third subclass gated behind
``ANYTYPE_E2E=1`` pointing the same tests at a real space.
"""

import asyncio

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.query import NodeQuery, Op, Predicate, run_query
from graph_context.domain.schema import Role
from graph_context.errors import NodeNotFound
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
        node = await repo.create_node(
            NodeDraft("Technology", name="Ashforge", summary="A forge.",
                      fields={"fuel": "bonemeal"})
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
                      fields={"done": "true"})
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
