"""Anytype in-space chat transport policy, kept free of infrastructure (WP14).

The Anytype sibling of ``discord_transport``: everything the bot decides
per message -- the echo/backlog/creator gate, the ``anytype:<id>``
identity mapping, plain-text rendering + object attachments, chunked
sends -- is plain logic
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from graph_context.orchestrator.channels import ChannelRoute
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
class AnytypeChatTurnHandler:
    """Gate -> turn -> plain-text chunked sends: the whole message policy.

    ``routes`` maps each served CHAT id to its runtime; ``spaces`` maps it
    back to the space id. ``send`` is injected per message, takes
    ``(text, attachment_object_ids)``, and must return the posted
    message's id, which feeds the echo suppressor. ``bot_identity`` is
    the ACCOUNT identity (quirk C6: member ids are space-scoped but end
    with it, so the self-check is a suffix match); ``""`` -- e.g. on the
    desktop endpoint, where bot and human share an account -- leaves the
    posted-id set carrying suppression alone.
    """

    routes: Mapping[str, ChannelRoute]
    spaces: Mapping[str, str]
    sent: SentMessages = field(default_factory=SentMessages)
    cursor: ChatCursor = field(default_factory=ChatCursor)
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

    async def run_turn(
        self,
        message: InboundChatMessage,
        send: Callable[[str, tuple[str, ...]], Awaitable[str]],
    ) -> None:
        """One accepted message -> one orchestrator turn -> n sends.

        The cursor advances BEFORE the turn: a failing turn must not make
        the same message eligible again on the next stream event.
        Replies go out as plain text (quirk C7) with every referenced
        object attached to the first chunk as a card.
        """
        self.cursor.advance(message)
        route = self.routes[message.chat_id]
        async with route.lock:
            events = await route.orchestrator.handle_message(
                session_id=f"anytype:{message.chat_id}",
                user_id=f"anytype:{message.creator}",
                text=message.text,
                # Intent nodes point back at the triggering chat message.
                origin=f"anytype:{message.chat_id}:{message.message_id}",
            )
        for event in events:
            rendered = render(event)
            attachments = object_references(rendered)
            for piece in chunk(plainify(rendered), ANYTYPE_MESSAGE_LIMIT):
                self.sent.add(await send(piece, attachments))
                attachments = ()  # cards ride the first chunk only
