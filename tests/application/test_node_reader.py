"""NodeReader (get_node) tests: edge grouping and WP7 provenance."""

from __future__ import annotations

from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationRecord
from graph_context.application.node_reader import NodeReader
from graph_context.application.node_writer import NodeWriter
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


async def test_get_node_groups_edges_both_directions(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    view = await NodeReader(repository, session).get_node(world.mira.id)
    types = set(view.edges)
    assert "participated_in" in types  # Mira -> Events
    assert "located_at" in types       # Mira -> Undercroft
    assert "possesses" in types        # Mira -> Ashbrand


async def test_get_node_edge_type_filter(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    view = await NodeReader(repository, session).get_node(
        world.mira.id, edge_type_filter=["possesses"]
    )
    assert set(view.edges) == {"possesses"}


class TestProvenance:
    """WP7: include_provenance mirrors include_prose; infra edges stay out
    of the edge groups (counts are the deliberate signal)."""

    async def _touched_world(self, repository, session):
        writer = NodeWriter(repository, session)
        mira = await writer.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.")
        )
        recorder = IntentRecorder(repository, now=lambda: "2026-07-04T01:00:00Z")
        intent = await recorder.record_turn(
            prompt="Add Mira to the world.",
            mutations=[MutationRecord(mira.id, "created")],
        )
        assert intent is not None
        return mira, intent

    async def test_include_provenance_returns_recent_intents_with_excerpts(
        self, repository, session
    ):
        mira, intent = await self._touched_world(repository, session)
        view = await NodeReader(repository, session).get_node(
            mira.id, include_provenance=1, excerpt_chars=40
        )
        assert view.provenance_count == 1
        (intent_node, excerpt), = view.provenance
        assert intent_node.id == intent.id
        assert excerpt.startswith("### gc:prompt")
        assert len(excerpt) <= 41  # 40 + ellipsis

    async def test_provenance_count_is_free_and_default_returns_none(
        self, repository, session
    ):
        mira, _ = await self._touched_world(repository, session)
        view = await NodeReader(repository, session).get_node(mira.id)
        assert view.provenance == ()
        assert view.provenance_count == 1

    async def test_infra_edges_never_reach_the_edge_groups(
        self, repository, session
    ):
        mira, _ = await self._touched_world(repository, session)
        recorder = ProseRecorder(repository, now=lambda: "t")
        await recorder.record(text="Ash.", summary="s", references=[mira.id])
        view = await NodeReader(repository, session).get_node(mira.id)
        # Neither the intent edge nor the prose references edge shows up.
        assert view.edges == {}
        assert view.provenance_count == 1
