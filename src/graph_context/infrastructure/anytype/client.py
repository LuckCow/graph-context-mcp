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
        retry: bool = True,
    ) -> dict[str, Any]:
        """``retry=False`` is for endpoints whose 5xx is a known SEMANTIC
        signal rather than transience (S9: a sourceless set's execution
        500s permanently) -- retrying those just burns the whole backoff
        ladder before the caller's skip-handling runs."""
        last_error: AnytypeApiError | None = None
        max_retries = self._config.max_retries if retry else 0
        for attempt in range(max_retries + 1):
            self.request_count += 1
            try:
                response = await self._http.request(method, path, params=params, json=json)
            except httpx.HTTPError as err:
                # Transport-level failure (connection refused, timeout, ...):
                # translate so callers see one error family, per the module
                # contract. status=0 marks "no HTTP response at all".
                raise AnytypeApiError(0, "transport", str(err), path) from err
            if response.status_code < 400:
                return response.json() if response.content else {}
            error = self._to_error(response, path)
            if response.status_code not in _RETRYABLE_STATUSES:
                raise error
            last_error = error
            if attempt < max_retries:
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
    def space_id(self) -> str:
        """The bound space id (deep links in the connections footer need it)."""
        return self._config.space_id

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

    def list_members(self) -> AsyncIterator[dict[str, Any]]:
        """Space members (ordinary paginated ``data`` envelope).

        The ONLY enumeration of participants (S11): list/search never
        return participant-layout objects, though the single-object GET
        serves them like any object.
        """
        return self.paginate(f"{self._space}/members")

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

    def list_templates(self, type_id: str) -> AsyncIterator[dict[str, Any]]:
        """A type's templates (ordinary paginated ``data`` envelope). Type object
        ID, not key. Templates are UI-authored: there is no create counterpart
        (POST of a ``template``-typed object 500s -- see the templates spike)."""
        return self.paginate(f"{self._space}/types/{type_id}/templates")

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

    # -- lists / set views (WP13 view param; spike S9) ---------------------

    def list_views(self, list_id: str) -> AsyncIterator[dict[str, Any]]:
        """A set/collection's views (ordinary paginated ``data`` envelope).

        S9: each view carries machine-readable ``filters`` and ``sorts``
        once a human has configured the set's source in the desktop; the
        payload quirks are quarantined in ``view_catalog.py``.
        """
        return self.paginate(f"{self._space}/lists/{list_id}/views")

    async def sample_view_objects(
        self, list_id: str, view_id: str, *, limit: int = 1
    ) -> list[dict[str, Any]]:
        """A few objects from a view's server-side execution.

        Used ONLY to infer a set's source type (the set object does not
        expose it -- S9 addendum); the query itself runs client-side.
        No retry: a sourceless set 500s PERMANENTLY here (S9), and the
        catalog treats that error as "skip this view" -- retrying it just
        stalls catalog load for the whole backoff ladder (live-caught:
        one shell set cost the E2E suite 8.5 minutes).
        """
        payload = await self.request(
            "GET",
            f"{self._space}/lists/{list_id}/views/{view_id}/objects",
            params={"limit": limit},
            retry=False,
        )
        data: list[dict[str, Any]] = payload.get("data", [])
        return data

    # -- chat (WP14; payload shapes live in chat.py, spike S10) ------------

    def list_chats(self) -> AsyncIterator[dict[str, Any]]:
        """Chat objects in the space (ordinary paginated ``data`` envelope)."""
        return self.paginate(f"{self._space}/chats")

    async def create_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", f"{self._space}/chats", json=body)
        return _unwrap(payload, "object")

    async def list_chat_messages(
        self, chat_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """The chat's most recent messages, oldest-first within the window.

        S10: this endpoint is NOT paginated -- the response is a bare
        ``{"messages": [...]}`` with no pagination block and ``offset`` is
        ignored, so it returns a limit-bounded recency window, not a page.
        """
        payload = await self.request(
            "GET", f"{self._space}/chats/{chat_id}/messages",
            params={"limit": limit},
        )
        messages: list[dict[str, Any]] = payload.get("messages", [])
        return messages

    async def create_chat_message(
        self, chat_id: str, body: dict[str, Any]
    ) -> str:
        """Post a message; returns the new message id (S10: the 201 body is
        a flat ``{"message_id": ...}``, unlike every other write envelope)."""
        payload = await self.request(
            "POST", f"{self._space}/chats/{chat_id}/messages", json=body
        )
        message_id: str = payload["message_id"]
        return message_id

    async def edit_chat_message(
        self, chat_id: str, message_id: str, body: dict[str, Any]
    ) -> None:
        await self.request(
            "PATCH", f"{self._space}/chats/{chat_id}/messages/{message_id}",
            json=body,
        )

    async def delete_chat_message(self, chat_id: str, message_id: str) -> None:
        await self.request(
            "DELETE", f"{self._space}/chats/{chat_id}/messages/{message_id}"
        )

    async def stream_lines(
        self, path: str, *, heartbeat_seconds: int = 30
    ) -> AsyncIterator[str]:
        """Yield raw SSE lines from a ``text/event-stream`` endpoint.

        Auth/version headers ride the shared client. The read timeout is
        tied to the requested heartbeat (2x + margin), so a half-dead
        stream raises instead of hanging forever -- the caller's reconnect
        loop is the recovery path. Framing is parsed in ``chat.py``.
        """
        timeout = httpx.Timeout(
            self._config.timeout_seconds, read=heartbeat_seconds * 2 + 5
        )
        try:
            async with self._http.stream(
                "GET",
                path,
                headers={"Anytype-Heartbeat-Seconds": str(heartbeat_seconds)},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._to_error(response, path)
                async for line in response.aiter_lines():
                    yield line
        except httpx.HTTPError as err:
            raise AnytypeApiError(0, "transport", str(err), path) from err

    def stream_chat_messages(
        self, chat_id: str, *, heartbeat_seconds: int = 30
    ) -> AsyncIterator[str]:
        return self.stream_lines(
            f"{self._space}/chats/{chat_id}/messages/stream",
            heartbeat_seconds=heartbeat_seconds,
        )

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
