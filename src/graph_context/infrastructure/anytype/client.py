"""Thin async HTTP client for the Anytype local API.

Responsibilities (and nothing more): authentication headers, the pinned
``Anytype-Version`` header, pagination stitching, bounded retry with
exponential backoff on 429/5xx, and translation of failures into
:class:`AnytypeApiError`. No domain knowledge -- payload shapes belong to
``mapping.py``.

Rate-limit awareness: the API allows a burst of 60 requests, then
1 request/second sustained (docs: Fundamentals -> Rate Limits). Design
rule: ``hydrate`` is the only code path allowed to approach the burst
budget; per-tool operations must stay far below it. ``request_count`` is
exposed so tests and the demo can assert call budgets.

``transport`` and ``sleep`` are injectable for tests (mock server /
no-op backoff).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from graph_context.infrastructure.anytype.config import AnytypeApiError, AnytypeConfig

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _unwrap(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Pull the single envelope key (``object``/``type``/``property``) the
    write endpoints wrap their result in, typed as a dict (the JSON decoder
    hands back ``Any``)."""
    value: dict[str, Any] = payload[key]
    return value


class AnytypeClient:
    """Low-level access to ``/v1`` endpoints, scoped to one configured space."""

    def __init__(
        self,
        config: AnytypeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self.request_count = 0
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Anytype-Version": config.api_version,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- generic request with bounded retry ------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: AnytypeApiError | None = None
        for attempt in range(self._config.max_retries + 1):
            self.request_count += 1
            response = await self._http.request(method, path, params=params, json=json)
            if response.status_code < 400:
                return response.json() if response.content else {}
            error = self._to_error(response, path)
            if response.status_code not in _RETRYABLE_STATUSES:
                raise error
            last_error = error
            if attempt < self._config.max_retries:
                delay = self._config.backoff_base_seconds * (2**attempt)
                logger.warning(
                    "retryable %s from %s (attempt %d), backing off %.2fs",
                    response.status_code, path, attempt + 1, delay,
                )
                await self._sleep(delay)
        assert last_error is not None
        raise last_error

    async def paginate(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        page_limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item across all pages of a list/search endpoint.

        Pagination is always via the ``offset``/``limit`` *query* parameters
        (spike S2: ``POST /search`` ignores them in the body), so the same
        loop serves both the GET list endpoints and the POST search endpoint.
        """
        limit = page_limit or self._config.page_limit
        offset = 0
        while True:
            page = await self.request(
                method,
                path,
                params={**(params or {}), "offset": offset, "limit": limit},
                json=json,
            )
            data: list[dict[str, Any]] = page.get("data", [])
            for item in data:
                yield item
            pagination = page.get("pagination") or {}
            if not pagination.get("has_more") or not data:
                return
            offset += len(data)

    # -- space-scoped convenience wrappers --------------------------------

    @property
    def _space(self) -> str:
        return f"/v1/spaces/{self._config.space_id}"

    async def get_space(self) -> dict[str, Any]:
        """The configured space's own record (name etc.)."""
        payload = await self.request("GET", self._space)
        return _unwrap(payload, "space")

    def list_objects(self) -> AsyncIterator[dict[str, Any]]:
        """Full unfiltered object sweep -- the hydrate path (spike S2: GET
        ``/objects`` takes no filters and honors a large page size)."""
        return self.paginate(f"{self._space}/objects")

    def search(
        self,
        *,
        types: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Filtered/sorted query via ``POST /search`` -- the resync path.

        Spike S3/S5: type-scoping (``types``) and the modified-since filter
        live in the request *body*; the endpoint pages at 100 max. The body
        keys are omitted when ``None`` so an empty search returns everything.
        """
        body: dict[str, Any] = {}
        if types is not None:
            body["types"] = types
        if filters is not None:
            body["filters"] = filters
        if sort is not None:
            body["sort"] = sort
        return self.paginate(
            f"{self._space}/search",
            method="POST",
            json=body,
            page_limit=self._config.search_page_limit,
        )

    async def get_object(self, object_id: str) -> dict[str, Any]:
        payload = await self.request("GET", f"{self._space}/objects/{object_id}")
        return _unwrap(payload, "object")

    async def create_object(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", f"{self._space}/objects", json=body)
        return _unwrap(payload, "object")

    async def update_object(self, object_id: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request(
            "PATCH", f"{self._space}/objects/{object_id}", json=body
        )
        return _unwrap(payload, "object")

    async def archive_object(self, object_id: str) -> None:
        await self.request("DELETE", f"{self._space}/objects/{object_id}")

    def list_types(self) -> AsyncIterator[dict[str, Any]]:
        return self.paginate(f"{self._space}/types")

    async def create_type(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", f"{self._space}/types", json=body)
        return _unwrap(payload, "type")

    def list_properties(self) -> AsyncIterator[dict[str, Any]]:
        return self.paginate(f"{self._space}/properties")

    async def create_property(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", f"{self._space}/properties", json=body)
        return _unwrap(payload, "property")

    def list_tags(self, property_id: str) -> AsyncIterator[dict[str, Any]]:
        """Select/multi_select options ("tags", ADR 012). Property ID, not key."""
        return self.paginate(f"{self._space}/properties/{property_id}/tags")

    async def create_tag(self, property_id: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request(
            "POST", f"{self._space}/properties/{property_id}/tags", json=body
        )
        return _unwrap(payload, "tag")

    @staticmethod
    def _to_error(response: httpx.Response, endpoint: str) -> AnytypeApiError:
        code, message = "unknown", response.text[:200]
        try:
            body = response.json()
            code = body.get("code", code)
            message = body.get("message", message)
        except ValueError:
            pass
        return AnytypeApiError(response.status_code, code, message, endpoint)
