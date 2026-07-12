"""Anytype in-space chat transport policy, kept free of infrastructure (WP14).

The Anytype sibling of ``discord_transport``: everything the bot decides
per message -- the echo/backlog/creator gate, the ``anytype:<id>``
identity mapping, plain-text rendering + object attachments, the
"Processing" placeholder that the reply edits in place, chunked sends --
is plain logic
over primitives, so the whole policy tests without httpx or a server.
Only the composition root (``anytype_chat_bot``) touches infrastructure;
import-linter holds that line.

Two chat-specific hazards this module owns (ADR 019):

* **Echo loops.** The bot's own posts come back as ``message_added``
  events. Suppression is belt and suspenders: every id returned by our
  own send lands in :class:`SentMessages` (persisted), and any creator
  whose member id ends with the bot's account identity is dropped too
  (identity discovered at startup; ``""`` on the desktop endpoint).
* **Backlog replay.** The SSE stream replays recent history on every
  connect. :class:`ChatCursor` orders messages by ``order_id`` (quirk
  C3: string comparison IS stream order) and PERSISTS its position (a
  small JSON file, the turn-log precedent), so messages sent while the
  bot was down are answered on the next startup. Only a chat with no
  persisted position fast-forwards past its history -- a freshly bound
  chat must not trigger a turn per historical message. Losing the file
  degrades to exactly that first-run behavior.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.pipeline import sender_attributed
from graph_context.orchestrator.rendering import chunk, render

logger = logging.getLogger(__name__)

# No small server-side cap was observed (S10f: 5000 chars posted fine);
# this is a readability limit for the chat surface, not a protocol one.
ANYTYPE_MESSAGE_LIMIT = 2000

# Anytype object ids as they appear in tool output (CIDv1, base32).
_OBJECT_ID = r"bafy[a-z2-7]{20,}"
_MARKDOWN_ID_LINK = re.compile(r"\[([^\]]+)\]\((" + _OBJECT_ID + r")\)")
_BARE_ID = re.compile(r"(?<![\w/=?])(" + _OBJECT_ID + r")\b")
_MARKDOWN_URL_LINK = re.compile(r"\[([^\]]+)\]\((\w+://[^)\s]+)\)")
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3}|`{1,3})(?=\S)(.+?)(?<=\S)\1")

MAX_ATTACHMENTS = 8  # per message; the reader wants cards, not a wall

# The placeholder a turn posts the moment it starts (edited into the real
# reply by TurnReply); turns serialize per space and can run for a while,
# so the user must see the bot working, not silence.
PROCESSING_NOTICE = "Processing…"

SendFn = Callable[[str, tuple[str, ...]], Awaitable[str]]
EditFn = Callable[[str, str, tuple[str, ...]], Awaitable[None]]


def object_references(text: str) -> tuple[str, ...]:
    """Object ids mentioned in a reply, in first-appearance order.

    These become message ATTACHMENTS (quirk C7): the chat UI is plain
    text, so links cannot be links -- but attached objects render as
    clickable cards, which is the better surface anyway. Capped at
    ``MAX_ATTACHMENTS``.
    """
    seen: list[str] = []
    for match in re.finditer(_OBJECT_ID, text):
        object_id = match.group(0)
        if object_id not in seen:
            seen.append(object_id)
    return tuple(seen[:MAX_ATTACHMENTS])


def plainify(text: str) -> str:
    """Markdown -> chat plain text (quirk C7: the chat renders glyphs
    literally). ``[Name](bafy...)`` collapses to ``Name`` (the object
    rides along as an attachment); ordinary ``[label](url)`` keeps its
    url as ``label (url)``; headers and emphasis marks are stripped.
    Bullets and blank lines read fine as-is and are left alone.
    """
    text = _MARKDOWN_ID_LINK.sub(r"\1", text)
    text = _MARKDOWN_URL_LINK.sub(r"\1 (\2)", text)
    text = _HEADER.sub("", text)
    # Repeat for nested emphasis (e.g. bold inside italics).
    for _ in range(3):
        stripped = _EMPHASIS.sub(r"\2", text)
        if stripped == text:
            break
        text = stripped
    return text


@dataclass(frozen=True, slots=True)
class InboundChatMessage:
    """The slice of a chat message the policy needs."""

    space_id: str
    chat_id: str
    message_id: str
    creator: str
    text: str
    order_id: str
    # The sender's display name (the API returns it on every message);
    # the pipeline shows it to the model so "assign this to me"-shaped
    # requests are answerable. "" degrades to an unattributed message.
    creator_name: str = ""


class SentMessages:
    """Bounded set of message ids the bot posted (echo suppression).

    PERSISTED (like the cursor) because it must survive restarts: on the
    desktop endpoint the bot posts as the user's own account, so
    ``creator`` cannot distinguish an old bot reply from a human message
    during startup catch-up -- only this set can. (Live-caught: a restart
    once answered its own previous-life reply.) Same degrade posture as
    the cursor: unreadable file -> empty set + warning, failed write ->
    in-memory only.
    """

    def __init__(self, max_size: int = 1024, path: str | None = None) -> None:
        self._max_size = max_size
        self._path = path
        self._ids: OrderedDict[str, None] = OrderedDict()
        if path and os.path.exists(path):
            try:
                for message_id in json.loads(Path(path).read_text()):
                    self._ids[str(message_id)] = None
            except (OSError, ValueError, TypeError):
                logger.warning(
                    "unreadable sent-message ledger at %s; starting empty "
                    "(old bot replies may be answered once)", path,
                )

    def add(self, message_id: str) -> None:
        self._ids[message_id] = None
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)
        if self._path:
            try:
                target = Path(self._path)
                target.parent.mkdir(parents=True, exist_ok=True)
                scratch = target.with_suffix(target.suffix + ".tmp")
                scratch.write_text(json.dumps(list(self._ids)))
                scratch.replace(target)
            except OSError:
                logger.warning(
                    "cannot persist sent-message ledger to %s; in-memory "
                    "for this process", self._path,
                )

    def __contains__(self, message_id: str) -> bool:
        return message_id in self._ids


class ChatCursor:
    """Last processed ``order_id`` per chat (quirk C3: string-ordered).

    With a ``path`` the positions persist as a small JSON object
    (``{chat_id: order_id}``) rewritten on every advance -- one message
    equals one turn equals one tiny write, so no debounce is warranted.
    An unreadable file degrades to first-run behavior (fast-forward) with
    a warning; a failing write degrades to in-memory (the turn-log
    posture: the diary must never take the bot down).
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._seen: dict[str, str] = {}
        if path and os.path.exists(path):
            try:
                loaded = json.loads(Path(path).read_text())
                self._seen = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError, AttributeError):
                logger.warning(
                    "unreadable chat cursor at %s; treating every chat as "
                    "first-run (history will be skipped, not replayed)", path,
                )

    def has(self, chat_id: str) -> bool:
        """Whether a position exists -- the first-run test: no position
        means fast-forward past history; a position means the gap since it
        (offline messages included) is fair game."""
        return chat_id in self._seen

    def is_new(self, message: InboundChatMessage) -> bool:
        last = self._seen.get(message.chat_id, "")
        return message.order_id > last

    def advance(self, message: InboundChatMessage) -> None:
        self.fast_forward(message.chat_id, message.order_id)

    def fast_forward(self, chat_id: str, order_id: str) -> None:
        if order_id > self._seen.get(chat_id, ""):
            self._seen[chat_id] = order_id
            self._save()

    def _save(self) -> None:
        if not self._path:
            return
        try:
            target = Path(self._path)
            target.parent.mkdir(parents=True, exist_ok=True)
            scratch = target.with_suffix(target.suffix + ".tmp")
            scratch.write_text(json.dumps(self._seen, indent=0, sort_keys=True))
            scratch.replace(target)  # atomic: a crash never half-writes
        except OSError:
            logger.warning(
                "cannot persist chat cursor to %s; positions are in-memory "
                "for this process", self._path,
            )


