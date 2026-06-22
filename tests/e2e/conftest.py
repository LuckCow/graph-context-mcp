"""Live Anytype end-to-end fixtures, gated behind ``ANYTYPE_E2E=1``.

These talk to a real local Anytype server, so they are skipped by default.
To run them::

    ANYTYPE_E2E=1 python -m pytest tests/e2e -q

Credentials/endpoint come from the environment (the devcontainer already
sets the key file and base URL):

* key   -- ``ANYTYPE_API_KEY`` or, failing that, ``ANYTYPE_API_KEY_FILE``
* base  -- ``ANYTYPE_BASE_URL`` or ``ANYTYPE_API_BASE_URL``

A fresh throwaway space is created and schema-bootstrapped **once per test
session**; objects accumulate in it across tests, which is fine because the
contract assertions are self-contained (they reference the ids they create).
Writes are throttled to ~1 req/s on a live server, so the config uses
generous retries -- the suite is intentionally slow.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import API_VERSION, AnytypeConfig
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema


def _key() -> str:
    if os.environ.get("ANYTYPE_API_KEY"):
        return os.environ["ANYTYPE_API_KEY"]
    path = os.environ.get("ANYTYPE_API_KEY_FILE")
    if path and os.path.exists(path):
        with open(path) as handle:
            return handle.read().strip()
    pytest.skip("no ANYTYPE_API_KEY / ANYTYPE_API_KEY_FILE for live E2E")


def _base() -> str:
    return (
        os.environ.get("ANYTYPE_BASE_URL")
        or os.environ.get("ANYTYPE_API_BASE_URL")
        or "http://localhost:31009"
    )


@pytest.fixture(scope="session")
def live_config() -> AnytypeConfig:
    if os.environ.get("ANYTYPE_E2E") != "1":
        pytest.skip("set ANYTYPE_E2E=1 to run live Anytype tests")
    key, base = _key(), _base()
    headers = {"Authorization": f"Bearer {key}", "Anytype-Version": API_VERSION}
    created = httpx.post(
        f"{base}/v1/spaces", headers=headers, json={"name": "GC-E2E"}, timeout=30
    )
    created.raise_for_status()
    space_id = created.json()["space"]["id"]
    config = AnytypeConfig(
        api_key=key, space_id=space_id, base_url=base,
        max_retries=10, backoff_base_seconds=0.5,
    )

    async def _bootstrap() -> None:
        client = AnytypeClient(config)
        try:
            await ensure_schema(client)
        finally:
            await client.aclose()

    asyncio.run(_bootstrap())
    return config


@pytest.fixture
async def repo(live_config: AnytypeConfig):
    client = AnytypeClient(live_config)
    repository = AnytypeGraphRepository(client)
    await repository.hydrate()
    yield repository
    await client.aclose()


class RawApi:
    """Direct, client-bypassing calls -- stands in for a human in the UI."""

    def __init__(self, config: AnytypeConfig) -> None:
        self._base = config.base_url
        self._space = config.space_id
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Anytype-Version": config.api_version,
        }

    def _send(self, method: str, obj_id: str, **kw: Any) -> httpx.Response:
        url = f"{self._base}/v1/spaces/{self._space}/objects/{obj_id}"
        for _ in range(15):
            response = httpx.request(method, url, headers=self._headers, timeout=30, **kw)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            time.sleep(1.1)
        response.raise_for_status()
        return response

    def rename(self, obj_id: str, name: str) -> None:
        self._send("PATCH", obj_id, json={"name": name})

    def archive(self, obj_id: str) -> None:
        self._send("DELETE", obj_id)


@pytest.fixture
def raw_api(live_config: AnytypeConfig) -> RawApi:
    return RawApi(live_config)
