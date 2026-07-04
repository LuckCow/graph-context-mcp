"""ProseRecorder tests (WP3): body assembly, truncation, references edges."""

from __future__ import annotations

from graph_context.application import prose_recorder as pr
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain.graph import Direction
from graph_context.domain.schema import Role
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


async def test_record_creates_prose_node_with_references(
    repository: InMemoryGraphRepository, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "2026-01-01T00:00:00Z")
    node = await recorder.record(
        text="Ash over the Undercroft.", summary="Aftermath.",
        references=[world.mira.id, world.undercroft.id],
    )
    assert node.role is Role.PROSE
    assert node.fields == {"generated_at": "2026-01-01T00:00:00Z"}
    # references edges: Prose -> each source.
    targets = {
        e.target
        for e in repository.graph.edges(node.id, Direction.OUT, ["references"])
    }
    assert targets == {world.mira.id, world.undercroft.id}


async def test_record_does_not_touch_the_focus_stack(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    # Prose is an infra role hidden from traversal; recording it must not
    # push it onto the focus stack or into recent history.
    session.touch(world.mira.id)
    focus_before = list(session.focus.entries)
    recent_before = list(session.recent.items)
    recorder = ProseRecorder(repository, now=lambda: "t")
    await recorder.record(text="rendered", summary="s", references=[world.mira.id])
    assert list(session.focus.entries) == focus_before
    assert list(session.recent.items) == recent_before


async def test_body_is_the_rendered_text_alone(
    repository: InMemoryGraphRepository, world: World
) -> None:
    """WP7 retired the llm_* body sections: generation provenance lives on
    intent nodes (ADR 008); prose bodies carry only the text itself."""
    recorder = ProseRecorder(repository, now=lambda: "t")
    node = await recorder.record(
        text="rendered", summary="s", references=[world.mira.id],
    )
    assert await repository.fetch_body(node.id) == "rendered"


async def test_oversized_body_is_truncated_with_marker(
    repository: InMemoryGraphRepository, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "t")
    huge = "z" * (pr.PROSE_BODY_CAP + 1000)
    node = await recorder.record(text=huge, summary="s", references=[world.mira.id])
    body = await repository.fetch_body(node.id)
    assert len(body) == pr.PROSE_BODY_CAP
    assert body.endswith(pr.TRUNCATION_MARKER)


async def test_title_defaults_to_first_line(
    repository: InMemoryGraphRepository, world: World
) -> None:
    recorder = ProseRecorder(repository, now=lambda: "t")
    node = await recorder.record(
        text="The vaults were silent.\nThen the bells.", summary="s",
        references=[world.mira.id],
    )
    assert node.name == "The vaults were silent."
