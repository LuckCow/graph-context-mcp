"""Native scalar property reflection into Node.fields (ADR 012 read side).

Adapter-read behavior like the links-mirror handling: the in-memory fake
has an open `fields` dict and no native-property concept, so these live
here rather than the contract suite.
"""

from __future__ import annotations

from typing import Any

from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from tests.anytype.conftest import seed_native_types


def _entry(key: str, fmt: str, value: Any) -> dict[str, Any]:
    return {"key": key, "format": fmt, fmt: value}


TAG_HERO = {"object": "tag", "id": "t-1", "key": "hero", "name": "Hero", "color": "red"}
TAG_DARK = {"object": "tag", "id": "t-2", "key": "dark", "name": "Dark", "color": "grey"}
TAG_HOPE = {"object": "tag", "id": "t-3", "key": "hope", "name": "Hopeful", "color": "lime"}


async def _seed_scalar_properties(client: AnytypeClient) -> None:
    for key, name, fmt in [
        ("role", "Role", "select"),
        ("themes", "Themes", "multi_select"),
        ("notes", "Notes", "text"),
        ("event_date", "Event date", "date"),
        ("verified", "Verified", "checkbox"),
        ("wordcount", "Word count", "number"),
    ]:
        await client.create_property({"key": key, "name": name, "format": fmt})


async def test_native_scalars_reflect_into_fields(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    await _seed_scalar_properties(client)
    node_id = mock.seed_object("character", "Autumn", properties=[
        _entry("role", "select", TAG_HERO),
        _entry("themes", "multi_select", [TAG_DARK, TAG_HOPE]),
        _entry("notes", "text", "warehouse arc"),
        _entry("event_date", "date", "2026-07-02T00:00:00Z"),
        _entry("verified", "checkbox", True),
        _entry("wordcount", "number", 1200.0),
    ])
    await repo.hydrate()
    assert repo.graph.node(node_id).fields == {
        "role": "Hero",
        "themes": "Dark, Hopeful",
        "notes": "warehouse arc",
        "event_date": "2026-07-02T00:00:00Z",
        "verified": "true",
        "wordcount": "1200",
    }


async def test_noise_never_reaches_fields(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    """The spurious-property filter: system timestamps, gc_ keys, the
    built-in description (summary channel), edges, empties, and unticked
    checkboxes all stay out of the LLM's context."""
    await _seed_scalar_properties(client)
    other = mock.seed_object("character", "Other")
    node_id = mock.seed_object("character", "Autumn", properties=[
        _entry("added_date", "date", "2026-01-01T00:00:00Z"),      # system
        _entry("last_opened_date", "date", "2026-01-01T00:00:00Z"),  # system
        _entry("description", "text", "One-liner."),              # summary channel
        _entry("gc_story_time", "number", 10),                     # first-class
        _entry("gc_description", "text", "legacy"),                # retired
        _entry("boss", "objects", [other]),                        # an edge
        _entry("notes", "text", ""),                               # empty
        _entry("verified", "checkbox", False),                     # unticked
    ])
    await repo.hydrate()
    node = repo.graph.node(node_id)
    assert node.fields == {}
    assert node.summary == "One-liner."       # went where it belongs
    assert node.story_time == 10              # first-class, not a field


async def test_gc_field_denylist_silences_space_specific_noise(
    mock: MockAnytype,
) -> None:
    config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
    client = AnytypeClient(config, transport=mock.transport)
    await ensure_schema(client)
    await seed_native_types(client)
    await client.create_property({"key": "notes", "name": "Notes", "format": "text"})
    node_id = mock.seed_object("character", "Autumn", properties=[
        _entry("notes", "text", "noisy import artifact"),
    ])
    repo = AnytypeGraphRepository(client, field_denylist=("notes",))
    await repo.hydrate()
    assert repo.graph.node(node_id).fields == {}
    await client.aclose()


async def test_native_wins_over_the_gc_fields_blob(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    await _seed_scalar_properties(client)
    node_id = mock.seed_object("character", "Autumn", properties=[
        _entry("role", "select", TAG_HERO),
        _entry("gc_fields", "text", '{"role": "stale blob value", "quirk": "hums"}'),
    ])
    await repo.hydrate()
    assert repo.graph.node(node_id).fields == {
        "role": "Hero",   # native outranks the blob
        "quirk": "hums",  # blob remains the channel for unmatched keys
    }


async def test_human_select_edit_reaches_fields_on_resync(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    await _seed_scalar_properties(client)
    node = await repo.create_node(
        NodeDraft("Character", name="Autumn", summary="Worker.")
    )
    mock.edit_object_directly(
        node.id, set_property=_entry("role", "select", TAG_HERO)
    )
    changed = await repo.resync()
    assert node.id in changed
    assert repo.graph.node(node.id).fields["role"] == "Hero"