@dataclass
class TurnReply:
    """One turn's outbound surface: a placeholder first, then the reply.

    ``open`` posts :data:`PROCESSING_NOTICE` the moment the turn starts;
    the FIRST delivered chunk EDITS that placeholder in place (quirk C8:
    an edit replaces content wholesale, so the chunk's attachments ride
    the edit body) and later chunks post as ordinary messages. Every
    posted id -- the placeholder's included -- lands in the sent ledger
    for echo suppression. The composition root's error paths deliver
    through the same object, so a failed turn replaces its placeholder
    instead of stranding "Processing…" in the chat; a delivery whose
    placeholder is gone (edit failed, or ``open`` never ran) degrades to
    a plain send.
    """

    send: SendFn
    edit: EditFn
    sent: SentMessages
    _placeholder_id: str | None = None

    async def open(self) -> None:
        if self._placeholder_id is None:
            self._placeholder_id = await self.send(PROCESSING_NOTICE, ())
            self.sent.add(self._placeholder_id)

    async def deliver(
        self, text: str, attachments: tuple[str, ...] = ()
    ) -> None:
        placeholder, self._placeholder_id = self._placeholder_id, None
        if placeholder is not None:
            await self.edit(placeholder, text, attachments)
        else:
            self.sent.add(await self.send(text, attachments))

    async def finish(self) -> None:
        """A turn that delivered nothing must not strand the placeholder.
        (The pipeline always yields at least one event; this is insurance.)"""
        if self._placeholder_id is not None:
            await self.deliver("(the turn produced no reply)")


