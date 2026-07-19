"""SchemaProposals (WP33, ADR 041): draft, confirm across a turn, apply."""

import pytest

from graph_context.application.schema_proposals import (
    MAX_PENDING_PROPOSALS,
    SchemaProposals,
)
from graph_context.domain.models import PropertyDraft
from graph_context.errors import (
    GraphContextError,
    SchemaChangeConflict,
    UnknownNodeType,
)
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)

MOTTO = PropertyDraft(name="Motto", format="text")


@pytest.fixture
def repo():
    return InMemoryGraphRepository()


@pytest.fixture
def proposals():
    return SchemaProposals()


class TestProposing:
    def test_propose_type_stashes_a_rendered_draft(self, proposals, repo):
        proposal = proposals.propose_type(
            repo, "Faction", properties=(MOTTO,), reason="track allegiances"
        )
        assert proposal.id == "p1"
        assert proposals.pending() == (proposal,)
        summary = "\n".join(proposal.summary())
        assert "NEW TYPE 'Faction'" in summary
        assert "Motto (text)" in summary
        assert "track allegiances" in summary

    def test_propose_type_rejects_an_existing_type_name(self, proposals, repo):
        with pytest.raises(SchemaChangeConflict, match="already exists"):
            proposals.propose_type(repo, "Character")

    def test_propose_fields_needs_at_least_one_property(self, proposals, repo):
        with pytest.raises(GraphContextError, match="at least one"):
            proposals.propose_fields(repo, "Character", ())

    def test_propose_fields_rejects_an_unknown_type(self, proposals, repo):
        with pytest.raises(UnknownNodeType):
            proposals.propose_fields(repo, "NoSuchType", (MOTTO,))

    def test_pending_cap_forces_resolution(self, proposals, repo):
        for i in range(MAX_PENDING_PROPOSALS):
            proposals.propose_type(repo, f"Type{i}")
        with pytest.raises(GraphContextError, match="pending"):
            proposals.propose_type(repo, "Overflow")


class TestDraftedDrain:
    """ADR 041 v2: drafts ride to the transport as confirm events; the
    ledger's ``drafted`` list is turn-scoped like the WP23 outbox."""

    def test_propose_lands_in_drafted_and_drain_clears(self, proposals, repo):
        proposal = proposals.propose_type(repo, "Faction", properties=(MOTTO,))
        assert proposals.drain_drafted() == (proposal,)
        assert proposals.drain_drafted() == ()  # exactly once
        assert proposals.pending() == (proposal,)  # still awaiting the human

    def test_same_turn_cancel_retracts_the_draft(self, proposals, repo):
        proposal = proposals.propose_type(repo, "Faction")
        proposals.cancel(proposal.id)
        assert proposals.drain_drafted() == ()  # no confirm message posts

    def test_confirm_text_renders_the_exact_change(self, proposals, repo):
        proposal = proposals.propose_type(
            repo, "Faction", properties=(MOTTO,), reason="track allegiances"
        )
        text = proposal.confirm_text()
        assert f"Schema proposal {proposal.id}:" in text
        assert "NEW TYPE 'Faction'" in text
        assert "Motto (text)" in text
        assert "track allegiances" in text
        # Instruction-free: the transport appends its own (reactions on
        # the Anytype chat; a where-to-confirm note elsewhere).
        assert "React" not in text

    async def test_apply_executes_a_confirmed_draft(self, proposals, repo):
        # apply is HARNESS-only (the reaction handler); no gate here.
        proposal = proposals.propose_type(repo, "Faction", properties=(MOTTO,))
        applied, type_name = await proposals.apply(repo, proposal.id)
        assert applied is proposal
        assert type_name == "Faction"
        assert "Faction" in repo.known_node_types()
        assert proposals.pending() == ()


class TestApplyAndCancel:
    async def test_apply_extends_an_existing_type(self, proposals, repo):
        proposal = proposals.propose_fields(repo, "Character", (MOTTO,))
        _, type_name = await proposals.apply(repo, proposal.id)
        assert type_name == "Character"
        specs = repo.field_catalog().get("Character", ())
        assert any(s.name == "Motto" for s in specs)

    async def test_apply_unknown_id_lists_pending(self, proposals, repo):
        proposals.propose_type(repo, "Faction")
        with pytest.raises(GraphContextError, match="pending: p1"):
            await proposals.apply(repo, "p9")

    async def test_blank_id_resolves_a_sole_pending_proposal(
        self, proposals, repo
    ):
        proposals.propose_type(repo, "Faction")
        await proposals.apply(repo, "")
        assert "Faction" in repo.known_node_types()

    async def test_failed_apply_keeps_the_proposal(self, proposals, repo):
        # A conflict surfacing only at apply (the space changed since the
        # draft) must not eat the proposal: the model re-presents or cancels.
        proposal = proposals.propose_fields(
            repo, "Character", (PropertyDraft(name="Motto", format="text"),)
        )
        await repo.add_type_properties(
            "Character", (PropertyDraft(name="Motto", format="number"),)
        )
        with pytest.raises(SchemaChangeConflict):
            await proposals.apply(repo, proposal.id)
        assert proposals.pending() == (proposal,)

    def test_cancel_discards(self, proposals, repo):
        proposal = proposals.propose_type(repo, "Faction")
        assert proposals.cancel(proposal.id) is proposal
        assert proposals.pending() == ()
