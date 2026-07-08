"""MockAnytype: an in-process simulator of the Anytype local API.

Stands in for a live server as an ``httpx.MockTransport`` handler. Its
behavior is pinned to what the **WP1 spike measured** against a real
instance (API version 2025-11-08, 2026-06-21), so the test suite means
something:

* CRUD on ``/v1/spaces/{space}/objects`` (+ types, properties), with the
  ``data``/``pagination`` envelope and ``offset``/``limit`` *query* paging.
* ``GET /objects`` is the **unfiltered** sweep used by hydrate: it returns
  every non-archived object and honors a large page size (no 100 cap).
* ``POST /search`` is the **filtered/sorted** endpoint used by resync:
  ``types`` / ``filters`` / ``sort`` live in the request *body*, paging is
  via query params, and a page is **hard-capped at ``max_page_limit``**
  (100 live) regardless of the requested limit.
* Timestamps are ``date``-format **properties**, not top-level fields:
  ``created_date`` is stamped at creation; ``last_modified_date`` is stamped
  only on a *modification* (spike S3 -- it is absent until then). Filtering
  and sorting compare the *effective* stamp (last_modified else created).
* DELETE archives (soft delete). Archived objects are invisible to **both**
  list and search and cannot be enumerated (spike S4), so human deletions
  are only reconciled by a full hydrate -- there is deliberately no knob to
  make them visible.
* Bodies (A5/A7, ADR 010): created via the ``body`` key, echoed back as
  ``markdown``, updated via the ``markdown`` key in PATCH (wholesale
  replace; a ``body`` key in PATCH is silently ignored -- the documented
  create/update field-name mismatch). ``markdown`` appears **only** on the
  single-object ``GET``; list and search responses never carry it, so
  hydration code can never accidentally depend on it.
* The single-object GET's ``markdown`` is an **export** (A8): the built-in
  ``description`` property is prepended as the first line, while PATCH
  writes body blocks only -- so a raw GET -> PATCH round-trip duplicates
  the summary line. Reads must go through ``mapping.body_of``.
* Select/multi_select options are **tags** (ADR 012): ``GET/POST
  /properties/{propertyId}/tags`` (property ID, not key -- a key 404s
  "invalid property id"). A write referencing a tag that does not exist
  400s the whole request ("invalid select option"), on POST /objects and
  PATCH alike; values are accepted by tag id or key and stored/read as
  the inline tag envelope.
* 429 ``rate_limit_exceeded`` payloads via ``fail_next`` for retry tests.

This module and ``mapping.py`` are the two places our representation
assumptions live; keep them in lockstep with the spike findings.

Test conveniences: ``seed_object`` / ``edit_object_directly`` /
``archive_directly`` simulate a *human* editing the space in the Anytype
UI (they stamp timestamps without going through the client), and
``request_log`` records every call for budget assertions.

The clock is a monotonic microsecond counter rendered as an ISO timestamp,
so timestamp ordering and ``>=`` filters are deterministic and comparable
as strings.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from itertools import count
from typing import Any

import httpx

_SPACES = re.compile(r"^/v1/spaces$")
_SPACE = re.compile(r"^/v1/spaces/(?P<space>[^/]+)$")
_MEMBERS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/members$")
_OBJECTS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects$")
_OBJECT = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects/(?P<obj>[^/]+)$")
_SEARCH = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/search$")
_TYPES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/types$")
_PROPERTIES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties$")
_TAGS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties/(?P<prop>[^/]+)/tags$")
_CHATS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/chats$")
_CHAT_MESSAGES = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)/messages$"
)
_CHAT_MESSAGE = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)/messages/(?P<msg>[^/]+)$"
)
_CHAT_STREAM = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)/messages/stream$"
)

PROP_LAST_MODIFIED = "last_modified_date"
PROP_CREATED = "created_date"

# Comparison conditions supported by the search filter (only what we use).
_CONDITIONS: dict[str, Callable[[str, str], bool]] = {
    "greater_or_equal": lambda a, b: a >= b,
    "greater": lambda a, b: a > b,
    "less_or_equal": lambda a, b: a <= b,
    "less": lambda a, b: a < b,
    "equal": lambda a, b: a == b,
}


def _sse_frame(kind: str, message: dict[str, Any]) -> bytes:
    """One chat SSE frame exactly as the live server sends it (C5)."""
    data = json.dumps({"type": kind, "payload": {"message": message}})
    return f"event: {kind}\ndata: {data}\n\n".encode()


def _without_markdown(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List/search views of objects: everything but the body (A7).

    The live server returns ``markdown`` only on the single-object GET;
    bodies never ride the hydrate sweep or resync queries.
    """
    return [{k: v for k, v in o.items() if k != "markdown"} for o in items]


