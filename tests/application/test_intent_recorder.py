"""WP7: exactly one intent node per mutating turn; none for read-only."""

from __future__ import annotations

from graph_context.application.intent_recorder import (
    INTENT_BODY_CAP,
    PROMPT_WITHHELD,
    IntentRecorder,
    ToolTrace,
)
from graph_context.application.mutation_journal import MutationRecord
from graph_context.domain.models import NodeDraft
from graph_context.domain.schema import Role
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository


def _recorder(
    repository: InMemoryGraphRepository, **kwargs: bool
) -> IntentRecorder:
    return IntentRecorder(repository, now=lambda: "2026-07-04T00:00:00Z", **kwargs)


async def _world(repository: InMemoryGraphRepository) -> tuple[str, str]:
    mira = await repository.create_node(
        NodeDraft("Character", name="Mira", summary="Engineer.")
    )
    keep = await repository.create_node(
        NodeDraft("Location", name="Keep", summary="Vaults.")
    )
    return mira.id, keep.id


async def test_mutating_turn_yields_one_intent_node_with_edges_to_every_touch():
    repository = InMemoryGraphRepository()
    mira, keep = await _world(repository)
    node = await _recorder(repository).record_turn(
        prompt="Add Mira and link her to the Keep.",
        mutations=[MutationRecord(mira, "created"), MutationRecord(keep, "modified")],
        trace=[ToolTrace("create_node", 'type="Character" name="Mira"')],
        user_id="cli:local",
        model="scripted",
    )
    assert node is not None
    assert node.role is Role.INTENT
    assert node.name.startswith("Intent: Add Mira and link her to the Keep.")
    assert node.name.endswith("2026-07-04T00:00:00Z")
    assert node.fields["user_id"] == "cli:local"
    assert node.fields["model"] == "scripted"
    targets = {e.target for e in repository.graph.edges(node.id)}
    assert targets == {mira, keep}
    assert {e.type for e in repository.graph.edges(node.id)} == {"intent"}
    body = await repository.fetch_body(node.id)
    assert "Add Mira and link her to the Keep." in body
    assert "create_node" in body
    assert f"created: {mira}" in body and f"modified: {keep}" in body


async def test_read_only_turn_writes_nothing():
    repository = InMemoryGraphRepository()
    before = repository.graph.node_count()
    result = await _recorder(repository).record_turn(
        prompt="Who is Mira?", mutations=[],
    )
    assert result is None
    assert repository.graph.node_count() == before


async def test_body_cap_and_truncation_marker():
    repository = InMemoryGraphRepository()
    mira, _ = await _world(repository)
    node = await _recorder(repository).record_turn(
        prompt="x" * (INTENT_BODY_CAP * 2),
        mutations=[MutationRecord(mira, "modified")],
    )
    assert node is not None
    body = await repository.fetch_body(node.id)
    assert len(body) == INTENT_BODY_CAP
    assert body.endswith("[truncated]")


async def test_privacy_knob_withholds_the_prompt_but_keeps_the_trace():
    repository = InMemoryGraphRepository()
    mira, _ = await _world(repository)
    node = await _recorder(repository, store_prompt=False).record_turn(
        prompt="secret creative notes",
        mutations=[MutationRecord(mira, "modified")],
        trace=[ToolTrace("update_node", "summary=...")],
    )
    assert node is not None
    body = await repository.fetch_body(node.id)
    assert "secret creative notes" not in body
    assert PROMPT_WITHHELD in body
    assert "update_node" in body  # the trace stays usable


async def test_privacy_knob_also_scrubs_name_and_summary():
    """A withheld prompt must not leak through the node NAME either --
    names render in every list view."""
    repository = InMemoryGraphRepository()
    mira, _ = await _world(repository)
    node = await _recorder(repository, store_prompt=False).record_turn(
        prompt="secret creative notes",
        mutations=[MutationRecord(mira, "modified")],
    )
    assert node is not None
    assert "secret" not in node.name
    assert "secret" not in node.summary
