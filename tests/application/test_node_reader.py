"""NodeReader (get_node) tests, including WP3 include_prose."""

from __future__ import annotations

from itertools import count

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


async def test_include_prose_orders_most_recent_first_and_bounds_excerpt(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    # Deterministic generated_at stamps so ordering is testable.
    clock = count()
    recorder = ProseRecorder(
        repository, now=lambda: f"2026-01-01T00:00:{next(clock):02d}Z"
    )
    older = await recorder.record(
        text="O" * 50, summary="older", references=[world.undercroft.id], title="older"
    )
    newer = await recorder.record(
        text="N" * 50, summary="newer", references=[world.undercroft.id], title="newer"
    )

    view = await NodeReader(repository, session).get_node(
        world.undercroft.id, include_prose=5, excerpt_chars=10
    )
    assert [p.id for p, _ in view.prose] == [newer.id, older.id]  # most recent first
    # excerpt is capped and ellipsis-marked when the body is longer
    _, excerpt = view.prose[0]
    assert excerpt == "N" * 10 + "…"


async def test_include_prose_respects_limit(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    clock = count()
    recorder = ProseRecorder(
        repository, now=lambda: f"2026-01-01T00:00:{next(clock):02d}Z"
    )
    for i in range(3):
        await recorder.record(
            text=f"scene {i}", summary="s", references=[world.undercroft.id]
        )
    view = await NodeReader(repository, session).get_node(
        world.undercroft.id, include_prose=2
    )
    assert len(view.prose) == 2


async def test_include_prose_zero_fetches_nothing(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    view = await NodeReader(repository, session).get_node(world.undercroft.id)
    assert view.prose == ()


async def test_prose_count_populated_without_include_prose(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "t")
    for i in range(2):
        await recorder.record(
            text=f"scene {i}", summary="s", references=[world.undercroft.id]
        )
    fetches = 0
    original_fetch_body = repository.fetch_body

    async def counting_fetch_body(node_id: str) -> str:
        nonlocal fetches
        fetches += 1
        return await original_fetch_body(node_id)

    repository.fetch_body = counting_fetch_body  # type: ignore[method-assign]
    view = await NodeReader(repository, session).get_node(world.undercroft.id)
    assert view.prose_count == 2
    assert view.prose == ()  # count is index-only ...
    assert fetches == 1  # ... costing only the node's own body fetch (ADR 010)


async def test_prose_count_excludes_story_node_references(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    # A human-created `references` relation between story nodes is not prose.
    from graph_context.domain.models import LinkSpec

    await repository.add_link(
        world.siege.id,
        LinkSpec("references", other=world.undercroft.id),
        create_missing_relations=True,
    )
    view = await NodeReader(repository, session).get_node(world.undercroft.id)
    assert view.prose_count == 0


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
        assert view.prose_count == 1
