"""NodeReader (get_node) tests, including WP3 include_prose."""

from __future__ import annotations

from itertools import count

from graph_context.application.node_reader import NodeReader
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain.schema import EdgeType
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


async def test_get_node_groups_edges_both_directions(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    view = await NodeReader(repository, session).get_node(world.mira.id)
    types = set(view.edges)
    assert EdgeType.PARTICIPATED_IN in types  # Mira -> Events
    assert EdgeType.LOCATED_AT in types       # Mira -> Undercroft
    assert EdgeType.POSSESSES in types        # Mira -> Ashbrand


async def test_get_node_edge_type_filter(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    view = await NodeReader(repository, session).get_node(
        world.mira.id, edge_type_filter=[EdgeType.POSSESSES]
    )
    assert set(view.edges) == {EdgeType.POSSESSES}


async def test_include_prose_orders_most_recent_first_and_bounds_excerpt(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    # Deterministic generated_at stamps so ordering is testable.
    clock = count()
    recorder = ProseRecorder(
        repository, session, now=lambda: f"2026-01-01T00:00:{next(clock):02d}Z"
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
        repository, session, now=lambda: f"2026-01-01T00:00:{next(clock):02d}Z"
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
