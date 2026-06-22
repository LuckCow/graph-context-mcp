"""ProseRecorder tests (WP3): body assembly, truncation, references edges."""

from __future__ import annotations

from graph_context.application import prose_recorder as pr
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain.graph import Direction
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


async def test_record_creates_prose_node_with_references(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, session, now=lambda: "2026-01-01T00:00:00Z")
    node = await recorder.record(
        text="Ash over the Undercroft.", summary="Aftermath.",
        references=[world.mira.id, world.undercroft.id], model="demo",
    )
    assert node.type is NodeType.PROSE
    assert node.fields == {"model": "demo", "generated_at": "2026-01-01T00:00:00Z"}
    # references edges: Prose -> each source.
    targets = {
        e.target
        for e in repository.graph.edges(node.id, Direction.OUT, [EdgeType.REFERENCES])
    }
    assert targets == {world.mira.id, world.undercroft.id}


async def test_body_assembles_delimited_llm_sections(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, session, now=lambda: "t")
    node = await recorder.record(
        text="rendered", summary="s", references=[world.mira.id],
        llm_input="the prompt", llm_output="the completion",
    )
    body = await repository.fetch_body(node.id)
    assert body.startswith("rendered")
    assert pr.LLM_INPUT_HEADER in body and "the prompt" in body
    assert pr.LLM_OUTPUT_HEADER in body and "the completion" in body


async def test_oversized_body_is_truncated_with_marker(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, session, now=lambda: "t")
    huge = "z" * (pr.PROSE_BODY_CAP + 1000)
    node = await recorder.record(text=huge, summary="s", references=[world.mira.id])
    body = await repository.fetch_body(node.id)
    assert len(body) == pr.PROSE_BODY_CAP
    assert body.endswith(pr.TRUNCATION_MARKER)


async def test_title_defaults_to_first_line(
    repository: InMemoryGraphRepository, session: SessionState, world: World
) -> None:
    recorder = ProseRecorder(repository, session, now=lambda: "t")
    node = await recorder.record(
        text="The vaults were silent.\nThen the bells.", summary="s",
        references=[world.mira.id],
    )
    assert node.name == "The vaults were silent."
