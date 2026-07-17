"""ModeStore contract (ADR 015 amendment): the fake and the Anytype
adapter agree.

The fake ``InMemoryModeStore`` is the executable spec; the Anytype-backed
``AnytypeModeStore`` (driven against the MockAnytype simulator) must match
its observable behaviour:

* a space with no Activity Mode objects loads the empty list (the fake's
  default; on the Anytype side a fresh space holds exactly the bootstrap's
  example object);
* a seeded mode object round-trips name / goal (the page body) /
  mutating / capture into the payload shape the port documents;
* archived objects are skipped -- archiving is the "disable" gesture;
* capture is ``None`` unless ``gc_capture_type`` is non-empty (presence
  enables capture; there is no separate toggle).

``ensure_schema``'s side of the feature is pinned here too: the type is
minted with its fields attached (live-confirmed) plus the one-time
example/explainer object, idempotently.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.mode_store import AnytypeModeStore
from graph_context.infrastructure.anytype.schema_bootstrap import (
    MODE_TYPE_KEY,
    ensure_schema,
)
from graph_context.infrastructure.memory.fake_mode_store import InMemoryModeStore

SCRIBE = {
    "id": "obj-1",
    "name": "Faithful Scribe",
    "goal": "Record only what the user explicitly states.",
    "mutating": True,
    "capture": None,
    "origin": "Faithful Scribe (obj-1)",
}


# -- the fake -----------------------------------------------------------------


async def test_memory_store_round_trip() -> None:
    store = InMemoryModeStore([SCRIBE])
    assert await store.load() == [SCRIBE]


async def test_memory_store_empty_loads_nothing() -> None:
    assert await InMemoryModeStore().load() == []


# -- the Anytype adapter (mock-backed) ---------------------------------------


@pytest.fixture
def mock() -> MockAnytype:
    return MockAnytype()


@pytest.fixture
async def anytype_client(mock: MockAnytype) -> AnytypeClient:
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # mints gc_activity_mode + the example
    return client


def _seed_mode(
    mock: MockAnytype,
    name: str,
    body: str,
    *,
    mutating: bool = False,
    capture_type: str = "",
    references: str = "",
    min_chars: float | None = None,
    activity_detail: str = "",
    web_search: bool = False,
    model: str = "",
) -> str:
    properties: list[dict[str, Any]] = [
        {"key": mapping.PROP_MODE_MUTATING, "format": "checkbox",
         "checkbox": mutating},
        {"key": mapping.PROP_MODE_WEB_SEARCH, "format": "checkbox",
         "checkbox": web_search},
        {"key": mapping.PROP_CAPTURE_TYPE, "format": "text",
         "text": capture_type},
        # A select: the read side sees the picked option as a tag envelope.
        {"key": mapping.PROP_MODE_ACTIVITY_DETAIL, "format": "select",
         "select": {"name": activity_detail} if activity_detail else None},
        {"key": mapping.PROP_MODE_MODEL, "format": "select",
         "select": {"name": model} if model else None},
    ]
    if references:
        properties.append({"key": mapping.PROP_CAPTURE_REFERENCES,
                           "format": "text", "text": references})
    if min_chars is not None:
        properties.append({"key": mapping.PROP_CAPTURE_MIN_CHARS,
                           "format": "number", "number": min_chars})
    return mock.seed_object(MODE_TYPE_KEY, name, properties, body=body)


async def _load_by_name(client: AnytypeClient) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in await AnytypeModeStore(client).load()}


async def test_the_type_mint_seeds_no_objects(
    anytype_client: AnytypeClient,
) -> None:
    """ADR 035: ensure_schema mints the TYPE only; the example/template
    object now ships with the starter-mode seed (mode_seeder), whose
    trigger is "the space has no Activity Mode objects" -- so the mint
    must leave the space empty for the seeder to see it that way."""
    await ensure_schema(anytype_client)  # idempotent second run
    assert await AnytypeModeStore(anytype_client).load() == []


async def test_anytype_store_round_trips_a_mode_object(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    object_id = _seed_mode(
        # Trailing padding mirrors the live export (it pads the body);
        # the store must hand the loader a clean goal either way.
        mock, "Faithful Scribe", "Record only what the user states.   \n",
        mutating=True,
        capture_type="note", references="sources", min_chars=120.0,
    )
    scribe = (await _load_by_name(anytype_client))["Faithful Scribe"]
    assert scribe["goal"] == "Record only what the user states."
    assert scribe["mutating"] is True
    assert scribe["capture"] == {
        "artifact_type": "note",
        "references_label": "sources",
        "min_chars": 120.0,
    }
    assert object_id in scribe["origin"]  # errors can name the object
    # ADR 034: the Space Context's default-mode link resolves by object id.
    assert scribe["id"] == object_id


async def test_anytype_store_skips_archived_objects(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    object_id = _seed_mode(mock, "Disabled", "Old goal.")
    mock.archive_directly(object_id)
    assert "Disabled" not in await _load_by_name(anytype_client)


async def test_activity_detail_rides_the_payload_when_set(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """WP19 (ADR 029): the picked gc_mode_activity_detail option's NAME
    reaches the loader (validation lives there, naming the object; the
    loader lowercases, so Title-Case options match); an unpicked select
    leaves the key out, so the loader applies the default."""
    _seed_mode(mock, "Chatty", "A goal.", activity_detail="Full")
    _seed_mode(mock, "Unset", "A goal.")
    payloads = await _load_by_name(anytype_client)
    assert payloads["Chatty"]["activity_detail"] == "Full"
    assert "activity_detail" not in payloads["Unset"]


async def test_web_search_checkbox_rides_the_payload(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """WP20 (ADR 030): gc_mode_web_search is a checkbox; ticked admits
    the provider's server-side web search for the mode. Always present
    in the payload (like ``mutating``) -- unticked reads False."""
    _seed_mode(mock, "Researcher", "A goal.", web_search=True)
    _seed_mode(mock, "Grounded", "A goal.")
    payloads = await _load_by_name(anytype_client)
    assert payloads["Researcher"]["web_search"] is True
    assert payloads["Grounded"]["web_search"] is False


async def test_model_choice_rides_the_payload_when_set(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """ADR 033: the picked gc_mode_model option's NAME reaches the loader
    (same select rule as activity_detail: the loader lowercases and
    validates, naming the object); an unpicked select leaves the key out,
    so the deployment default applies."""
    _seed_mode(mock, "Heavy", "A goal.", model="Opus 4.8")
    _seed_mode(mock, "Unset", "A goal.")
    payloads = await _load_by_name(anytype_client)
    assert payloads["Heavy"]["model"] == "Opus 4.8"
    assert "model" not in payloads["Unset"]


async def test_bootstrap_seeds_the_model_dropdown_options(
    anytype_client: AnytypeClient,
) -> None:
    """ADR 033: gc_mode_model is a SELECT whose options bootstrap
    pre-seeds -- the human picks the Claude model from a dropdown.
    Idempotent: a re-run adds no duplicates."""
    await ensure_schema(anytype_client)  # second run
    prop = {
        p["key"]: p async for p in anytype_client.list_properties()
    }[mapping.PROP_MODE_MODEL]
    assert prop["format"] == "select"
    names = [
        t["name"] async for t in anytype_client.list_tags(prop["id"])
    ]
    assert sorted(names) == ["Fable 5", "Opus 4.8", "Sonnet 5"]


async def test_bootstrap_seeds_the_detail_dropdown_options(
    anytype_client: AnytypeClient,
) -> None:
    """WP19 amendment: gc_mode_activity_detail is a SELECT whose options
    bootstrap pre-seeds -- the human picks Off/Minimal/Tools/Full from a
    dropdown instead of typing the enum. Idempotent: a re-run adds no
    duplicates."""
    await ensure_schema(anytype_client)  # second run
    prop = {
        p["key"]: p async for p in anytype_client.list_properties()
    }[mapping.PROP_MODE_ACTIVITY_DETAIL]
    assert prop["format"] == "select"
    names = [
        t["name"] async for t in anytype_client.list_tags(prop["id"])
    ]
    assert sorted(names) == ["Full", "Minimal", "Off", "Tools"]


async def test_bootstrap_heals_a_text_minted_detail_property(
    mock: MockAnytype,
) -> None:
    """Quirk A12: the one-day-old TEXT variant of gc_mode_activity_detail
    cannot change format in place -- bootstrap deletes it and re-mints a
    select, re-attaching the field to the type with options seeded."""
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id),
        transport=mock.transport,
    )
    await client.create_property({
        "key": mapping.PROP_MODE_ACTIVITY_DETAIL,
        "name": mapping.PROP_MODE_ACTIVITY_DETAIL, "format": "text",
    })
    await client.create_type({
        "key": MODE_TYPE_KEY, "name": "Activity Mode",
        "plural_name": "Activity Modes", "layout": "basic",
        "properties": [
            {"key": key, "name": key,
             "format": "text" if key == mapping.PROP_MODE_ACTIVITY_DETAIL
             else fmt}
            for key, fmt in mapping.MODE_PROPERTIES.items()
        ],
    })
    await ensure_schema(client)
    prop = {
        p["key"]: p async for p in client.list_properties()
    }[mapping.PROP_MODE_ACTIVITY_DETAIL]
    assert prop["format"] == "select"
    names = [t["name"] async for t in client.list_tags(prop["id"])]
    assert sorted(names) == ["Full", "Minimal", "Off", "Tools"]
    mode_type = {t["key"]: t async for t in client.list_types()}[MODE_TYPE_KEY]
    attached = {
        e["key"]: e["format"] for e in mode_type.get("properties", [])
    }
    assert attached[mapping.PROP_MODE_ACTIVITY_DETAIL] == "select"
    assert set(mapping.MODE_PROPERTIES) <= set(attached)  # nothing lost


async def test_capture_absent_when_capture_type_empty(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """Presence of gc_capture_type is the capture switch: an empty text
    (the property exists, the human left it blank) means no capture."""
    _seed_mode(mock, "Plain", "A goal.", capture_type="", min_chars=99.0)
    plain = (await _load_by_name(anytype_client))["Plain"]
    assert plain["capture"] is None


async def test_bootstrap_attaches_mode_fields_to_the_type(
    anytype_client: AnytypeClient,
) -> None:
    """The UI story: the fields show on the type (inline create,
    live-confirmed 2026-07-06) and exist as space properties."""
    mode_type = {
        t["key"]: t async for t in anytype_client.list_types()
    }[MODE_TYPE_KEY]
    type_properties = {
        entry["key"] for entry in mode_type.get("properties", [])
    }
    assert set(mapping.MODE_PROPERTIES) <= type_properties
    space_properties = {
        p["key"] async for p in anytype_client.list_properties()
    }
    assert set(mapping.MODE_PROPERTIES) <= space_properties


async def test_bootstrap_retrofits_new_fields_onto_an_existing_type(
    mock: MockAnytype,
) -> None:
    """The upgraded-space story (WP19): a type minted BEFORE a field was
    added to its inline set gains that field on the next ensure_schema
    (quirk A11 update: full list resent + the missing entry), keeping
    everything it already had -- and without re-seeding the example."""
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id),
        transport=mock.transport,
    )
    pre_wp19 = {
        key: fmt for key, fmt in mapping.MODE_PROPERTIES.items()
        if key != mapping.PROP_MODE_ACTIVITY_DETAIL
    }
    await client.create_type({
        "key": MODE_TYPE_KEY, "name": "Activity Mode",
        "plural_name": "Activity Modes", "layout": "basic",
        "properties": [
            {"key": key, "name": key, "format": fmt}
            for key, fmt in pre_wp19.items()
        ],
    })
    await ensure_schema(client)
    mode_type = {t["key"]: t async for t in client.list_types()}[MODE_TYPE_KEY]
    type_properties = {e["key"] for e in mode_type.get("properties", [])}
    assert set(mapping.MODE_PROPERTIES) <= type_properties  # retrofitted
    assert set(pre_wp19) <= type_properties                 # nothing lost
    hits = [o async for o in client.search(types=[MODE_TYPE_KEY])]
    assert hits == []  # the example seeds only when the TYPE is minted
