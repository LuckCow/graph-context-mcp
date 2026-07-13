"""SessionStore contract (WP3, keyed since WP8): fake and adapter agree.

The fake ``InMemorySessionStore`` is the executable spec; the Anytype-backed
``AnytypeSessionStore`` (driven against the MockAnytype simulator) must match
its observable behaviour:

* save then load for a key returns an equal snapshot;
* distinct keys are fully independent snapshots;
* a fresh store with nothing stored for the key loads ``None``;
* an empty key raises ``ValueError`` (a bug, never data);
* unreadable/corrupt stored state loads ``None`` rather than raising
  (the lenient-load contract that keeps startup from crashing);
* stray unkeyed session nodes match NO key (adapter-only: warned once,
  left for the human to delete).
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

KEY = "anytype:chat-arc-one"
OTHER_KEY = "anytype:chat-arc-two"

SNAPSHOT: dict[str, Any] = {
    "version": 2,
    "project": "Ashfall",
    "scratchpad": "next turn: resolve the gate standoff",
    "mode": "authoring",
    "working_set": [
        {"node_id": "n1", "detail": "full"},
        {"node_id": "n2", "detail": "summaries"},
    ],
    "recent": ["n2", "n1"],
}


# -- the fake -----------------------------------------------------------------


async def test_memory_store_round_trip() -> None:
    store = InMemorySessionStore()
    await store.save(SNAPSHOT, KEY)
    assert await store.load(KEY) == SNAPSHOT


async def test_memory_store_keys_are_independent() -> None:
    store = InMemorySessionStore()
    await store.save(SNAPSHOT, KEY)
    await store.save({**SNAPSHOT, "project": "Fieldwork"}, OTHER_KEY)
    assert (await store.load(KEY))["project"] == "Ashfall"
    assert (await store.load(OTHER_KEY))["project"] == "Fieldwork"


async def test_memory_store_empty_loads_none() -> None:
    assert await InMemorySessionStore().load(KEY) is None


async def test_memory_store_rejects_an_empty_key() -> None:
    store = InMemorySessionStore()
    with pytest.raises(ValueError):
        await store.load("")
    with pytest.raises(ValueError):
        await store.save(SNAPSHOT, "  ")


# -- the Anytype adapter (mock-backed) ---------------------------------------


@pytest.fixture
def mock() -> MockAnytype:
    return MockAnytype()


@pytest.fixture
async def anytype_client(mock: MockAnytype) -> AnytypeClient:
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # creates gc_session_context type
    return client


async def test_anytype_store_round_trip(anytype_client: AnytypeClient) -> None:
    await AnytypeSessionStore(anytype_client).save(SNAPSHOT, KEY)
    # A *fresh* store instance must find the node by type search (no cache).
    loaded = await AnytypeSessionStore(anytype_client).load(KEY)
    assert loaded == SNAPSHOT


async def test_anytype_store_keys_get_independent_nodes(
    anytype_client: AnytypeClient,
) -> None:
    store = AnytypeSessionStore(anytype_client)
    await store.save(SNAPSHOT, KEY)
    await store.save({**SNAPSHOT, "project": "Fieldwork"}, OTHER_KEY)
    objs = [o async for o in anytype_client.search(types=[SESSION_TYPE_KEY])]
    assert len(objs) == 2  # one SessionContext node per key
    fresh = AnytypeSessionStore(anytype_client)
    assert (await fresh.load(KEY))["project"] == "Ashfall"
    assert (await fresh.load(OTHER_KEY))["project"] == "Fieldwork"


async def test_anytype_store_empty_loads_none(anytype_client: AnytypeClient) -> None:
    assert await AnytypeSessionStore(anytype_client).load(KEY) is None


async def test_anytype_store_rejects_an_empty_key(
    anytype_client: AnytypeClient,
) -> None:
    with pytest.raises(ValueError):
        await AnytypeSessionStore(anytype_client).load("")


async def test_anytype_store_overwrites_in_place(anytype_client: AnytypeClient) -> None:
    store = AnytypeSessionStore(anytype_client)
    await store.save(SNAPSHOT, KEY)
    await store.save({**SNAPSHOT, "project": "Second"}, KEY)
    # still exactly one SessionContext object for the key, latest snapshot
    objs = [o async for o in anytype_client.search(types=[SESSION_TYPE_KEY])]
    assert len(objs) == 1
    assert (await AnytypeSessionStore(anytype_client).load(KEY))["project"] == "Second"


async def test_anytype_node_carries_key_and_label(
    anytype_client: AnytypeClient,
) -> None:
    labels = {KEY: "Plot: Act II"}
    await AnytypeSessionStore(anytype_client, labels=labels).save(SNAPSHOT, KEY)
    (obj,) = [o async for o in anytype_client.search(types=[SESSION_TYPE_KEY])]
    assert obj["name"] == "Session context — Plot: Act II"
    properties = {p["key"]: p.get("text") for p in obj["properties"]}
    assert properties[mapping.PROP_SESSION_KEY] == KEY


async def test_stray_unkeyed_node_matches_no_key(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """Nodes without gc_session_key (e.g. hand-created) are inert: never
    loaded, never overwritten -- a new keyed node is minted beside them."""
    mock.seed_object(
        SESSION_TYPE_KEY,
        "Session context (managed)",
        properties=[
            {"key": mapping.PROP_CHAT_SESSION, "format": "text",
             "text": '{"version": 2, "project": "Stray"}'},
        ],
    )
    store = AnytypeSessionStore(anytype_client)
    assert await store.load(KEY) is None  # inert, not adopted
    await store.save(SNAPSHOT, KEY)
    objs = [o async for o in anytype_client.search(types=[SESSION_TYPE_KEY])]
    assert len(objs) == 2  # keyed node minted BESIDE the stray one
    assert (await AnytypeSessionStore(anytype_client).load(KEY)) == SNAPSHOT


async def test_two_spaces_persist_sessions_independently() -> None:
    """ADR 017: each channel's runtime saves to its own space's
    SessionContext node -- neighbors never see each other's snapshot."""
    stores = []
    for space in ("space-a", "space-b"):
        mock = MockAnytype(space_id=space)
        client = AnytypeClient(
            AnytypeConfig(api_key="t", space_id=space), transport=mock.transport
        )
        await ensure_schema(client)
        stores.append(AnytypeSessionStore(client))
    await stores[0].save(SNAPSHOT, KEY)
    await stores[1].save({**SNAPSHOT, "project": "Fieldwork"}, KEY)
    assert (await stores[0].load(KEY))["project"] == "Ashfall"
    assert (await stores[1].load(KEY))["project"] == "Fieldwork"


async def test_anytype_store_corrupt_json_loads_none(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    mock.seed_object(
        SESSION_TYPE_KEY,
        "Session context — broken",
        properties=[
            {"key": mapping.PROP_SESSION_KEY, "format": "text", "text": KEY},
            {"key": mapping.PROP_CHAT_SESSION, "format": "text",
             "text": "{not valid json"},
        ],
    )
    assert await AnytypeSessionStore(anytype_client).load(KEY) is None
