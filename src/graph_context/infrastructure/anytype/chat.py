"""Chat payload shapes and the chat client -- the chat quirk quarantine.

The chat analogue of ``mapping.py``: every representation assumption about
the Chat API (heart v0.50.7+, still under the pinned ``2025-11-08``
version header) lives here, pinned by spike S10 and mirrored by
``mock_server.py``:

    C1. ``POST .../messages`` returns a flat ``{"message_id": ...}`` --
        no envelope key, unlike every other write endpoint.
    C2. ``GET .../messages`` returns ``{"messages": [...]}`` with NO
        pagination block; ``offset`` is ignored (a recency window).
    C3. A message's text lives at ``content.text`` (markdown stored
        verbatim); ``order_id`` is a short lexicographically-increasing
        string (e.g. ``"!!%>"``) -- string comparison IS stream order.
    C4. ``creator`` is the member id ``_participant_<space>_<identity>``.
    C5. SSE framing: ``event: <kind>`` + ``data: {"type": ..., "payload":
        {"message": {...}}}`` + blank line; keepalives are COMMENT lines
        ``: heartbeat``. On connect the stream replays recent history as
        ordinary ``message_added`` frames -- consumers must fast-forward.
    C7. The chat UI renders message text as PLAIN TEXT (markdown shows
        its literal glyphs -- live-observed), but a message accepts
        ``attachments: [{"target": <object_id>, "type": "link"}]``
        (a bare id list 400s), which the clients render as object cards.
        Object references therefore travel as attachments, not links.
    C6. There is no "who am I" endpoint and members carry no self marker
        (S10d), but a member id embeds the account identity
        (``_participant_<space-with-dots-as-underscores>_<identity>``)
        and every account owns a private default space with exactly ONE
        member -- itself. :func:`discover_bot_identity` exploits that:
        list spaces, find one with a sole member, return its identity.
        The transport then self-filters by identity SUFFIX match on
        ``creator``. Posted-message-id suppression remains as the belt
        to this suspender.
    C8. ``PATCH .../messages/<id>`` replaces the message's content
        WHOLESALE: ``text`` and ``attachments`` both take the body's
        value, and attachments ABSENT from the body are removed
        (live-confirmed 2026-07-11 against the sidecar). An edit that
        wants to keep or add cards must re-send their envelopes.
    C9. A chat object has NO single-chat route: GET and PATCH on
        ``/chats/<id>`` both 404 (spike S12). It IS addressable through
        the generic ``/objects/<id>`` routes -- ``PATCH`` there renames
        it and the new name shows in the next ``/chats`` re-list. A chat
        created without a name is born with ``name: ""`` (what a fresh
        UI-created chat looks like to discovery).
    C10. Files (spike S13): ``POST /files`` uploads (multipart field
        ``file``; FLAT response -- object_id/name/media/extension/
        size_in_bytes, no envelope; the client's default JSON
        Content-Type must be overridden or the server sees no file);
        ``GET /files/<id>`` serves the raw bytes directly with the
        Content-Type header as the read-side media source (no
        ``/content`` sub-route; no list route). The upload is a REAL
        object (type key ``image`` for images, ``file`` otherwise;
        ``size_in_bytes``/``file_ext`` properties, but NOT the MIME
        type). Chat messages reference files through the same
        ``attachments`` envelopes as C7 (``{"target", "type"}``), and
        inbound messages EXPOSE their attachments.
    C11. Rich text rides ``marks`` beside ``text`` (spike S14):
        ``{"from", "to", "type"}`` + ``"param"`` for links, offsets in
        UTF-16 CODE UNITS. Ranges are bounds-checked server-side and an
        invalid one 500s (which the client retries!), so outbound text
        is converted markdown -> plain text + validated marks in ONE
        place (``marks.to_marked_text``, applied by ``_message_body``).
        Marks round-trip at ``content.marks`` and replace wholesale on
        edit like the rest of the content (C8). Full pinned detail in
        ``marks.py``'s module docstring.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.marks import to_marked_text

logger = logging.getLogger(__name__)

MESSAGE_EVENT_KINDS = frozenset(
    {"message_added", "message_updated", "message_deleted", "reactions_updated"}
)


@dataclass(frozen=True, slots=True)
class ChatAttachment:
    """One message attachment envelope (C7/C10): an object id plus the
    sender's envelope type (``link`` for object cards, ``file`` for
    files -- advisory only; the TARGET object's type is authoritative)."""

    target: str
    type: str = "link"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message, reduced to what the transport needs (C3/C4)."""

    id: str
    creator: str
    text: str
    order_id: str
    created_at: int = 0
    creator_name: str = ""
    attachments: tuple[ChatAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """One SSE frame: a message event, or a keepalive heartbeat."""

    kind: str  # one of MESSAGE_EVENT_KINDS, or "heartbeat"
    message: ChatMessage | None = None


def to_chat_message(raw: dict[str, Any]) -> ChatMessage:
    content = raw.get("content") or {}
    return ChatMessage(
        id=str(raw.get("id", "")),
        creator=str(raw.get("creator", "")),
        text=str(content.get("text", "")),
        order_id=str(raw.get("order_id", "")),
        created_at=int(raw.get("created_at") or 0),
        creator_name=str(raw.get("creator_name", "")),
        attachments=tuple(
            ChatAttachment(
                target=str(entry.get("target", "")),
                type=str(entry.get("type", "link")),
            )
            for entry in raw.get("attachments") or []
            if isinstance(entry, dict) and entry.get("target")
        ),
    )


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[ChatEvent]:
    """Translate raw SSE lines into :class:`ChatEvent`s (framing rule C5).

    Comment lines surface immediately as heartbeats (they are the
    liveness signal); ``event:``/``data:`` pairs accumulate until the
    blank frame terminator. Unparseable frames are logged and dropped --
    a malformed event must not kill the stream.
    """
    event_kind = ""
    data_parts: list[str] = []
    async for line in lines:
        if line.startswith(":"):
            yield ChatEvent(kind="heartbeat")
            continue
        if line.startswith("event:"):
            event_kind = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_parts.append(line[len("data:"):].strip())
            continue
        if line == "" and (event_kind or data_parts):
            kind, message = event_kind, None
            try:
                payload = json.loads("".join(data_parts)) if data_parts else {}
                kind = payload.get("type", event_kind) or event_kind
                raw = (payload.get("payload") or {}).get("message")
                if raw is not None:
                    message = to_chat_message(raw)
            except (ValueError, TypeError):
                logger.warning("dropping malformed SSE frame (event=%r)", event_kind)
                event_kind, data_parts = "", []
                continue
            event_kind, data_parts = "", []
            if kind in MESSAGE_EVENT_KINDS:
                yield ChatEvent(kind=kind, message=message)
            # unknown kinds are dropped silently: forward-compatible


async def discover_bot_identity(client: AnytypeClient) -> str:
    """The bot account's identity string, via quirk C6's side door.

    Any space with exactly one active member is (in practice) the bot's
    own default space, and that member is the bot. Returns ``""`` when no
    such space exists -- callers degrade to posted-id echo suppression
    alone, with a warning, rather than dying.
    """
    spaces = await client.request("GET", "/v1/spaces", params={"limit": 100})
    for space in spaces.get("data", []):
        members_payload = await client.request(
            "GET", f"/v1/spaces/{space['id']}/members", params={"limit": 2}
        )
        members = members_payload.get("data", [])
        if len(members) == 1 and members[0].get("identity"):
            identity = str(members[0]["identity"])
            logger.info(
                "bot identity %s (from solo-member space %s)",
                identity, space["id"],
            )
            return identity
    logger.warning(
        "no solo-member space found; bot identity unknown -- echo "
        "suppression rides on the posted-id ledger alone"
    )
    return ""


def _message_body(
    text: str, attachments: Sequence[str], attachment_type: str = "link"
) -> dict[str, Any]:
    """The create/edit payload: markdown becomes plain text + marks
    (C11 -- links go clickable, C7's plain-text rendering shows the
    rest unstyled otherwise), object ids become C7/C10 envelopes."""
    plain, marks = to_marked_text(text)
    body: dict[str, Any] = {"text": plain}
    if marks:
        body["marks"] = marks
    if attachments:
        body["attachments"] = [
            {"target": object_id, "type": attachment_type}
            for object_id in attachments
        ]
    return body


class AnytypeChatClient:
    """Chat operations for the client's bound space."""

    def __init__(self, client: AnytypeClient) -> None:
        self._client = client

    @property
    def space_id(self) -> str:
        return self._client.space_id

    async def list_chats(self) -> list[tuple[str, str]]:
        """Every chat in the space as ``(id, name)`` pairs (WP8).

        The bot serves each as its own thread; discovery re-lists to catch
        chats created while it runs. Reads are unthrottled (S7), so a
        periodic re-list is cheap."""
        return [
            (str(c["id"]), str(c.get("name") or ""))
            async for c in self._client.list_chats()
        ]

    async def rename(self, chat_id: str, name: str) -> None:
        """Set a chat's title (C9: via the generic object PATCH -- the
        chat namespace has no update route). WP21's auto-titling caller."""
        await self._client.rename_chat(chat_id, name)

    async def attachment_facts(self, target: str) -> dict[str, Any]:
        """What the transport needs to CLASSIFY an attachment before
        deciding to fetch it (WP23): the target object's name, type key,
        size, and extension. The MIME type is absent on the read side
        (C10) -- it arrives with the bytes."""
        obj = await self._client.get_object(target)
        props: dict[str, Any] = {}
        for entry in obj.get("properties", []):
            fmt = entry.get("format", "")
            props[entry.get("key", "")] = entry.get(fmt)
        return {
            "name": str(obj.get("name") or ""),
            "type_key": str((obj.get("type") or {}).get("key") or ""),
            "size_in_bytes": int(props.get("size_in_bytes") or 0),
            "extension": str(props.get("file_ext") or ""),
        }

    async def fetch_file(self, file_id: str) -> tuple[bytes, str]:
        """A file object's raw bytes + media type (C10)."""
        return await self._client.download_file(file_id)

    async def upload_file(self, filename: str, content: bytes) -> str:
        """Upload a file into the space; answers the new file OBJECT id,
        attachable to a message like any object (C7/C10 envelopes)."""
        uploaded = await self._client.upload_file(filename, content)
        return str(uploaded["object_id"])

    async def send_file_message(
        self, chat_id: str, text: str, file_id: str
    ) -> str:
        """Post a message carrying one uploaded file (C10: same envelope
        family as C7, ``type: "file"`` so clients render a file card)."""
        return await self._client.create_chat_message(
            chat_id, _message_body(text, (file_id,), attachment_type="file")
        )

    async def recent_messages(
        self, chat_id: str, *, limit: int = 100
    ) -> list[ChatMessage]:
        """The chat's recency window (C2), oldest-first -- the startup
        catch-up source for answering messages sent while the bot was down."""
        raw = await self._client.list_chat_messages(chat_id, limit=limit)
        return [to_chat_message(item) for item in raw]

    async def send(
        self, chat_id: str, text: str, attachments: Sequence[str] = ()
    ) -> str:
        """Post a message; ``attachments`` are object ids, sent in the
        envelope quirk C7 requires so clients render them as cards."""
        return await self._client.create_chat_message(
            chat_id, _message_body(text, attachments)
        )

    async def edit(
        self, chat_id: str, message_id: str, text: str,
        attachments: Sequence[str] = (),
    ) -> None:
        """Replace a message's content (quirk C8: the edit is wholesale,
        so any attachments the message should keep must ride along)."""
        await self._client.edit_chat_message(
            chat_id, message_id, _message_body(text, attachments)
        )

    async def delete(self, chat_id: str, message_id: str) -> None:
        """Remove a message (WP19: the live activity trace is deleted
        once the reply has posted)."""
        await self._client.delete_chat_message(chat_id, message_id)

    async def stream(
        self, chat_id: str, *, heartbeat_seconds: int = 30
    ) -> AsyncIterator[ChatEvent]:
        lines = self._client.stream_chat_messages(
            chat_id, heartbeat_seconds=heartbeat_seconds
        )
        async for event in parse_sse(lines):
            yield event
