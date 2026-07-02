"""Body round-trips through the Anytype adapter (WP3 spike S6; A7 / ADR 010).

Body is supplied at creation as Markdown (A5), fetched on demand via
``fetch_body`` (never hydrated), and updated via the ``markdown`` PATCH key
(A7 -- wholesale replace; the ``body`` key is only valid on create). An
update that does not name the body must leave it untouched.
"""

from __future__ import annotations

import pytest

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.errors import NodeNotFound
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mock_server import MockAnytype
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


async def test_body_survives_an_update_that_does_not_name_it(
    repo: AnytypeGraphRepository,
) -> None:
    prose = await repo.create_node(
        NodeDraft("gc_prose", name="Scene", summary="s", body="original prose")
    )
    await repo.update_node(prose.id, summary="revised summary")
    assert await repo.fetch_body(prose.id) == "original prose"


async def test_fetch_body_falls_back_to_legacy_description_property(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    """A space that predates the ADR 010 migration still reads its
    descriptions: markdown wins when present, gc_description otherwise."""
    legacy_id = mock.seed_object(
        "location", "Old Keep",
        properties=[
            {"key": "gc_summary", "format": "text", "text": "s"},
            {"key": "gc_description", "format": "text",
             "text": "Pre-migration description."},
        ],
    )
    await repo.hydrate()
    assert await repo.fetch_body(legacy_id) == "Pre-migration description."
    # The body, once written, outranks the retired property.
    await client.update_object(legacy_id, {"markdown": "Migrated text."})
    assert await repo.fetch_body(legacy_id) == "Migrated text."


async def test_fetch_body_unknown_id_raises(repo: AnytypeGraphRepository) -> None:
    with pytest.raises(NodeNotFound):
        await repo.fetch_body("no-such-node")


async def test_empty_body_is_empty_string(repo: AnytypeGraphRepository) -> None:
    node = await repo.create_node(
        NodeDraft("Location", name="Plain", summary="no body here")
    )
    assert await repo.fetch_body(node.id) == ""


class TestA7BodyEditing:
    """Pin the A7 quirk (ADR 010) at the API level, mirroring the live server.

    Create writes the ``body`` key; update goes through the ``markdown`` key
    (wholesale replace, combinable with name/properties in one PATCH); a
    ``body`` key in PATCH is silently ignored -- the documented create/update
    field-name mismatch. ``markdown`` appears only on single-object GET.
    """

    async def test_markdown_patch_replaces_the_body(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        node = await repo.create_node(
            NodeDraft("Location", name="Keep", summary="s", body="v1")
        )
        await client.update_object(node.id, {"markdown": "v2"})
        assert (await client.get_object(node.id))["markdown"] == "v2"

    async def test_markdown_combines_with_name_and_properties_in_one_patch(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        node = await repo.create_node(
            NodeDraft("Location", name="Keep", summary="s", body="v1")
        )
        await client.update_object(node.id, {
            "name": "Keep II",
            "markdown": "v2",
            "properties": [{"key": "gc_summary", "format": "text", "text": "s2"}],
        })
        obj = await client.get_object(node.id)
        props = {p["key"]: p.get("text") for p in obj["properties"]}
        assert (obj["name"], obj["markdown"], props["gc_summary"]) == (
            "Keep II", "v2", "s2",
        )

    async def test_empty_markdown_clears_the_body(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        node = await repo.create_node(
            NodeDraft("Location", name="Keep", summary="s", body="v1")
        )
        await client.update_object(node.id, {"markdown": ""})
        assert (await client.get_object(node.id))["markdown"] == ""

    async def test_body_key_in_patch_is_silently_ignored(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        node = await repo.create_node(
            NodeDraft("Location", name="Keep", summary="s", body="v1")
        )
        await client.update_object(node.id, {"body": "clobber attempt"})
        assert (await client.get_object(node.id))["markdown"] == "v1"

    async def test_list_and_search_never_carry_markdown(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        await repo.create_node(
            NodeDraft("Location", name="Keep", summary="s", body="never hydrated")
        )
        async for obj in client.list_objects():
            assert "markdown" not in obj
        results = [obj async for obj in client.search()]
        assert results and all("markdown" not in obj for obj in results)
