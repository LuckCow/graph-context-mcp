"""Golden-ish tests for the WP2/WP3 presenter additions.

``render_node_view`` and ``render_path`` are the formats the LLM reads, so
they double as review artifacts: assert the structural shape (grouped
edges with derived arrows, the path chain), not brittle whole-string
equality.
"""

from __future__ import annotations

from graph_context.application.node_reader import NodeReader
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
