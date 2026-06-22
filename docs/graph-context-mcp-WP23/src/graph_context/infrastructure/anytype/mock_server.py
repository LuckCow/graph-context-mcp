"""MockAnytype: an in-process simulator of the Anytype local API.

Stands in for a live server (none is available in this environment) by
implementing the *documented* behavior of API version 2025-11-08 as an
``httpx.MockTransport`` handler:

* CRUD on ``/v1/spaces/{space}/objects`` (+ types, properties), with the
  ``data``/``pagination`` envelope and offset/limit paging.
* List filtering by ``type`` (type key) and ``last_modified_date[gte]``
  (the documented dynamic-filter style).
* DELETE archives (soft delete); archived objects are hidden from lists
  unless ``archived_visible_in_lists=True`` -- this is spike question S4
  made into a knob, so tests cover both possible live-server answers.
* PATCH **replaces** each provided property's value wholesale, including
  multi-value relation lists (mapping assumption A4 -- also a knob-worthy
  spike question; replace is the conservative assumption).
* 429 ``rate_limit_exceeded`` payloads via ``fail_next`` for retry tests.

This module and ``mapping.py`` are the two places our representation
assumptions live. When the live-server spike contradicts something here,
update both in the same PR so the suite keeps meaning something.

Test conveniences: ``seed_object`` / ``edit_object_directly`` /
``archive_directly`` simulate a *human* editing the space in the Anytype
UI (they bump ``last_modified_date`` without going through the client),
and ``request_log`` records every call for budget assertions.

The clock is a monotonic microsecond counter rendered as an ISO timestamp,
so ``last_modified_date`` ordering and ``[gte]`` filters are deterministic
and comparable as strings.
"""

from __future__ import annotations

import json
import re
from itertools import count
from typing import Any

import httpx

_OBJECTS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects$")
_OBJECT = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects/(?P<obj>[^/]+)$")
_TYPES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/types$")
_PROPERTIES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties$")

MODIFIED_GTE = "last_modified_date[gte]"


