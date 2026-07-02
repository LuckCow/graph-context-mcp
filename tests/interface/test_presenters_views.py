"""Golden-ish tests for the WP2/WP3 presenter additions.

``render_node_view`` and ``render_path`` are the formats the LLM reads, so
they double as review artifacts: assert the structural shape (grouped
edges with derived arrows, the path chain), not brittle whole-string
equality.
"""

from __future__ import annotations

from graph_context.application.node_reader import NodeReader
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain import pathfinding
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface import presenters
from tests.conftest import World


async def test_render_node_view_groups_edges_with_arrows(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    reader = NodeReader(repository, session)
    view = await reader.get_node(world.mira.id)
    out = presenters.render_node_view(view)

    assert out.splitlines()[0] == f"Mira (Character, id={world.mira.id})"
    assert "summary: Exiled siege engineer." in out
    assert "edges:" in out
    # Mira participated_in the siege -> outgoing arrow from Mira.
    assert f"participated_in -> Siege of Brakk (Event, id={world.siege.id})" in out
    # Mira located_at the Undercroft -> outgoing.
    assert f"located_at -> The Undercroft (Location, id={world.undercroft.id})" in out


async def test_render_node_view_marks_stale_summary(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    await repository.update_node(world.mira.id, summary_stale=True)
    reader = NodeReader(repository, session)
    out = presenters.render_node_view(await reader.get_node(world.mira.id))
    assert "[summary stale]" in out.splitlines()[0]


async def test_render_node_view_signals_no_prose_explicitly(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    reader = NodeReader(repository, session)
    out = presenters.render_node_view(await reader.get_node(world.mira.id))
    assert "prose: none recorded" in out


async def test_render_node_view_shows_prose_count_without_excerpts(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "t")
    await recorder.record(text="scene", summary="s", references=[world.mira.id])
    reader = NodeReader(repository, session)
    out = presenters.render_node_view(await reader.get_node(world.mira.id))
    assert "prose: 1 passage(s) reference this node (pass include_prose" in out


async def test_render_node_view_titles_excerpts_with_n_of_m(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "t")
    for i in range(3):
        await recorder.record(
            text=f"scene {i}", summary="s", references=[world.mira.id]
        )
    reader = NodeReader(repository, session)
    view = await reader.get_node(world.mira.id, include_prose=2)
    out = presenters.render_node_view(view)
    assert "prose (2 of 3):" in out


def test_render_path_renders_the_chain(
    repository: InMemoryGraphRepository, world: World
) -> None:
    path = pathfinding.find_path(repository.graph, world.siege.id, world.mira.id)
    out = presenters.render_path(path)
    assert out.startswith("Siege of Brakk (Event)")
    assert "Mira (Character)" in out
    assert "participated_in" in out


def test_render_path_none_is_actionable() -> None:
    out = presenters.render_path(None)
    assert "No path found" in out
    assert "max_length" in out
