"""SessionStore contract (WP3): the fake and the Anytype adapter agree.

The fake ``InMemorySessionStore`` is the executable spec; the Anytype-backed
``AnytypeSessionStore`` (driven against the MockAnytype simulator) must match
its observable behaviour:

* save then load returns an equal snapshot;
* a fresh store with nothing stored loads ``None``;
* unreadable/corrupt stored state loads ``None`` rather than raising
  (the lenient-load contract that keeps startup from crashing).
"""

from __future__ import annotations

from typing import Any

import pytest

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.schema_bootstrap import (
    SESSION_TYPE_KEY,
    ensure_schema,
)
from graph_context.infrastructure.anytype.session_repository import AnytypeSessionStore
from graph_context.infrastructure.memory.fake_session_store import InMemorySessionStore

SNAPSHOT: dict[str, Any] = {
    "version": 1,
    "project": "Ashfall",
    "focus": [{"node_id": "n1", "pinned": True}, {"node_id": "n2", "pinned": False}],
    "recent": ["n2", "n1"],
}


# -- the fake -----------------------------------------------------------------


async def test_memory_store_round_trip() -> None:
    store = InMemorySessionStore()
    await store.save(SNAPSHOT)
    assert await store.load() == SNAPSHOT


async def test_memory_store_empty_loads_none() -> None:
    assert await InMemorySessionStore().load() is None


# -- the Anytype adapter (mock-backed) ---------------------------------------


@pytest.fixture
async def anytype_client() -> AnytypeClient:
    mock = MockAnytype()
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # creates gc_session_context type
    client._mock = mock  # type: ignore[attr-defined]  # handy for seeding
    return client


async def test_anytype_store_round_trip(anytype_client: AnytypeClient) -> None:
    await AnytypeSessionStore(anytype_client).save(SNAPSHOT)
    # A *fresh* store instance must find the node by type search (no cache).
    loaded = await AnytypeSessionStore(anytype_client).load()
    assert loaded == SNAPSHOT


async def test_anytype_store_empty_loads_none(anytype_client: AnytypeClient) -> None:
    assert await AnytypeSessionStore(anytype_client).load() is None


async def test_anytype_store_overwrites_in_place(anytype_client: AnytypeClient) -> None:
    store = AnytypeSessionStore(anytype_client)
    await store.save(SNAPSHOT)
    await store.save({**SNAPSHOT, "project": "Second"})
    # still exactly one SessionContext object, holding the latest snapshot
    objs = [o async for o in anytype_client.search(types=[SESSION_TYPE_KEY])]
    assert len(objs) == 1
    assert (await AnytypeSessionStore(anytype_client).load())["project"] == "Second"


async def test_anytype_store_corrupt_json_loads_none(
    anytype_client: AnytypeClient,
) -> None:
    mock: MockAnytype = anytype_client._mock  # type: ignore[attr-defined]
    mock.seed_object(
        SESSION_TYPE_KEY,
        "Session context (managed)",
        properties=[
            {"key": mapping.PROP_FIELDS, "format": "text", "text": "{not valid json"}
        ],
    )
    assert await AnytypeSessionStore(anytype_client).load() is None