@dataclass
class AnytypeChatTurnHandler:
    """Gate -> turn -> plain-text chunked deliveries: the message policy.

    ``routes`` maps each served CHAT id to its runtime; ``spaces`` maps it
    back to the space id. The send/edit primitives are injected per
    message (via :meth:`reply`): ``send`` takes ``(text,
    attachment_object_ids)`` and must return the posted message's id,
    which feeds the echo suppressor; ``edit`` takes ``(message_id, text,
    attachment_object_ids)``. ``bot_identity`` is
    the ACCOUNT identity (quirk C6: member ids are space-scoped but end
    with it, so the self-check is a suffix match); ``""`` -- e.g. on the
    desktop endpoint, where bot and human share an account -- leaves the
    posted-id set carrying suppression alone.
    """

    routes: Mapping[str, ChannelRoute]
    spaces: Mapping[str, str]
    sent: SentMessages = field(default_factory=SentMessages)
    cursor: ChatCursor = field(default_factory=ChatCursor)
    # WP15: where each chat's conversation memory begins. ``/clear`` never
    # deletes chat messages (the chat is the human record; there is no
    # bulk-delete endpoint anyway) -- it records a boundary, and startup
    # seeding only reads history after it. A second ChatCursor because the
    # need is identical: a persisted per-chat order_id watermark.
    clear_marks: ChatCursor = field(default_factory=ChatCursor)
    bot_identity: str = ""

    def accepts(self, message: InboundChatMessage) -> bool:
        """The gate, separate from the turn (mirrors DiscordTurnHandler)."""
        if message.chat_id not in self.routes:
            return False
        if message.message_id in self.sent:  # our own post, echoed back
            return False
        if self.bot_identity and message.creator.endswith(self.bot_identity):
            return False  # ours even if the send's id was never recorded
        if not self.cursor.is_new(message):  # backlog replay / reconnect
            return False
        if not message.text.strip():  # noqa: SIM103 -- gate reads as a checklist
            return False
        return True

    def seed_events(
        self, chat_id: str, messages: Sequence[InboundChatMessage]
    ) -> list[tuple[str, str]]:
        """Classify already-answered history into conversation-memory seed
        events (WP15 startup catch-up).

        ``messages`` is the fetched recency window, oldest-first. Kept:
        messages after the chat's ``/clear`` watermark, minus unanswered
        USER backlog above the cursor (the catch-up turn answers and
        remembers those). Bot messages above the cursor stay: the reply to
        the last answered message always posts after it, and the gate never
        re-answers our own posts -- dropping it left every restart-seeded
        prompt ending in an apparently-unanswered request. Bot messages are
        recognised by the same two signals the gate uses -- the sent ledger
        and the identity suffix; ``/``-commands are dropped (they were
        never conversation).
        """
        seed: list[tuple[str, str]] = []
        for message in messages:
            if message.chat_id != chat_id:
                continue
            if not self.clear_marks.is_new(message):
                continue  # at or before the last /clear
            ours = message.message_id in self.sent or (
                self.bot_identity != ""
                and message.creator.endswith(self.bot_identity)
            )
            if not ours and self.cursor.is_new(message):
                continue  # unanswered backlog: the catch-up turn owns it
            text = message.text.strip()
            if not text:
                continue
            if not ours and text.startswith("/"):
                continue
            if ours:
                seed.append(("assistant", text))
            else:
                # Same attribution the live turn applies, so restart-seeded
                # history reads identically to remembered history.
                seed.append(
                    ("user", sender_attributed(text, message.creator_name))
                )
        return seed

    def reply(self, send: SendFn, edit: EditFn) -> TurnReply:
        """The outbound surface for one turn, wired to this handler's
        echo ledger."""
        return TurnReply(send=send, edit=edit, sent=self.sent)

    async def run_turn(
        self, message: InboundChatMessage, reply: TurnReply
    ) -> None:
        """One accepted message -> placeholder -> turn -> n deliveries.

        The cursor advances BEFORE the turn: a failing turn must not make
        the same message eligible again on the next stream event. The
        placeholder posts BEFORE the route lock, so a queued message
        shows progress even while an earlier turn holds the space.
        Replies go out as plain text (quirk C7) with every referenced
        object attached to the first chunk as a card.
        """
        self.cursor.advance(message)
        await reply.open()
        if message.text.strip() == "/clear":
            # The orchestrator empties the in-memory ring; the watermark
            # makes the clear survive a restart (seeding stops here).
            self.clear_marks.fast_forward(message.chat_id, message.order_id)
        route = self.routes[message.chat_id]
        async with route.lock:
            events = await route.orchestrator.handle_message(
                session_id=f"anytype:{message.chat_id}",
                user_id=f"anytype:{message.creator}",
                text=message.text,
                # Intent nodes point back at the triggering chat message.
                origin=f"anytype:{message.chat_id}:{message.message_id}",
                sender=message.creator_name,
            )
        for event in events:
            rendered = render(event)
            attachments = object_references(rendered)
            for piece in chunk(plainify(rendered), ANYTYPE_MESSAGE_LIMIT):
                await reply.deliver(piece, attachments)
                attachments = ()  # cards ride the first chunk only
        await reply.finish()
