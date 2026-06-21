"""Adapter test fixtures: a bootstrapped repository over the mock server."""

import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema


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
    repository = AnytypeGraphRepository(client)
    await repository.hydrate()
    return repository