class MockAnytype:
    """Stateful fake of one Anytype instance holding one or more spaces."""

    def __init__(
        self,
        space_id: str = "space-1",
        *,
        archived_visible_in_lists: bool = False,
        max_page_limit: int = 100,
    ) -> None:
        self.space_id = space_id
        self.archived_visible_in_lists = archived_visible_in_lists
        self.max_page_limit = max_page_limit
        self._objects: dict[str, dict[str, Any]] = {}
        self._types: dict[str, dict[str, Any]] = {}
        self._properties: dict[str, dict[str, Any]] = {}
        self._ids = count(1)
        self._clock = count(1)
        self._fail_queue: list[tuple[int, dict[str, Any]]] = []
        self.request_log: list[tuple[str, str]] = []

    # -- transport ---------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.request_log.append((request.method, request.url.path))
        if self._fail_queue:
            status, body = self._fail_queue.pop(0)
            return httpx.Response(status, json=body)
        path = request.url.path
        for pattern, handler in (
            (_OBJECT, self._handle_object),
            (_OBJECTS, self._handle_objects),
            (_TYPES, self._handle_types),
            (_PROPERTIES, self._handle_properties),
        ):
            match = pattern.match(path)
            if match:
                if match.group("space") != self.space_id:
                    return self._error(404, "space_not_found")
                return handler(request, match)
        return self._error(404, "not_found")

    # -- knobs & out-of-band ("human") mutations -----------------------------

    def fail_next(self, n: int = 1, status: int = 429) -> None:
        body = {"code": "rate_limit_exceeded", "message": "Rate limit exceeded",
                "object": "error", "status": status}
        self._fail_queue.extend([(status, body)] * n)

    def seed_object(
        self,
        type_key: str,
        name: str,
        properties: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create an object as if a human did it in the UI (no API call)."""
        object_id = self._new_id()
        self._objects[object_id] = {
            "id": object_id,
            "name": name,
            "type": {"key": type_key},
            "archived": False,
            "properties": properties or [],
            "snippet": "",
            "last_modified_date": self._now(),
        }
        return object_id

    def edit_object_directly(self, object_id: str, **changes: Any) -> None:
        """Mutate an object as a human edit; bumps last_modified_date.

        ``changes`` may set ``name`` or ``properties`` (full entries list)
        or ``set_property`` = a single entry dict to upsert by key.
        """
        obj = self._objects[object_id]
        if "name" in changes:
            obj["name"] = changes["name"]
        if "properties" in changes:
            obj["properties"] = changes["properties"]
        if "set_property" in changes:
            self._upsert_property_entry(obj, changes["set_property"])
        obj["last_modified_date"] = self._now()

    def archive_directly(self, object_id: str) -> None:
        self._objects[object_id]["archived"] = True
        self._objects[object_id]["last_modified_date"] = self._now()

    def object(self, object_id: str) -> dict[str, Any]:
        return self._objects[object_id]

    # -- route handlers -------------------------------------------------------

    def _handle_objects(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            items = [
                o for o in self._objects.values()
                if (self.archived_visible_in_lists or not o["archived"])
                and self._passes_filters(o, request.url.params)
            ]
            return self._paginated(items, request.url.params)
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("type_key") not in self._types:
                return self._error(400, "unknown_type")
            object_id = self._new_id()
            self._objects[object_id] = {
                "id": object_id,
                "name": body.get("name", ""),
                "type": {"key": body["type_key"]},
                "archived": False,
                "properties": body.get("properties", []),
                "snippet": "",
                "markdown": body.get("body", ""),  # A5: body in, markdown out
                "last_modified_date": self._now(),
            }
            return httpx.Response(201, json={"object": self._objects[object_id]})
        return self._error(405, "method_not_allowed")

    def _handle_object(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        obj = self._objects.get(match.group("obj"))
        if obj is None:
            return self._error(404, "object_not_found")
        if request.method == "GET":
            return httpx.Response(200, json={"object": obj})
        if request.method == "PATCH":
            body = json.loads(request.content)
            if "body" in body:  # A6: documented limitation -- bodies are write-once
                return self._error(400, "body_patch_unsupported")
            if "name" in body:
                obj["name"] = body["name"]
            for entry in body.get("properties", []):
                self._upsert_property_entry(obj, entry)  # REPLACE semantics (A4)
            obj["last_modified_date"] = self._now()
            return httpx.Response(200, json={"object": obj})
        if request.method == "DELETE":
            obj["archived"] = True
            obj["last_modified_date"] = self._now()
            return httpx.Response(200, json={"object": obj})
        return self._error(405, "method_not_allowed")

    def _handle_types(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._types.values()), request.url.params)
        body = json.loads(request.content)
        self._types[body["key"]] = {"id": self._new_id(), **body}
        return httpx.Response(201, json={"type": self._types[body["key"]]})

    def _handle_properties(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._properties.values()), request.url.params)
        body = json.loads(request.content)
        self._properties[body["key"]] = {"id": self._new_id(), **body}
        return httpx.Response(201, json={"property": self._properties[body["key"]]})

    # -- helpers ---------------------------------------------------------------

    def _passes_filters(self, obj: dict[str, Any], params: httpx.QueryParams) -> bool:
        type_filter = params.get("type")
        if type_filter and obj["type"]["key"] != type_filter:
            return False
        since = params.get(MODIFIED_GTE)
        return not (since and obj["last_modified_date"] < since)

    def _paginated(self, items: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
        offset = int(params.get("offset", 0))
        limit = min(int(params.get("limit", 100)), self.max_page_limit)
        page = items[offset : offset + limit]
        return httpx.Response(200, json={
            "data": page,
            "pagination": {
                "total": len(items), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(items),
            },
        })

    @staticmethod
    def _upsert_property_entry(obj: dict[str, Any], entry: dict[str, Any]) -> None:
        properties = [p for p in obj["properties"] if p.get("key") != entry.get("key")]
        properties.append(entry)
        obj["properties"] = properties

    def _new_id(self) -> str:
        return f"any-{next(self._ids):05d}"

    def _now(self) -> str:
        return f"2026-01-01T00:00:00.{next(self._clock):06d}Z"

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={
            "code": code, "message": code.replace("_", " "), "object": "error",
            "status": status,
        })
