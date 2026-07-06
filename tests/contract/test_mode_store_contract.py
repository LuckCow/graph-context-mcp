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
    EXAMPLE_MODE_NAME,
    MODE_TYPE_KEY,
    ensure_schema,
)
from graph_context.infrastructure.memory.fake_mode_store import InMemoryModeStore

SCRIBE = {
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
async def anytype_client() -> AnytypeClient:
    mock = MockAnytype()
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # mints gc_activity_mode + the example
    client._mock = mock  # type: ignore[attr-defined]  # handy for seeding
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
) -> str:
    properties: list[dict[str, Any]] = [
        {"key": mapping.PROP_MODE_MUTATING, "format": "checkbox",
         "checkbox": mutating},
        {"key": mapping.PROP_CAPTURE_TYPE, "format": "text",
         "text": capture_type},
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


async def test_bootstrap_seeds_the_example_mode_once(
    anytype_client: AnytypeClient,
) -> None:
    """The type mint ships the one-time template whose body explains the
    feature (including that /mode applies edits); a re-run adds nothing."""
    await ensure_schema(anytype_client)  # idempotent second run
    payloads = await AnytypeModeStore(anytype_client).load()
    assert [p["name"] for p in payloads] == [EXAMPLE_MODE_NAME]
    example = payloads[0]
    assert "/mode" in example["goal"]  # the explainer names the command
    assert not example["mutating"]
    assert example["capture"] is None


async def test_anytype_store_round_trips_a_mode_object(
    anytype_client: AnytypeClient,
) -> None:
    mock: MockAnytype = anytype_client._mock  # type: ignore[attr-defined]
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


async def test_anytype_store_skips_archived_objects(
    anytype_client: AnytypeClient,
) -> None:
    mock: MockAnytype = anytype_client._mock  # type: ignore[attr-defined]
    object_id = _seed_mode(mock, "Disabled", "Old goal.")
    mock.archive_directly(object_id)
    assert "Disabled" not in await _load_by_name(anytype_client)


async def test_capture_absent_when_capture_type_empty(
    anytype_client: AnytypeClient,
) -> None:
    """Presence of gc_capture_type is the capture switch: an empty text
    (the property exists, the human left it blank) means no capture."""
    mock: MockAnytype = anytype_client._mock  # type: ignore[attr-defined]
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
