"""Live Anytype end-to-end fixtures, gated behind ``ANYTYPE_E2E=1``.

These talk to a real local Anytype server, so they are skipped by default.
To run them::

    ANYTYPE_E2E=1 python -m pytest tests/e2e -q

Credentials/endpoint come from the environment (the devcontainer already
sets the key file and base URL):

* key   -- ``ANYTYPE_API_KEY`` or, failing that, ``ANYTYPE_API_KEY_FILE``
* base  -- ``ANYTYPE_BASE_URL`` or ``ANYTYPE_API_BASE_URL``

The suite runs in ONE reusable space named ``GC-E2E`` (found by exact name,
created only if absent): the local API has no space-deletion endpoint
(list/create/get/update only, confirmed against 2025-11-08), so
space-per-run would leak a space every session. Instead the space is
*reset* -- every object archived, every test-created relation deleted --
both before the run (so a crashed session can't poison test premises) and
after it (so nothing lingers "once done"). The reset re-verifies the space
is named exactly ``GC-E2E`` and refuses to touch anything else. Writes are
throttled to ~1 req/s on a live server, so the config uses generous
retries -- the suite is intentionally slow.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import API_VERSION, AnytypeConfig
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema

# Native types a real story-world space would already define. The live E2E
# space is a throwaway, so the fixture seeds them before the contract runs.
_NATIVE_TYPES = {
    "character": "Character",
    "location": "Location",
    "event": "Event",
    "item": "Item",
    "organization": "Organization",
    "technology": "Technology",
}

# The one space this suite is allowed to write to -- and to reset.
_E2E_SPACE_NAME = "GC-E2E"

# ``objects``-format relations that belong to Anytype or our bootstrap and
# must survive a reset; anything else objects-format was created by a test.
_RESET_KEEP_RELATIONS = frozenset({"links", "backlinks", "creator", "last_modified_by"})


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


def _request(
    base: str, headers: dict[str, str], method: str, path: str, **kw: Any
) -> httpx.Response:
    """Plain call with bounded retry on throttling AND transient 5xx (a
    freshly created space 500s briefly before it is ready)."""
    response: httpx.Response | None = None
    for attempt in range(12):
        response = httpx.request(
            method, f"{base}{path}", headers=headers, timeout=30, **kw
        )
        if response.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(0.6 * (attempt + 1))
    assert response is not None
    response.raise_for_status()
    return response


def _find_or_create_space(base: str, headers: dict[str, str]) -> str:
    """Reuse the one space named exactly ``GC-E2E``; create it only once.

    The local API cannot delete spaces, so this suite must never mint a
    space per run.
    """
    listed = _request(base, headers, "GET", "/v1/spaces", params={"limit": 100})
    for space in listed.json()["data"]:
        if space.get("name") == _E2E_SPACE_NAME:
            return str(space["id"])
    created = _request(
        base, headers, "POST", "/v1/spaces", json={"name": _E2E_SPACE_NAME}
    )
    return str(created.json()["space"]["id"])


def _reset_space(base: str, headers: dict[str, str], space_id: str) -> None:
    """Clear the E2E space: archive every object, delete test relations.

    SAFETY: re-reads the space and refuses to touch it unless its name is
    exactly ``GC-E2E`` -- a mis-set ANYTYPE_SPACE_ID or a copied id must
    never empty a real story world.
    """
    space = _request(base, headers, "GET", f"/v1/spaces/{space_id}").json()["space"]
    if space.get("name") != _E2E_SPACE_NAME:
        raise RuntimeError(
            f"refusing to reset space {space_id}: named {space.get('name')!r}, "
            f"expected {_E2E_SPACE_NAME!r}"
        )
    # Archive everything (archived objects vanish from list/search, which is
    # what hydrate reads). Archiving shrinks the listing, so loop from the
    # first page until it comes back empty.
    while True:
        page = _request(
            base, headers, "GET", f"/v1/spaces/{space_id}/objects",
            params={"limit": 100},
        ).json()["data"]
        if not page:
            break
        for obj in page:
            _request(base, headers, "DELETE", f"/v1/spaces/{space_id}/objects/{obj['id']}")
    # Delete relations tests created (e.g. `inspired_by`) so "brand-new
    # relation" premises hold on the next run. gc_* (bootstrap-owned) and
    # Anytype's own relations survive.
    props = _request(
        base, headers, "GET", f"/v1/spaces/{space_id}/properties",
        params={"limit": 100},
    ).json()["data"]
    for prop in props:
        prop_key = str(prop.get("key", ""))
        if (
            prop.get("format") == "objects"
            and not prop_key.startswith("gc_")
            and prop_key not in _RESET_KEEP_RELATIONS
        ):
            # a failed delete is an undeletable builtin; harmless to leave
            with contextlib.suppress(httpx.HTTPStatusError):
                _request(
                    base, headers, "DELETE",
                    f"/v1/spaces/{space_id}/properties/{prop['id']}",
                )


@pytest.fixture(scope="session")
def live_config() -> Iterator[AnytypeConfig]:
    if os.environ.get("ANYTYPE_E2E") != "1":
        pytest.skip("set ANYTYPE_E2E=1 to run live Anytype tests")
    key, base = _key(), _base()
    headers = {"Authorization": f"Bearer {key}", "Anytype-Version": API_VERSION}
    space_id = _find_or_create_space(base, headers)
    _reset_space(base, headers, space_id)  # a crashed run must not leak state in
    config = AnytypeConfig(
        api_key=key, space_id=space_id, base_url=base,
        max_retries=10, backoff_base_seconds=0.5,
    )

    async def _bootstrap() -> None:
        client = AnytypeClient(config)
        try:
            await ensure_schema(client)
            # The space-reflecting model writes the user's *native* types; the
            # E2E space has none on first use, so seed a representative set.
            # Types survive resets, so seeding must be idempotent.
            existing = {t.get("key") async for t in client.list_types()}
            for key, name in _NATIVE_TYPES.items():
                if key not in existing:
                    await client.create_type(
                        {"key": key, "name": name, "plural_name": f"{name}s",
                         "layout": "basic"}
                    )
        finally:
            await client.aclose()

    asyncio.run(_bootstrap())
    yield config
    _reset_space(base, headers, space_id)  # leave nothing behind once done


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
