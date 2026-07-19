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
* Space members (S11, live-confirmed 2026-07-12): ``GET /members`` returns
  member envelopes; each member's *participant object* answers the
  single-object GET like any object but is invisible to **both** list and
  search, and ``objects``-format relations accept participant ids as
  targets (Anytype's own Assignee mechanism). Seed via ``seed_member``.
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
* The PATCH ``markdown`` importer flattens a FIRST-line heading to plain
  text (A9, live-confirmed 2026-07-11); headings on later lines survive.
* The body round trip drops fenced code blocks' LANGUAGE TAGS (A13,
  live-confirmed 2026-07-19): a ```python block reads back as a bare
  ``` fence on the markdown export -- why ``rules.extract_script``
  accepts untagged fences (WP32).
* Template objects are readable via the single-object GET, ``markdown``
  carrying the template's body scaffold (live-confirmed by the templates
  spike) -- how the repository detects scaffolded types (ADR 013
  amendment).
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
import email.parser
import email.policy
import json
import mimetypes
import re
from collections.abc import Callable
from itertools import count
from typing import Any

import httpx

from graph_context.infrastructure.anytype.marks import utf16_len

_SPACES = re.compile(r"^/v1/spaces$")
_SPACE = re.compile(r"^/v1/spaces/(?P<space>[^/]+)$")
_MEMBERS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/members$")
_OBJECTS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects$")
_OBJECT = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/objects/(?P<obj>[^/]+)$")
_SEARCH = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/search$")
_TYPES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/types$")
_TYPE = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/types/(?P<type>[^/]+)$")
_TEMPLATES = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/types/(?P<type>[^/]+)/templates$"
)
_PROPERTIES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties$")
_PROPERTY = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties/(?P<prop>[^/]+)$")
_TAGS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/properties/(?P<prop>[^/]+)/tags$")
_LIST_VIEWS = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/lists/(?P<list>[^/]+)/views$"
)
_LIST_VIEW_OBJECTS = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/lists/(?P<list>[^/]+)/views/(?P<view>[^/]+)/objects$"
)
_FILES = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/files$")
_FILE = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/files/(?P<file>[^/]+)$")
_CHATS = re.compile(r"^/v1/spaces/(?P<space>[^/]+)/chats$")
_CHAT_MESSAGES = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)/messages$"
)
_CHAT_MESSAGE = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)/messages/(?P<msg>[^/]+)$"
)
_CHAT_REACTIONS = re.compile(
    r"^/v1/spaces/(?P<space>[^/]+)/chats/(?P<chat>[^/]+)"
    r"/messages/(?P<msg>[^/]+)/reactions$"
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


def _parse_multipart_file(
    request: httpx.Request,
) -> tuple[str | None, bytes | None]:
    """The ``file`` field of a multipart upload (C10), or ``(None, None)``
    when the request carries none -- the live server's 400 case."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return None, None
    parser = email.parser.BytesParser(policy=email.policy.HTTP)
    message = parser.parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    for part in message.iter_parts():
        if 'name="file"' in str(part.get("Content-Disposition", "")):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return part.get_filename() or "", payload
    return None, None


def _sse_frame(kind: str, message: dict[str, Any]) -> bytes:
    """One chat SSE frame exactly as the live server sends it (C5)."""
    data = json.dumps({"type": kind, "payload": {"message": message}})
    return f"event: {kind}\ndata: {data}\n\n".encode()


def _sse_payload_frame(kind: str, payload: dict[str, Any]) -> bytes:
    """A frame whose payload is NOT a message envelope (C12:
    ``reactions_updated`` sends ``{"id", "reactions"}`` bare)."""
    data = json.dumps({"type": kind, "payload": payload})
    return f"event: {kind}\ndata: {data}\n\n".encode()


def _without_markdown(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List/search views of objects: everything but the body (A7).

    The live server returns ``markdown`` only on the single-object GET;
    bodies never ride the hydrate sweep or resync queries.
    """
    return [{k: v for k, v in o.items() if k != "markdown"} for o in items]


_LEADING_HEADING = re.compile(r"^#{1,6}\s+")


def _flatten_first_line_heading(markdown: str) -> str:
    """A9 (live-confirmed 2026-07-11): the PATCH importer strips heading
    markup from the body's first line; later headings survive."""
    first, sep, rest = markdown.partition("\n")
    return _LEADING_HEADING.sub("", first) + sep + rest


_FENCE_LANGUAGE = re.compile(r"(?m)^(```)[ \t]*\S.*$")


def _strip_fence_language(markdown: str) -> str:
    """A13 (live-confirmed 2026-07-19): the body round trip drops fenced
    code blocks' language tags -- a ```python block written at create or
    PATCH reads back as a bare ``` fence on the markdown export. (Why
    ``rules.extract_script`` accepts untagged fences.)"""
    return _FENCE_LANGUAGE.sub(r"\1", markdown)


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
        # Type templates (UI-authored; the API can't mint them, so tests seed
        # via seed_template). Keyed by type OBJECT id, plus a by-id index for
        # applying a template on create.
        self._templates: dict[str, list[dict[str, Any]]] = {}
        self._templates_by_id: dict[str, dict[str, Any]] = {}
        # Chat state (WP14, spike S10 quirks C1-C5 in chat.py). The caller's
        # own member id -- the live server attributes API posts to the
        # authenticated account's participant id.
        self.api_member_id = "mock-self"
        # C12: reactions carry ACCOUNT identities, not member ids -- this
        # is what the API caller's toggles record.
        self.api_identity = "mock-self-identity"
        # Space membership (WP14 identity discovery, quirk C6): tests set
        # this to model solo-member (bot's own) vs shared spaces.
        self.members: list[dict[str, Any]] = []
        # Set views (WP13, spike S9 shapes): set object id -> view dicts,
        # and the set's source type (None = sourceless, execution 500s).
        self._set_views: dict[str, list[dict[str, Any]]] = {}
        self._set_sources: dict[str, str | None] = {}
        self._chats: dict[str, dict[str, Any]] = {}
        self._chat_messages: dict[str, list[dict[str, Any]]] = {}
        # File contents by object id (WP23, quirk C10): the object half
        # lives in self._objects like the live server's real file objects.
        self._file_bytes: dict[str, tuple[bytes, str]] = {}  # id -> (bytes, media)
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
            (_CHAT_REACTIONS, self._handle_chat_reactions),  # before _CHAT_MESSAGE
            (_CHAT_MESSAGE, self._handle_chat_message),
            (_CHAT_MESSAGES, self._handle_chat_messages),
            (_CHATS, self._handle_chats),
            (_FILE, self._handle_file),  # before _FILES (more specific)
            (_FILES, self._handle_files),
            (_LIST_VIEW_OBJECTS, self._handle_list_view_objects),
            (_LIST_VIEWS, self._handle_list_views),
            (_MEMBERS, self._handle_members),
            (_OBJECT, self._handle_object),
            (_OBJECTS, self._handle_objects),
            (_SEARCH, self._handle_search),
            (_TEMPLATES, self._handle_templates),  # before _TYPES (more specific)
            (_TYPE, self._handle_type),
            (_TYPES, self._handle_types),
            (_TAGS, self._handle_tags),  # before _PROPERTY (more specific)
            (_PROPERTY, self._handle_property),
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

    def seed_member(
        self,
        name: str,
        *,
        identity: str = "",
        role: str = "editor",
        status: str = "active",
    ) -> str:
        """Add a space member as the live server represents one (S11).

        The member envelope rides ``GET /members``; its participant OBJECT
        answers the single-object GET like any object but is invisible to
        list and search (the live blind spot member reflection works
        around). Returns the participant/member id.
        """
        member_identity = identity or f"ident{self._new_id()}"
        space_part = self.space_id.replace(".", "_")
        member_id = f"_participant_{space_part}_{member_identity}"
        self.members.append({
            "object": "member", "id": member_id, "name": name,
            "icon": None, "identity": member_identity, "global_name": "",
            "status": status, "role": role,
        })
        self._types.setdefault("participant", {
            "id": self._new_id(), "key": "participant",
            "name": "Space member", "plural_name": "Space members",
            "layout": "participant", "properties": [],
        })
        self._objects[member_id] = {
            "id": member_id,
            "name": name,
            "type": {"key": "participant"},
            "layout": "participant",
            "archived": False,
            "properties": [],
            "snippet": "",
            "markdown": "",
        }
        self._stamp(self._objects[member_id], PROP_CREATED)
        return member_id

    def seed_template(
        self,
        type_key: str,
        *,
        body: str = "",
        default_properties: list[dict[str, Any]] | None = None,
    ) -> str:
        """Author a type template as a human would in the UI (the API can't
        mint them). ``default_properties`` are pre-resolved property entries
        (e.g. a select envelope) the template applies on create; ``body`` is
        the template's scaffold, prepended to a caller body on create."""
        type_id = self._types[type_key]["id"]
        template_id = self._new_id()
        template = {
            "id": template_id,
            "name": f"{type_key} template",
            "body": body,
            "default_properties": list(default_properties or []),
        }
        self._templates.setdefault(type_id, []).append(template)
        self._templates_by_id[template_id] = template
        return template_id

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
            # A13 applies to human edits too: the editor's code blocks
            # export without language tags.
            obj["markdown"] = _strip_fence_language(changes["markdown"])
        self._stamp(obj, PROP_LAST_MODIFIED)

    def archive_directly(self, object_id: str) -> None:
        self._objects[object_id]["archived"] = True
        self._stamp(self._objects[object_id], PROP_LAST_MODIFIED)

    def object(self, object_id: str) -> dict[str, Any]:
        return self._objects[object_id]

    # -- set-view knobs (WP13) ----------------------------------------------

    def seed_set(
        self,
        name: str,
        *,
        source_type_key: str | None,
        views: list[dict[str, Any]],
    ) -> str:
        """Create a Set as a human would: source + configured views.

        ``source_type_key=None`` models an API-created SOURCELESS set —
        its views list fine but view execution 500s (spike S9).
        """
        set_id = self.seed_object("set", name)
        self._set_views[set_id] = list(views)
        self._set_sources[set_id] = source_type_key
        return set_id

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
        self, chat_id: str, creator: str, text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        """A message from another member -- the 'human is typing' analogue
        of ``edit_object_directly``. Live streams see ``message_added``.
        ``attachments`` are C7/C10 envelopes (a human dropping a file)."""
        return str(self._new_chat_message(
            chat_id, creator, text, attachments=attachments
        )["id"])

    def chat_messages(self, chat_id: str) -> list[dict[str, Any]]:
        """The chat's delivered messages, oldest first (assertion surface --
        the read-side twin of ``post_chat_message_directly``). Copies, so
        a test cannot accidentally mutate the store."""
        return [dict(m) for m in self._chat_messages[chat_id]]

    def react_directly(
        self, chat_id: str, message_id: str, emoji: str, identity: str
    ) -> None:
        """A reaction from another member -- the 'human reacted' analogue
        of ``post_chat_message_directly`` (C12). Toggle semantics, like
        live; open streams see ``reactions_updated``."""
        message = next(
            m for m in self._chat_messages[chat_id] if m["id"] == message_id
        )
        self._toggle_reaction(chat_id, message, emoji, identity)

    def _toggle_reaction(
        self, chat_id: str, message: dict[str, Any], emoji: str, identity: str
    ) -> None:
        reactions: dict[str, list[str]] = message.setdefault("reactions", {})
        holders = reactions.setdefault(emoji, [])
        if identity in holders:
            holders.remove(identity)
            if not holders:
                del reactions[emoji]
        else:
            holders.append(identity)
        # C12: the frame carries id + reactions bare, no message envelope.
        self._notify_chat(chat_id, {
            "kind": "reactions_updated",
            "payload": {"id": message["id"], "reactions": reactions},
        })

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
        marks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # C7: attachments must be {"target", "type"} envelopes -- a bare
        # id list 400s on the live server.
        for entry in attachments or []:
            if not isinstance(entry, dict) or "target" not in entry:
                raise ValueError("attachments must be {'target', 'type'} dicts")
        content: dict[str, Any] = {"text": text, "style": "paragraph"}
        if marks:
            # C11d: marks round-trip verbatim at content.marks (the key
            # is absent when none were sent, like live).
            content["marks"] = list(marks)
        message = {
            "id": self._new_id(),
            # Short lexicographically-increasing string, like live (C3).
            "order_id": f"o{next(self._chat_order):08d}",
            "creator": creator,
            "creator_name": creator,
            "created_at": next(self._clock),
            "modified_at": 0,
            "content": content,
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
            # Participants are hidden too (S11): the live list NEVER returns
            # participant-layout objects; /members is their only enumeration.
            items = [
                o for o in self._objects.values()
                if not o["archived"] and o.get("layout") != "participant"
            ]
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
            # template_id (spiked): the template supplies default property
            # values + a scaffold body server-side; the request's properties
            # override defaults per key, and the request body is appended below
            # the template body (template first). Defaults are trusted (seeded)
            # so they bypass the request validation above.
            tpl = self._templates_by_id.get(body.get("template_id", ""))
            properties: list[dict[str, Any]] = list(tpl["default_properties"]) if tpl else []
            for entry in body.get("properties", []):
                properties = [p for p in properties if p.get("key") != entry.get("key")]
                properties.append(entry)
            tpl_body = tpl["body"] if tpl else ""
            req_body = body.get("body", "")
            markdown = f"{tpl_body}\n{req_body}" if tpl_body and req_body else tpl_body or req_body
            object_id = self._new_id()
            self._objects[object_id] = {
                "id": object_id,
                "name": body.get("name", ""),
                "type": {"key": body["type_key"]},
                "archived": False,
                "icon": body.get("icon"),
                "properties": properties,
                "snippet": "",
                # A5: body in, markdown out; A13: fence tags dropped.
                "markdown": _strip_fence_language(markdown),
            }
            self._stamp(self._objects[object_id], PROP_CREATED)
            return httpx.Response(201, json={"object": self._objects[object_id]})
        return self._error(405, "method_not_allowed")

    def _handle_search(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method != "POST":
            return self._error(405, "method_not_allowed")
        body = json.loads(request.content) if request.content else {}
        # Participants are invisible to search exactly like the live server
        # (S11) -- a name query for a member returns nothing.
        items = [
            o for o in self._objects.values()
            if not o["archived"] and o.get("layout") != "participant"
        ]
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
            chat = self._chats.get(match.group("obj"))
            if chat is not None:
                # C9 (spike S12): chats have no single-chat route of their
                # own, but the GENERIC object routes serve them -- GET
                # answers the envelope, PATCH {"name"} renames (the next
                # /chats re-list reflects it).
                if request.method == "GET":
                    return httpx.Response(200, json={"object": chat})
                if request.method == "PATCH":
                    body = json.loads(request.content)
                    if "name" in body:
                        chat["name"] = body["name"]
                    return httpx.Response(200, json={"object": chat})
            template = self._templates_by_id.get(match.group("obj"))
            if template is not None and request.method == "GET":
                # Templates answer the single-object GET like any object,
                # markdown carrying their body scaffold (live-confirmed).
                return httpx.Response(200, json={"object": {
                    "id": template["id"],
                    "name": template["name"],
                    "type": {"key": "template"},
                    "archived": False,
                    "properties": [],
                    "snippet": "",
                    "markdown": template["body"],
                }})
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
                obj["markdown"] = _strip_fence_language(
                    _flatten_first_line_heading(body["markdown"])
                )
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

    # -- list/view routes (WP13; quirks V1-V5 in view_catalog.py, spike S9) --

    def _handle_list_views(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        list_id = match.group("list")
        if list_id not in self._set_views:
            return self._error(404, "list_not_found")
        return self._paginated(list(self._set_views[list_id]), request.url.params)

    def _handle_list_view_objects(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        list_id = match.group("list")
        source = self._set_sources.get(list_id)
        if source is None:
            # S9: a sourceless set's execution endpoint 500s.
            return self._error(500, "internal_error")
        items = [
            o for o in self._objects.values()
            if not o["archived"] and o["type"]["key"] == source
        ]
        # NOTE: filters/sorts are NOT executed here — the compile path
        # (ADR 018) only samples this endpoint for the source type.
        return self._paginated(_without_markdown(items), request.url.params)

    # -- file routes (WP23; quirk C10, pinned by spike S13) ------------------

    def _handle_files(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        # C10: no list route -- GET /files 404s live.
        if request.method != "POST":
            return self._error(404, "not_found")
        filename, content = _parse_multipart_file(request)
        if filename is None or content is None:
            return httpx.Response(400, json={
                "object": "error", "status": 400, "code": "bad_request",
                "message": "missing file in request",
            })
        media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        stem, _dot, extension = filename.rpartition(".")
        file_id = self.seed_file(
            stem or filename, content, media=media, extension=extension
        )
        # C10: FLAT response, no envelope.
        return httpx.Response(200, json={
            "object_id": file_id, "name": stem or filename, "media": media,
            "extension": extension, "size_in_bytes": len(content),
        })

    def _handle_file(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if request.method != "GET":
            return self._error(404, "not_found")
        stored = self._file_bytes.get(match.group("file"))
        if stored is None:
            return self._error(404, "file_not_found")
        content, media = stored
        # C10: the raw bytes directly, Content-Type as the media source.
        return httpx.Response(200, content=content,
                              headers={"Content-Type": media})

    def seed_file(
        self, name: str, content: bytes, *,
        media: str = "application/octet-stream", extension: str = "",
    ) -> str:
        """A file in the space, as the live upload leaves one (C10): the
        bytes behind GET /files/:id plus a REAL object (type ``image``
        for images, ``file`` otherwise; size/extension properties, no
        MIME type)."""
        type_key = "image" if media.startswith("image/") else "file"
        file_id = self.seed_object(type_key, name, [
            {"key": "size_in_bytes", "format": "number",
             "number": len(content)},
            {"key": "file_ext", "format": "text", "text": extension},
        ])
        self._file_bytes[file_id] = (content, media)
        return file_id

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
            text = str(body.get("text", ""))
            invalid = self._marks_error(text, body.get("marks"))
            if invalid is not None:
                return invalid
            message = self._new_chat_message(
                chat_id, self.api_member_id, text,
                attachments=raw_attachments, marks=body.get("marks"),
            )
            # C1: flat message_id, no envelope key.
            return httpx.Response(201, json={"message_id": message["id"]})
        return self._error(405, "method_not_allowed")

    def _marks_error(
        self, text: str, marks: Any
    ) -> httpx.Response | None:
        """C11 (spike S14): a non-list ``marks`` 400s (Go unmarshal); a
        range that is negative, inverted, or past the text's UTF-16
        length 500s ("failed to add chat message"). Mark ``type``
        vocabulary is NOT vetted (unknown types land silently)."""
        if marks is None:
            return None
        if not isinstance(marks, list) or any(
            not isinstance(m, dict) for m in marks
        ):
            return self._error(400, "bad_request")
        limit = utf16_len(text)
        for mark in marks:
            start, end = int(mark.get("from", 0)), int(mark.get("to", 0))
            if start < 0 or end < start or end > limit:
                return self._error(500, "internal_server_error")
        return None

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
            raw_attachments = body.get("attachments")
            if raw_attachments is not None and any(
                not isinstance(e, dict) for e in raw_attachments
            ):
                return self._error(400, "bad_request")  # C7: envelopes only
            text = str(body.get("text", ""))
            invalid = self._marks_error(text, body.get("marks"))
            if invalid is not None:
                return invalid
            message["content"]["text"] = text
            # C8: an edit replaces content wholesale -- attachments (and
            # marks, C11d) absent from the body are removed (live-confirmed).
            message["attachments"] = list(raw_attachments or [])
            if body.get("marks"):
                message["content"]["marks"] = list(body["marks"])
            else:
                message["content"].pop("marks", None)
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

    def _handle_chat_reactions(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        """C12 (spike S15): POST toggles the calling ACCOUNT's reaction;
        the 200 body is empty; open streams see ``reactions_updated``."""
        if request.method != "POST":
            return self._error(405, "method_not_allowed")
        chat_id = match.group("chat")
        if chat_id not in self._chats:
            return self._error(404, "chat_not_found")
        message = next(
            (m for m in self._chat_messages[chat_id]
             if m["id"] == match.group("msg")),
            None,
        )
        if message is None:
            return self._error(404, "message_not_found")
        body = json.loads(request.content)
        emoji = str(body.get("emoji", ""))
        if not emoji:
            return self._error(400, "bad_request")
        self._toggle_reaction(chat_id, message, emoji, self.api_identity)
        return httpx.Response(200)

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
                    elif "payload" in item:
                        # C12: reactions_updated frames carry a bare
                        # id+reactions payload, no message envelope.
                        yield _sse_payload_frame(item["kind"], item["payload"])
                    else:
                        yield _sse_frame(item["kind"], item["message"])
            finally:
                self._chat_listeners[chat_id].remove(queue)

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=frames()
        )

    def _handle_templates(
        self, request: httpx.Request, match: re.Match[str]
    ) -> httpx.Response:
        """A type's templates, addressed by type OBJECT id (like the live
        route). The list view carries id/name only; body + defaults are the
        server-side apply state, read only when the template is applied."""
        if request.method != "GET":
            return self._error(405, "method_not_allowed")
        type_id = match.group("type")
        if not any(t["id"] == type_id for t in self._types.values()):
            return self._error(404, "type_not_found")
        listed = [
            {"id": t["id"], "name": t["name"]}
            for t in self._templates.get(type_id, [])
        ]
        return self._paginated(listed, request.url.params)

    def _handle_types(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._types.values()), request.url.params)
        body = json.loads(request.content)
        if not body.get("key") or not body.get("plural_name"):
            # The live API requires both (spike: a missing plural_name 400s).
            return self._error(400, "bad_request")
        # An inline ``properties`` list attaches the fields to the type AND
        # creates them as space properties (live-confirmed 2026-07-06). The
        # type's own ``properties`` carry the property ids, exactly like the
        # live GET /types response (the templates spike reads them).
        for entry in body.get("properties", []):
            self._properties.setdefault(
                entry["key"], {"id": self._new_id(), **entry}
            )
        type_properties = [
            {**entry, "id": self._properties[entry["key"]]["id"]}
            for entry in body.get("properties", [])
        ]
        self._types[body["key"]] = {
            "id": self._new_id(), **body, "properties": type_properties,
        }
        return httpx.Response(201, json={"type": self._types[body["key"]]})

    def _handle_type(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """Single type by object ID: GET, and PATCH with quirk A11 --
        an updated ``properties`` list replaces the type's fields
        WHOLESALE (live-confirmed 2026-07-15, spike_type_update); new
        keys mint space-wide exactly like an inline create; omitting
        ``properties`` leaves the fields untouched."""
        type_id = match.group("type")
        entry = next(
            (t for t in self._types.values() if t["id"] == type_id), None
        )
        if entry is None:
            return self._error(404, "object_not_found")
        if request.method == "GET":
            return httpx.Response(200, json={"type": entry})
        if request.method != "PATCH":
            return self._error(405, "method_not_allowed")
        body = json.loads(request.content)
        for field in ("name", "plural_name", "layout"):
            if field in body:
                entry[field] = body[field]
        if "properties" in body:
            for prop in body["properties"]:
                self._properties.setdefault(
                    prop["key"], {"id": self._new_id(), **prop}
                )
            entry["properties"] = [
                {**prop, "id": self._properties[prop["key"]]["id"]}
                for prop in body["properties"]
            ]
        return httpx.Response(200, json={"type": entry})

    def _handle_properties(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        if request.method == "GET":
            return self._paginated(list(self._properties.values()), request.url.params)
        body = json.loads(request.content)
        self._properties[body["key"]] = {"id": self._new_id(), **body}
        if self.property_settle_patches > 0 and body.get("format") == "objects":
            # Only ``objects``-format relations have the settle window. A
            # fresh SCALAR property is immediately usable in POST /objects
            # and PATCH (live-confirmed 2026-07-10, ADR 023 spike).
            self._settling[body["key"]] = self.property_settle_patches
        return httpx.Response(201, json={"property": self._properties[body["key"]]})

    def _handle_property(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """Single property by object ID. DELETE detaches the field from
        every type that carries it (live-confirmed 2026-07-15) and frees
        the key for re-creation under a new format (quirk A12)."""
        prop = next(
            (p for p in self._properties.values()
             if p["id"] == match.group("prop")),
            None,
        )
        if prop is None:
            return self._error(404, "object_not_found")
        if request.method == "GET":
            return httpx.Response(200, json={"property": prop})
        if request.method != "DELETE":
            return self._error(405, "method_not_allowed")
        del self._properties[prop["key"]]
        self._tags.pop(prop["id"], None)
        for entry in self._types.values():
            entry["properties"] = [
                p for p in entry.get("properties", [])
                if p.get("key") != prop["key"]
            ]
        return httpx.Response(200, json={"property": prop})

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