class MockAnytype:
    """Stateful fake of one Anytype instance holding one or more spaces."""

    def __init__(
        self,
        space_id: str = "space-1",
        *,
        space_name: str = "TestWorld",
        max_page_limit: int = 100,
        property_settle_patches: int = 0,
        tag_settle_writes: int = 0,
    ) -> None:
        self.space_id = space_id
        self.space_name = space_name
        self.max_page_limit = max_page_limit  # the POST /search per-page cap
        # Live finding (2026-07): a relation created via POST /properties is
        # not immediately usable -- a PATCH naming it 400s ("unknown property
        # key") for a short settle window. The knob makes the next N PATCHes
        # per fresh key reject the same way (0 = settled instantly).
        self.property_settle_patches = property_settle_patches
        # Same knob for freshly created TAGS (select options): the next N
        # object writes referencing a fresh tag 400 "invalid select option"
        # (inferred from a live flake; same shape as the relation window).
        self.tag_settle_writes = tag_settle_writes
        self._settling: dict[str, int] = {}
        self._tag_settling: dict[str, int] = {}
        self._objects: dict[str, dict[str, Any]] = {}
        self._types: dict[str, dict[str, Any]] = {}
        self._properties: dict[str, dict[str, Any]] = {}
        # Chat state (WP14, spike S10 quirks C1-C5 in chat.py). The caller's
        # own member id -- the live server attributes API posts to the
        # authenticated account's participant id.
        self.api_member_id = "mock-self"
        # Space membership (WP14 identity discovery, quirk C6): tests set
        # this to model solo-member (bot's own) vs shared spaces.
        self.members: list[dict[str, Any]] = []
        self._chats: dict[str, dict[str, Any]] = {}
        self._chat_messages: dict[str, list[dict[str, Any]]] = {}
        self._chat_listeners: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}
        self._chat_order = count(1)
        # Select/multi_select options ("tags", ADR 012), keyed by property ID
        # -- the live route rejects property keys ("invalid property id").
        self._tags: dict[str, list[dict[str, Any]]] = {}
        self._ids = count(1)
        self._clock = count(1)
        self._fail_queue: list[tuple[int, dict[str, Any]]] = []
        self.request_log: list[tuple[str, str]] = []

    # -- transport ---------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle_async)

    async def _handle_async(self, request: httpx.Request) -> httpx.Response:
        # Fidelity: real I/O always suspends the calling task. Without this
        # yield every mock request completes atomically and in-process
        # concurrency bugs (lost read-modify-write updates, ADR 009) are
        # invisible to the whole suite.
        await asyncio.sleep(0)
        return self.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.request_log.append((request.method, request.url.path))
        if self._fail_queue:
            status, body = self._fail_queue.pop(0)
            return httpx.Response(status, json=body)
        path = request.url.path
        if _SPACES.match(path):  # global, not space-scoped
            return self._handle_spaces(request)
        for pattern, handler in (
            (_CHAT_STREAM, self._handle_chat_stream),  # before _CHAT_MESSAGE
            (_CHAT_MESSAGE, self._handle_chat_message),
            (_CHAT_MESSAGES, self._handle_chat_messages),
            (_CHATS, self._handle_chats),
            (_MEMBERS, self._handle_members),
            (_OBJECT, self._handle_object),
            (_OBJECTS, self._handle_objects),
            (_SEARCH, self._handle_search),
            (_TYPES, self._handle_types),
            (_TAGS, self._handle_tags),
            (_PROPERTIES, self._handle_properties),
            (_SPACE, self._handle_space),
        ):
            match = pattern.match(path)
            if match:
                if match.group("space") != self.space_id:
                    return self._error(404, "space_not_found")
                return handler(request, match)
        return self._error(404, "not_found")

    # -- knobs & out-of-band ("human") mutations -----------------------------

    def fail_next(self, n: int = 1, status: int = 429) -> None:
        body = {"code": "rate_limit_exceeded",
                "message": "You have reached maximum request limit.",
                "object": "error", "status": status}
        self._fail_queue.extend([(status, body)] * n)

    def seed_object(
        self,
        type_key: str,
        name: str,
        properties: list[dict[str, Any]] | None = None,
        *,
        body: str = "",
    ) -> str:
        """Create an object as if a human did it in the UI (no API call)."""
        object_id = self._new_id()
        self._objects[object_id] = {
            "id": object_id,
            "name": name,
            "type": {"key": type_key},
            "archived": False,
            "properties": list(properties or []),
            "snippet": "",
            "markdown": body,
        }
        self._stamp(self._objects[object_id], PROP_CREATED)
        return object_id

    def edit_object_directly(self, object_id: str, **changes: Any) -> None:
        """Mutate an object as a human edit; stamps last_modified_date.

        ``changes`` may set ``name`` or ``properties`` (full entries list),
        ``set_property`` = a single entry dict to upsert by key, or
        ``markdown`` = the body as rewritten in the Anytype editor.
        """
        obj = self._objects[object_id]
        if "name" in changes:
            obj["name"] = changes["name"]
        if "properties" in changes:
            obj["properties"] = changes["properties"]
        if "set_property" in changes:
            self._upsert_property_entry(obj, changes["set_property"])
        if "markdown" in changes:
            obj["markdown"] = changes["markdown"]
        self._stamp(obj, PROP_LAST_MODIFIED)

    def archive_directly(self, object_id: str) -> None:
        self._objects[object_id]["archived"] = True
        self._stamp(self._objects[object_id], PROP_LAST_MODIFIED)

    def object(self, object_id: str) -> dict[str, Any]:
        return self._objects[object_id]

    # -- chat knobs (WP14) --------------------------------------------------

    def seed_chat(self, name: str = "Chat") -> str:
        """Create a chat as if a human did it in the UI (no API call)."""
        chat_id = self._new_id()
        self._chats[chat_id] = {
            "object": "object", "id": chat_id, "name": name, "layout": "chat",
        }
        self._chat_messages[chat_id] = []
        return chat_id

    def post_chat_message_directly(
        self, chat_id: str, creator: str, text: str
    ) -> str:
        """A message from another member -- the 'human is typing' analogue
        of ``edit_object_directly``. Live streams see ``message_added``."""
        return str(self._new_chat_message(chat_id, creator, text)["id"])

    def emit_chat_heartbeat(self, chat_id: str) -> None:
        """Push a ``: heartbeat`` comment to every open stream (C5)."""
        self._notify_chat(chat_id, {"kind": "heartbeat"})

    def end_chat_streams(self, chat_id: str) -> None:
        """Terminate every open stream for the chat (server drop)."""
        self._notify_chat(chat_id, None)

    def _new_chat_message(
        self,
        chat_id: str,
        creator: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # C7: attachments must be {"target", "type"} envelopes -- a bare
        # id list 400s on the live server.
        for entry in attachments or []:
            if not isinstance(entry, dict) or "target" not in entry:
                raise ValueError("attachments must be {'target', 'type'} dicts")
        message = {
            "id": self._new_id(),
            # Short lexicographically-increasing string, like live (C3).
            "order_id": f"o{next(self._chat_order):08d}",
            "creator": creator,
            "creator_name": creator,
            "created_at": next(self._clock),
            "modified_at": 0,
            "content": {"text": text, "style": "paragraph"},
            "attachments": list(attachments or []),
            "reactions": {},
            "pinned": False,
        }
        self._chat_messages[chat_id].append(message)
        self._notify_chat(chat_id, {"kind": "message_added", "message": message})
        return message

    def _notify_chat(self, chat_id: str, item: dict[str, Any] | None) -> None:
        for queue in list(self._chat_listeners.get(chat_id, [])):
            queue.put_nowait(item)

    # -- route handlers -------------------------------------------------------

    def _handle_objects(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            # Hydrate sweep: unfiltered, archived hidden, large pages honored.
            items = [o for o in self._objects.values() if not o["archived"]]
            return self._paginated(_without_markdown(items), request.url.params)
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("type_key") not in self._types:
                return self._error(400, "unknown_type")
            # Spike/incident finding (2026-06): the live API rejects a relation
            # (``objects``-format) property inlined in the create body with
            # ``400 bad input: unknown property key`` -- a freshly-created
            # relation is not yet attached to the object's type. Scalar gc_
            # properties inline fine; relations must be written via a follow-up
            # PATCH (which tolerates any space property). We model that here so
            # the adapter's PATCH-after-create contract is enforced in CI.
            for entry in body.get("properties", []):
                if entry.get("format") == "objects":
                    return httpx.Response(400, json={
                        "code": "bad_request",
                        "message": f'bad input: unknown property key: '
                                   f'"{entry.get("key")}"',
                        "object": "error", "status": 400,
                    })
            error = self._resolve_select_values(body.get("properties", []))
            if error is not None:
                return error
            object_id = self._new_id()
            self._objects[object_id] = {
                "id": object_id,
                "name": body.get("name", ""),
                "type": {"key": body["type_key"]},
                "archived": False,
                "icon": body.get("icon"),
                "properties": list(body.get("properties", [])),
                "snippet": "",
                "markdown": body.get("body", ""),  # A5: body in, markdown out
            }
            self._stamp(self._objects[object_id], PROP_CREATED)
            return httpx.Response(201, json={"object": self._objects[object_id]})
        return self._error(405, "method_not_allowed")

    def _handle_search(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method != "POST":
            return self._error(405, "method_not_allowed")
        body = json.loads(request.content) if request.content else {}
        items = [o for o in self._objects.values() if not o["archived"]]
        types = body.get("types")
        if types:
            items = [o for o in items if o["type"]["key"] in types]
        filt = body.get("filters")
        if filt:
            items = [o for o in items if self._passes_filter(o, filt)]
        sort = body.get("sort")
        if sort and sort.get("property_key") in (PROP_LAST_MODIFIED, PROP_CREATED):
            items.sort(key=self._effective, reverse=sort.get("direction") == "desc")
        return self._paginated(
            _without_markdown(items), request.url.params, cap=self.max_page_limit
        )

    def _handle_object(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        obj = self._objects.get(match.group("obj"))
        if obj is None:
            return self._error(404, "object_not_found")
        if request.method == "GET":
            return httpx.Response(200, json={"object": self._exported(obj)})
        if request.method == "PATCH":
            body = json.loads(request.content)
            for entry in body.get("properties", []):
                key = entry.get("key", "")
                if self._settling.get(key, 0) > 0:  # settle window still open
                    self._settling[key] -= 1
                    return httpx.Response(400, json={
                        "code": "bad_request",
                        "message": f'bad input: unknown property key: "{key}"',
                        "object": "error", "status": 400,
                    })
            error = self._resolve_select_values(body.get("properties", []))
            if error is not None:
                return error
            if "name" in body:
                obj["name"] = body["name"]
            for entry in body.get("properties", []):
                self._upsert_property_entry(obj, entry)  # REPLACE semantics (A4)
            # A7 (ADR 010, live-confirmed 2026-07-02): the update field for
            # body content is ``markdown`` -- a wholesale replace, combinable
            # with name/properties in one PATCH; empty string clears the body.
            # A ``body`` key in PATCH is silently ignored (200, content
            # unchanged) -- the create/update field-name mismatch is the
            # documented gotcha the original S6 spike tripped on.
            if "markdown" in body:
                obj["markdown"] = body["markdown"]
            self._stamp(obj, PROP_LAST_MODIFIED)
            return httpx.Response(200, json={"object": obj})
        if request.method == "DELETE":
            obj["archived"] = True
            self._stamp(obj, PROP_LAST_MODIFIED)
            return httpx.Response(200, json={"object": obj})
        return self._error(405, "method_not_allowed")

    def _handle_space(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        return httpx.Response(
            200, json={"space": {"id": self.space_id, "name": self.space_name}}
        )

    def _handle_spaces(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        return self._paginated(
            [{"object": "anytype.space", "id": self.space_id,
              "name": self.space_name}],
            request.url.params,
        )

    def _handle_members(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        return self._paginated(list(self.members), request.url.params)

    # -- chat routes (WP14; quirks C1-C5 in chat.py, pinned by spike S10) ----

    def _handle_chats(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._chats.values()), request.url.params)
        if request.method == "POST":
            body = json.loads(request.content)
            chat_id = self.seed_chat(str(body.get("name", "")))
            return httpx.Response(201, json={"object": self._chats[chat_id]})
        return self._error(405, "method_not_allowed")

    def _handle_chat_messages(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        chat_id = match.group("chat")
        if chat_id not in self._chats:
            return self._error(404, "chat_not_found")
        if request.method == "GET":
            # C2: a recency WINDOW, oldest-first -- bare `messages` key, no
            # pagination block, offset ignored (live-confirmed, S10).
            limit = int(request.url.params.get("limit", 100))
            return httpx.Response(
                200, json={"messages": self._chat_messages[chat_id][-limit:]}
            )
        if request.method == "POST":
            body = json.loads(request.content)
            raw_attachments = body.get("attachments")
            if raw_attachments is not None and any(
                not isinstance(e, dict) for e in raw_attachments
            ):
                return self._error(400, "bad_request")  # C7: envelopes only
            message = self._new_chat_message(
                chat_id, self.api_member_id, str(body.get("text", "")),
                attachments=raw_attachments,
            )
            # C1: flat message_id, no envelope key.
            return httpx.Response(201, json={"message_id": message["id"]})
        return self._error(405, "method_not_allowed")

    def _handle_chat_message(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        chat_id, message_id = match.group("chat"), match.group("msg")
        if chat_id not in self._chats:
            return self._error(404, "chat_not_found")
        messages = self._chat_messages[chat_id]
        message = next((m for m in messages if m["id"] == message_id), None)
        if message is None:
            return self._error(404, "message_not_found")
        if request.method == "PATCH":
            body = json.loads(request.content)
            message["content"]["text"] = str(body.get("text", ""))
            message["modified_at"] = next(self._clock)
            self._notify_chat(
                chat_id, {"kind": "message_updated", "message": message}
            )
            return httpx.Response(200, json={})
        if request.method == "DELETE":
            messages.remove(message)
            self._notify_chat(
                chat_id, {"kind": "message_deleted", "message": message}
            )
            return httpx.Response(200, json={})
        return self._error(405, "method_not_allowed")

    def _handle_chat_stream(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        chat_id = match.group("chat")
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        if chat_id not in self._chats:
            return self._error(404, "chat_not_found")
        backlog = list(self._chat_messages[chat_id])
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._chat_listeners.setdefault(chat_id, []).append(queue)

        async def frames() -> Any:
            try:
                for message in backlog:  # C5: history replays as ordinary adds
                    yield _sse_frame("message_added", message)
                while True:
                    item = await queue.get()
                    if item is None:  # server-side drop (end_chat_streams)
                        return
                    if item["kind"] == "heartbeat":
                        yield b": heartbeat\n\n"
                    else:
                        yield _sse_frame(item["kind"], item["message"])
            finally:
                self._chat_listeners[chat_id].remove(queue)

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=frames()
        )

    def _handle_types(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._types.values()), request.url.params)
        body = json.loads(request.content)
        if not body.get("key") or not body.get("plural_name"):
            # The live API requires both (spike: a missing plural_name 400s).
            return self._error(400, "bad_request")
        # An inline ``properties`` list attaches the fields to the type AND
        # creates them as space properties (live-confirmed 2026-07-06).
        for entry in body.get("properties", []):
            self._properties.setdefault(
                entry["key"], {"id": self._new_id(), **entry}
            )
        self._types[body["key"]] = {"id": self._new_id(), **body}
        return httpx.Response(201, json={"type": self._types[body["key"]]})

    def _handle_properties(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._properties.values()), request.url.params)
        body = json.loads(request.content)
        self._properties[body["key"]] = {"id": self._new_id(), **body}
        if self.property_settle_patches > 0 and body.get("format") == "objects":
            self._settling[body["key"]] = self.property_settle_patches
        return httpx.Response(201, json={"property": self._properties[body["key"]]})

    def _handle_tags(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """Select/multi_select options (ADR 012). Addressed by property ID --
        the live route 404s on a property KEY ("invalid property id")."""
        prop = next(
            (p for p in self._properties.values() if p["id"] == match.group("prop")),
            None,
        )
        if prop is None:
            return httpx.Response(404, json={
                "code": "object_not_found", "message": "invalid property id",
                "object": "error", "status": 404,
            })
        tags = self._tags.setdefault(prop["id"], [])
        if request.method == "GET":
            return self._paginated(tags, request.url.params)
        if request.method == "POST":
            body = json.loads(request.content)
            if not body.get("color"):
                # CreateTagRequest.Color is REQUIRED (live-confirmed).
                return httpx.Response(400, json={
                    "code": "bad_request", "object": "error", "status": 400,
                    "message": "Key: 'CreateTagRequest.Color' Error:Field "
                               "validation for 'Color' failed on the "
                               "'required' tag",
                })
            name = body.get("name", "")
            tag = {
                "object": "tag", "id": self._new_id(),
                "key": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
                "name": name, "color": body["color"],
            }
            tags.append(tag)
            if self.tag_settle_writes > 0:
                self._tag_settling[tag["key"]] = self.tag_settle_writes
            return httpx.Response(201, json={"tag": tag})
        return self._error(405, "method_not_allowed")

    def _resolve_select_values(
        self, entries: list[dict[str, Any]]
    ) -> httpx.Response | None:
        """Validate + normalize select/multi_select entries in a write.

        Live behavior (ADR 012 spikes): the value must reference an EXISTING
        tag by id or key -- a bare unknown string 400s the whole request, on
        POST /objects and PATCH alike. Stored values become the inline tag
        envelope that reads return."""
        def find(
            value: Any, tags: list[dict[str, Any]]
        ) -> dict[str, Any] | None:
            if isinstance(value, dict):  # already an envelope (seeded)
                return value
            return next((t for t in tags if value in (t["id"], t["key"])), None)

        for entry in entries:
            fmt = entry.get("format")
            if fmt not in ("select", "multi_select"):
                continue
            prop = self._properties.get(entry.get("key", ""))
            tags = self._tags.get(prop["id"], []) if prop else []

            if fmt == "select":
                raw = entry.get("select")
                if raw is None:
                    continue
                tag = find(raw, tags)
                if tag is not None and self._tag_settling.get(tag["key"], 0) > 0:
                    self._tag_settling[tag["key"]] -= 1
                    tag = None  # settle window still open: reject as invalid
                if tag is None:
                    return httpx.Response(400, json={
                        "code": "bad_request", "object": "error", "status": 400,
                        "message": f'bad input: invalid select option for '
                                   f'"{entry.get("key")}": {raw}',
                    })
                entry["select"] = tag
            else:
                resolved = []
                for raw in entry.get("multi_select") or []:
                    tag = find(raw, tags)
                    if tag is None:
                        return httpx.Response(400, json={
                            "code": "bad_request", "object": "error", "status": 400,
                            "message": f'bad input: invalid multi_select option '
                                       f'for "{entry.get("key")}": {raw}',
                        })
                    resolved.append(tag)
                entry["multi_select"] = resolved
        return None

    # -- helpers ---------------------------------------------------------------

    def _passes_filter(self, obj: dict[str, Any], filt: dict[str, Any]) -> bool:
        if "and" in filt:
            return all(self._passes_filter(obj, f) for f in filt["and"])
        if "or" in filt:
            return any(self._passes_filter(obj, f) for f in filt["or"])
        if filt.get("property_key") in (PROP_LAST_MODIFIED, PROP_CREATED):
            compare = _CONDITIONS.get(filt.get("condition", ""))
            if compare is not None:
                return compare(self._effective(obj), str(filt.get("value", "")))
        return True  # unknown property/condition: don't hide the object

    def _effective(self, obj: dict[str, Any]) -> str:
        dates = {
            p["key"]: p.get("date")
            for p in obj["properties"]
            if p.get("format") == "date"
        }
        return str(dates.get(PROP_LAST_MODIFIED) or dates.get(PROP_CREATED) or "")

    @staticmethod
    def _exported(obj: dict[str, Any]) -> dict[str, Any]:
        """The single-object GET view: markdown is an EXPORT, not the raw
        body (A8) -- the built-in ``description`` property is prepended as
        the first line. PATCH, by contrast, writes body blocks only; the
        asymmetry is the round-trip trap ``mapping.body_of`` defuses."""
        description = next(
            (str(p.get("text") or "") for p in obj["properties"]
             if p.get("key") == "description" and p.get("format") == "text"),
            "",
        )
        if not description:
            return obj
        return {**obj, "markdown": f"{description}\n{obj.get('markdown', '')}"}

    def _paginated(
        self, items: list[dict[str, Any]], params: httpx.QueryParams, *, cap: int | None = None
    ) -> httpx.Response:
        offset = int(params.get("offset", 0))
        requested = int(params.get("limit", 100))
        limit = min(requested, cap) if cap is not None else requested
        page = items[offset : offset + limit]
        return httpx.Response(200, json={
            "data": page,
            "pagination": {
                "total": len(items), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(items),
            },
        })

    def _stamp(self, obj: dict[str, Any], key: str) -> None:
        self._upsert_property_entry(
            obj, {"key": key, "format": "date", "date": self._now()}
        )

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
