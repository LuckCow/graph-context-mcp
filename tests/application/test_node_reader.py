"""NodeReader (get_node) tests, including WP3 include_prose."""

from __future__ import annotations

from itertools import count

from graph_context.application.node_reader import NodeReader
from graph_context.application.prose_recorder import ProseRecorder
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
