"""Body (Prose text) round-trip through the Anytype adapter (WP3 / spike S6).

Body is supplied at creation as Markdown (A5), fetched on demand via
``fetch_body`` (never hydrated), and is write-once: a later update must NOT
disturb it (A6 / S6 -- the live server silently ignores a body in PATCH,
mirrored by the mock).
"""

from __future__ import annotations

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.errors import NodeNotFound
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository


async def test_body_round_trips_via_fetch_body(repo: AnytypeGraphRepository) -> None:
    place = await repo.create_node(
        NodeDraft("Location", name="The Undercroft", summary="Vaults.")
    )
    prose = await repo.create_node(
        NodeDraft(
            "gc_prose", name="Scene", summary="Aftermath.",
            body="Ash drifted over the Undercroft.",
        ),
        links=[LinkSpec("references", other=place.id)],
    )
    assert await repo.fetch_body(prose.id) == "Ash drifted over the Undercroft."


async def test_body_is_not_in_the_index(repo: AnytypeGraphRepository) -> None:
    prose = await repo.create_node(
        NodeDraft("gc_prose", name="Scene", summary="s", body="secret text")
    )
    # The indexed node carries no body attribute; bodies are fetch-only.
    assert not hasattr(repo.graph.node(prose.id), "body")


async def test_body_survives_a_later_update(repo: AnytypeGraphRepository) -> None:
    prose = await repo.create_node(
        NodeDraft("gc_prose", name="Scene", summary="s", body="original prose")
    )
    await repo.update_node(prose.id, summary="revised summary")
    assert await repo.fetch_body(prose.id) == "original prose"  # write-once (A6/S6)


async def test_fetch_body_unknown_id_raises(repo: AnytypeGraphRepository) -> None:
    with pytest.raises(NodeNotFound):
        await repo.fetch_body("no-such-node")


async def test_empty_body_is_empty_string(repo: AnytypeGraphRepository) -> None:
    node = await repo.create_node(
        NodeDraft("Location", name="Plain", summary="no body here")
    )
    assert await repo.fetch_body(node.id) == ""
