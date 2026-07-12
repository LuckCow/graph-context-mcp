"""Native scalar property reflection into Node.fields (ADR 012 read side).

Adapter-read behavior like the links-mirror handling: the in-memory fake
has an open `fields` dict and no native-property concept, so these live
here rather than the contract suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph_context.domain import attribution
from graph_context.domain.models import NodeDraft
from graph_context.errors import GraphContextError, UnknownFieldKey
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


async def test_stray_gc_fields_blob_is_never_read(
    repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
) -> None:
    """ADR 028 retired the gc_fields JSON blob: a stray one on a pre-pivot
    object is just another unreflected gc_ property, not a fields channel."""
    await _seed_scalar_properties(client)
    node_id = mock.seed_object("character", "Autumn", properties=[
        _entry("role", "select", TAG_HERO),
        _entry("gc_fields", "text", '{"role": "stale blob value", "quirk": "hums"}'),
    ])
    await repo.hydrate()
    assert repo.graph.node(node_id).fields == {"role": "Hero"}


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


class TestFieldWriteRouting:
    """ADR 012/023/028 write side: `fields` keys matching native properties
    write those properties (select values resolved-or-created as tags
    BEFORE the object write); every write is native-only -- an unmatched
    key errors unless declared via create_missing_fields, and no gc_fields
    blob is written at all."""

    async def _seed_role_property(self, client: AnytypeClient) -> str:
        prop = await client.create_property(
            {"key": "role", "name": "Role", "format": "select"}
        )
        await client.create_tag(prop["id"], {"name": "Everyperson", "color": "blue"})
        return str(prop["id"])

    async def test_create_writes_natives_and_no_blob(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await self._seed_role_property(client)
        await client.create_property({"key": "notes", "name": "Notes", "format": "text"})
        await repo.hydrate()  # registry must know the new properties
        node = await repo.create_node(NodeDraft(
            "Character", name="Autumn", summary="Worker.",
            fields={"role": "everyperson", "notes": "arc 1"},
        ))
        stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
        assert stored["role"]["select"]["name"] == "Everyperson"  # matched by name
        assert stored["notes"]["text"] == "arc 1"
        assert "gc_fields" not in stored  # story nodes carry no blob (ADR 023)
        assert node.fields == {"role": "Everyperson", "notes": "arc 1"}

    async def test_unmatched_key_errors_listing_reusable_properties(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        await self._seed_role_property(client)
        await repo.hydrate()
        with pytest.raises(UnknownFieldKey) as err:
            await repo.create_node(NodeDraft(
                "Character", name="Autumn", summary="Worker.",
                fields={"quirk": "hums"},
            ))
        message = str(err.value)
        assert "'quirk'" in message
        assert "Role (select)" in message
        assert "create_missing_fields" in message
        assert "date" in message  # the format menu is echoed

    async def test_error_lists_type_properties_with_select_options(
        self, mock: MockAnytype
    ) -> None:
        """A type created WITH inline properties (the live GET /types shape)
        surfaces them first in the error, options included."""
        config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
        client = AnytypeClient(config, transport=mock.transport)
        await ensure_schema(client)
        await client.create_type({
            "key": "task", "name": "Task", "plural_name": "Tasks",
            "layout": "action",
            "properties": [
                {"key": "due_date", "name": "Due date", "format": "date"},
                {"key": "status", "name": "Status", "format": "select"},
            ],
        })
        repo = AnytypeGraphRepository(client)
        await repo.hydrate()
        status_id = next(
            p["id"] for p in [p async for p in client.list_properties()]
            if p["key"] == "status"
        )
        await client.create_tag(status_id, {"name": "To Do", "color": "grey"})
        with pytest.raises(UnknownFieldKey) as err:
            await repo.create_node(NodeDraft(
                "Task", name="Ship it", summary="A task.", fields={"due": "2026-08-01"},
            ))
        message = str(err.value)
        assert "Properties on Task: Due date (date), Status (select: To Do)" in message
        await client.aclose()

    async def test_create_missing_fields_mints_a_reusable_native_property(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await repo.hydrate()
        node = await repo.create_node(
            NodeDraft(
                "Character", name="Autumn", summary="Worker.",
                fields={"quirk": "hums"},
            ),
            create_missing_fields={"quirk": "text"},
        )
        stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
        assert stored["quirk"]["text"] == "hums"
        assert "gc_fields" not in stored
        # The property now exists in the space: the next write reuses it
        # without the opt-in.
        second = await repo.create_node(NodeDraft(
            "Character", name="Renata", summary="Exec.", fields={"quirk": "whistles"},
        ))
        assert second.fields["quirk"] == "whistles"

    async def test_update_writes_natives_and_ignores_stray_blob(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await self._seed_role_property(client)
        # A pre-pivot object with stray blob data, edited by the bot now.
        node_id = mock.seed_object("character", "Autumn", properties=[
            _entry("gc_fields", "text", '{"quirk": "hums"}'),
        ])
        await repo.hydrate()
        updated = await repo.update_node(node_id, fields={"role": "Everyperson"})
        stored = {p["key"]: p for p in mock.object(node_id)["properties"]}
        assert stored["role"]["select"]["name"] == "Everyperson"
        assert stored["gc_fields"]["text"] == '{"quirk": "hums"}'  # untouched
        # No read merge (ADR 028): stray blob data never reaches fields.
        assert updated.fields == {"role": "Everyperson"}

    async def test_update_unmatched_key_errors_before_any_write(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await repo.hydrate()
        node = await repo.create_node(
            NodeDraft("Character", name="Autumn", summary="Worker.")
        )
        with pytest.raises(UnknownFieldKey):
            await repo.update_node(node.id, fields={"tic": "taps twice"})
        stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
        assert "gc_fields" not in stored

    async def test_infra_nodes_write_native_attribution_properties(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        """Recorders (gc_prose / gc_intent) stamp provenance into the
        bootstrap-minted attribution properties (ADR 028) -- real,
        human-visible fields; no gc_fields blob exists anymore."""
        node = await repo.create_node(NodeDraft(
            "gc_prose", name="Scene 1", summary="A captured scene.",
            fields={
                attribution.FIELD_USER_ID: "u-1",
                attribution.FIELD_GENERATED_AT: "2026-07-10",
            },
        ))
        stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
        assert stored[attribution.FIELD_USER_ID]["text"] == "u-1"
        assert stored[attribution.FIELD_GENERATED_AT]["text"] == "2026-07-10"
        assert "gc_fields" not in stored
        # And they reflect back into Node.fields for consumers like the
        # provenance sort in NodeReader.
        assert node.fields[attribution.FIELD_GENERATED_AT] == "2026-07-10"

    async def test_infra_unmatched_key_errors_like_any_other(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        """ADR 028 removed the infra fall-through: one native-only rule for
        every role."""
        with pytest.raises(UnknownFieldKey):
            await repo.create_node(NodeDraft(
                "gc_prose", name="Scene 1", summary="A captured scene.",
                fields={"free_form_bookkeeping": "nope"},
            ))

    async def test_unknown_select_value_creates_the_tag(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        prop_id = await self._seed_role_property(client)
        await repo.hydrate()
        node = await repo.create_node(NodeDraft(
            "Character", name="Renata", summary="Exec.", fields={"role": "Antagonist"},
        ))
        assert node.fields["role"] == "Antagonist"
        tags = [t async for t in client.list_tags(prop_id)]
        assert "Antagonist" in {t["name"] for t in tags}

    async def test_multi_select_splits_on_commas(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await client.create_property(
            {"key": "themes", "name": "Themes", "format": "multi_select"}
        )
        await repo.hydrate()
        node = await repo.create_node(NodeDraft(
            "Character", name="Autumn", summary="Worker.",
            fields={"themes": "Dark, Hopeful"},
        ))
        assert node.fields["themes"] == "Dark, Hopeful"

    async def test_bad_number_and_checkbox_values_error_actionably(
        self, repo: AnytypeGraphRepository, client: AnytypeClient
    ) -> None:
        await client.create_property(
            {"key": "wordcount", "name": "Word count", "format": "number"}
        )
        await client.create_property(
            {"key": "verified", "name": "Verified", "format": "checkbox"}
        )
        await repo.hydrate()
        with pytest.raises(GraphContextError, match="number"):
            await repo.create_node(NodeDraft(
                "Character", name="A", summary="s", fields={"wordcount": "lots"},
            ))
        with pytest.raises(GraphContextError, match="true"):
            await repo.create_node(NodeDraft(
                "Character", name="B", summary="s", fields={"verified": "maybe"},
            ))

    async def test_field_matches_by_display_name_too(
        self, repo: AnytypeGraphRepository, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        await client.create_property(
            {"key": "real_life_inspiration", "name": "Real life inspiration",
             "format": "text"}
        )
        await repo.hydrate()
        node = await repo.create_node(NodeDraft(
            "Character", name="Mary", summary="Marketer.",
            fields={"Real life inspiration": "rental family services"},
        ))
        stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
        assert stored["real_life_inspiration"]["text"] == "rental family services"


async def test_fresh_tag_settle_window_is_retried() -> None:
    """A write immediately after create_tag may 400 "invalid select option"
    (live flake; same shape as the fresh-relation window). The repository
    retries with backoff instead of failing the create."""
    mock = MockAnytype(tag_settle_writes=2)
    config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
    client = AnytypeClient(config, transport=mock.transport)
    await ensure_schema(client)
    await seed_native_types(client)
    await client.create_property({"key": "role", "name": "Role", "format": "select"})

    async def instant(_: float) -> None:
        pass

    repo = AnytypeGraphRepository(client, sleep=instant)
    await repo.hydrate()
    node = await repo.create_node(NodeDraft(
        "Character", name="Autumn", summary="Worker.", fields={"role": "Antagonist"},
    ))
    assert node.fields["role"] == "Antagonist"
    await client.aclose()
