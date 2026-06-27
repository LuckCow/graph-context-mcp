"""Adapter test fixtures: a bootstrapped repository over the mock server.

The space-reflecting model reads/writes the user's *native* types, so the
mock space is seeded with a representative set of native types (as if the
user created them in the Anytype UI) in addition to the gc_ infrastructure
that ``ensure_schema`` bootstraps.
"""

import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema

# Native types the user already has in their space (key -> display name).
NATIVE_TYPES = {
    "character": "Character",
    "location": "Location",
    "event": "Event",
    "item": "Item",
    "organization": "Organization",
    "technology": "Technology",
    "theme": "Theme",
}


async def seed_native_types(client: AnytypeClient) -> None:
    for key, name in NATIVE_TYPES.items():
        await client.create_type(
            {"key": key, "name": name, "plural_name": f"{name}s", "layout": "basic"}
        )


async def _noop_sleep(_: float) -> None:
    """Keep retry/backoff tests instant."""


@pytest.fixture
def mock() -> MockAnytype:
    return MockAnytype()


@pytest.fixture
async def client(mock: MockAnytype):
    config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
    c = AnytypeClient(config, transport=mock.transport, sleep=_noop_sleep)
    yield c
    await c.aclose()


@pytest.fixture
async def repo(mock: MockAnytype, client: AnytypeClient) -> AnytypeGraphRepository:
    await ensure_schema(client)
    await seed_native_types(client)
    repository = AnytypeGraphRepository(client)
    await repository.hydrate()
    return repository
