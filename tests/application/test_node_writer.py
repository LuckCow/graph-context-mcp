"""NodeWriter: composite writes, validation, and the staleness rule."""

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.schema import Role
from graph_context.errors import (
    InfraWriteDenied,
    NodeNotFound,
    SchemaViolation,
)
from tests.conftest import World


class TestCreateNode:
    async def test_composite_create_writes_node_and_links(self, writer, repository, world: World):
        faction = (await writer.create_node(
            NodeDraft("Organization", name="Emberguard", summary="Brakk's last defenders."),
            links=[LinkSpec("member_of", other=world.mira.id)],
        )).node
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

    async def test_created_node_is_the_most_recently_touched(
        self, writer, session, world: World
    ):
        node = (await writer.create_node(
            NodeDraft("Location", name="Brakk Gate", summary="The city gate.")
        )).node
        assert session.recent.items[0] == node.id
        assert session.working_set.entries == ()  # holds are explicit only


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


class TestTypeScopedDeclarations:
    """ADR 042: a scope="type" declaration drafts an EXTEND_TYPE proposal
    into the session ledger AFTER the write lands; drafting failures
    degrade to warnings, never unwind the write."""

    def _writer_with_ledger(self):
        from graph_context.application.node_writer import NodeWriter
        from graph_context.application.schema_proposals import SchemaProposals
        from graph_context.domain.session import SessionState
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )

        repository = InMemoryGraphRepository()
        proposals = SchemaProposals()
        writer = NodeWriter(
            repository, SessionState(), proposals=proposals
        )
        return writer, repository, proposals

    async def test_type_scope_writes_the_value_and_drafts(self) -> None:
        from graph_context.domain.models import PropertyDeclaration

        writer, repository, proposals = self._writer_with_ledger()
        outcome = await writer.create_node(
            NodeDraft("Character", name="Gerald", summary="A cook.",
                      fields={"shift_active": "true"}),
            declarations={
                "shift_active": PropertyDeclaration(
                    "shift_active", "checkbox", scope="type"
                )
            },
        )
        node = repository.graph.node(outcome.node.id)
        assert node.fields["shift_active"] == "true"  # value is durable
        assert len(outcome.drafted) == 1
        proposal = outcome.drafted[0]
        assert proposal.type_name == "Character"
        assert proposal.properties[0].name == "Shift Active"
        # The draft rides the SHARED ledger, so the pipeline's
        # drain_drafted turns it into a confirm event.
        assert proposals.drain_drafted() == (proposal,)

    async def test_instance_scope_drafts_nothing(self) -> None:
        from graph_context.domain.models import PropertyDeclaration

        writer, _, proposals = self._writer_with_ledger()
        outcome = await writer.create_node(
            NodeDraft("Character", name="Gerald", summary="A cook.",
                      fields={"quirk": "hums"}),
            declarations={"quirk": PropertyDeclaration("quirk", "text")},
        )
        assert outcome.drafted == () and outcome.warnings == ()
        assert proposals.drain_drafted() == ()

    async def test_ledger_cap_degrades_to_a_warning(self) -> None:
        from graph_context.domain.models import PropertyDeclaration, PropertyDraft

        writer, repository, proposals = self._writer_with_ledger()
        for n in range(5):  # fill the ledger to MAX_PENDING_PROPOSALS
            proposals.propose_fields(
                repository, "Character",
                (PropertyDraft(name=f"Filler {n}", format="text"),),
            )
        outcome = await writer.create_node(
            NodeDraft("Character", name="Gerald", summary="A cook.",
                      fields={"shift_active": "true"}),
            declarations={
                "shift_active": PropertyDeclaration(
                    "shift_active", "checkbox", scope="type"
                )
            },
        )
        # The write landed; the drafting failure became a warning.
        assert repository.graph.node(outcome.node.id).fields["shift_active"] == "true"
        assert outcome.drafted == ()
        assert any("saved" in w for w in outcome.warnings)

    async def test_no_ledger_degrades_to_a_warning(self, writer) -> None:
        from graph_context.domain.models import PropertyDeclaration

        outcome = await writer.create_node(
            NodeDraft("Character", name="Gerald", summary="A cook.",
                      fields={"shift_active": "true"}),
            declarations={
                "shift_active": PropertyDeclaration(
                    "shift_active", "checkbox", scope="type"
                )
            },
        )
        assert outcome.drafted == ()
        assert any("schema tool" in w for w in outcome.warnings)

    async def test_declaration_without_a_value_is_rejected(self, writer) -> None:
        from graph_context.domain.models import PropertyDeclaration

        with pytest.raises(SchemaViolation, match="no value"):
            await writer.create_node(
                NodeDraft("Character", name="Gerald", summary="A cook."),
                declarations={
                    "quirk": PropertyDeclaration("quirk", "text")
                },
            )


class TestInfraWriteGuard:
    """ADR 045: generic writes may not target infra-role objects unless
    the caller's privilege admits the role. The dedicated services
    (scheduler, rule engine, recorders) bypass NodeWriter entirely."""

    MODE_DRAFT = NodeDraft(
        "Activity Mode", name="Recipe Mode", summary="Cooks recipes."
    )

    async def test_unprivileged_create_of_a_mode_object_is_denied(
        self, writer, repository
    ):
        before = repository.graph.node_count()
        with pytest.raises(InfraWriteDenied, match="system configuration"):
            await writer.create_node(self.MODE_DRAFT)
        assert repository.graph.node_count() == before

    async def test_admitted_create_lands_with_the_mode_role(
        self, writer, repository
    ):
        node = (await writer.create_node(
            self.MODE_DRAFT, admitted_infra_roles=frozenset({Role.MODE}),
        )).node
        assert repository.graph.node(node.id).role is Role.MODE

    async def test_update_of_a_mode_object_needs_the_same_privilege(
        self, writer, repository
    ):
        node = (await writer.create_node(
            self.MODE_DRAFT, admitted_infra_roles=frozenset({Role.MODE}),
        )).node
        with pytest.raises(InfraWriteDenied):
            await writer.update_node(node.id, summary="tweaked")
        updated = (await writer.update_node(
            node.id, summary="tweaked",
            admitted_infra_roles=frozenset({Role.MODE}),
        )).node
        assert updated.summary == "tweaked"

    async def test_mode_privilege_does_not_unlock_other_infra(self, writer):
        with pytest.raises(InfraWriteDenied):
            await writer.create_node(
                NodeDraft("Scheduled Event", name="Ping", summary="s."),
                admitted_infra_roles=frozenset({Role.MODE}),
            )
