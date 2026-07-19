"""SpaceContextStore contract (ADR 034): the fake and the Anytype
adapter agree.

The fake ``InMemorySpaceContextStore`` is the executable spec; the
Anytype-backed ``AnytypeSpaceContextStore`` (driven against the
MockAnytype simulator) must match its observable behaviour:

* a space with no Space Context object loads the empty list (the fake's
  default; on the Anytype side bootstrap seeds exactly one singleton
  when the type is first minted, with an EMPTY default-mode link);
* the object's ``gc_default_mode`` relation targets round-trip into
  ``default_mode_ids``, with an origin the loader's errors can name;
* archived objects are skipped -- archiving is the "reset" gesture.

``ensure_schema``'s side is pinned here too: the type is minted with the
link field attached plus the one-time singleton, idempotently. The edge
quarantine is pinned at the mapping seam: the settings link is server
config, never story structure.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.schema_bootstrap import (
    SPACE_CONTEXT_NAME,
    SPACE_CONTEXT_TYPE_KEY,
    ensure_schema,
)
from graph_context.infrastructure.anytype.space_context_store import (
    AnytypeSpaceContextStore,
)
from graph_context.infrastructure.memory.fake_space_context_store import (
    InMemorySpaceContextStore,
)

SETTINGS = {
    "name": "Space Context",
    "default_mode_ids": ["obj-1"],
    "origin": "Space Context (sc-1)",
}


# -- the fake -----------------------------------------------------------------


async def test_memory_store_round_trip() -> None:
    store = InMemorySpaceContextStore([SETTINGS])
    assert await store.load() == [SETTINGS]


async def test_memory_store_empty_loads_nothing() -> None:
    assert await InMemorySpaceContextStore().load() == []


# -- the Anytype adapter (mock-backed) ---------------------------------------


@pytest.fixture
def mock() -> MockAnytype:
    return MockAnytype()


@pytest.fixture
async def anytype_client(mock: MockAnytype) -> AnytypeClient:
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # mints gc_space_context + the singleton
    return client


def _seed_space_context(
    mock: MockAnytype, name: str, linked_ids: list[str]
) -> str:
    properties: list[dict[str, Any]] = [
        {"key": mapping.PROP_DEFAULT_MODE, "format": "objects",
         "objects": linked_ids},
    ]
    return mock.seed_object(SPACE_CONTEXT_TYPE_KEY, name, properties)


async def test_bootstrap_seeds_the_singleton_with_an_empty_link(
    anytype_client: AnytypeClient,
) -> None:
    """The type mint ships ONE settings object whose default-mode link is
    empty (new chats keep the profile default until a human links a
    mode); a re-run adds nothing."""
    await ensure_schema(anytype_client)  # idempotent second run
    payloads = await AnytypeSpaceContextStore(anytype_client).load()
    assert [p["name"] for p in payloads] == [SPACE_CONTEXT_NAME]
    assert payloads[0]["default_mode_ids"] == []


async def test_link_targets_ride_the_payload_with_the_origin(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    object_id = _seed_space_context(mock, "House Rules", ["mode-1", "mode-2"])
    payloads = await AnytypeSpaceContextStore(anytype_client).load()
    rules = next(p for p in payloads if p["name"] == "House Rules")
    # Order preserved, arity NOT judged here -- the loader owns rejection.
    assert rules["default_mode_ids"] == ["mode-1", "mode-2"]
    assert object_id in rules["origin"]  # errors can name the object


async def test_archived_objects_are_skipped(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    object_id = _seed_space_context(mock, "Old Settings", ["mode-1"])
    mock.archive_directly(object_id)
    payloads = await AnytypeSpaceContextStore(anytype_client).load()
    assert "Old Settings" not in [p["name"] for p in payloads]


async def test_bootstrap_attaches_the_link_field_to_the_type(
    anytype_client: AnytypeClient,
) -> None:
    """The UI story: the Default mode field shows on the type (inline
    create) and exists as a space property."""
    context_type = {
        t["key"]: t async for t in anytype_client.list_types()
    }[SPACE_CONTEXT_TYPE_KEY]
    attached = {
        entry["key"]: entry["format"]
        for entry in context_type.get("properties", [])
    }
    assert attached[mapping.PROP_DEFAULT_MODE] == "objects"
    space_properties = {
        p["key"] async for p in anytype_client.list_properties()
    }
    assert mapping.PROP_DEFAULT_MODE in space_properties


def test_the_settings_link_never_reflects_as_an_edge() -> None:
    """The quarantine (ADR 034): gc_default_mode is server config on an
    infra node, not story structure -- ``to_edges`` (the single seam
    every hydrate/resync edge passes through) must skip it."""
    edges = mapping.to_edges({
        "id": "sc-1",
        "properties": [
            {"key": mapping.PROP_DEFAULT_MODE, "format": "objects",
             "objects": ["mode-1"]},
        ],
    })
    assert edges == []
