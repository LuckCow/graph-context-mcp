"""Anytype in-space chat transport policy, kept free of infrastructure (WP14).

The Anytype sibling of ``discord_transport``: everything the bot decides
per message -- the echo/backlog/creator gate, the ``anytype:<id>``
identity mapping, deep-link rewriting, chunked sends -- is plain logic
over primitives, so the whole policy tests without httpx or a server.
Only the composition root (``anytype_chat_bot``) touches infrastructure;
import-linter holds that line.

Two chat-specific hazards this module owns (ADR 019):

* **Echo loops.** The bot's own posts come back as ``message_added``
  events. Suppression is belt and suspenders: every id returned by our
  own send lands in :class:`SentMessages`, and (once the sidecar's bot
  account exists) ``creator == bot_member_id`` is dropped too.
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


def _deep_link(object_id: str, space_id: str) -> str:
    return f"anytype://object?objectId={object_id}&spaceId={space_id}"


def linkify(text: str, space_id: str) -> str:
    """Rewrite object references into clickable ``anytype://`` deep links.

    ``[Name](bafy...)`` becomes ``[Name](anytype://object?...)``; a bare
    ``bafy...`` token becomes ``[bafy1234...](anytype://object?...)``.
    Existing ``anytype://`` links (and ids already inside URLs) pass
    through untouched -- the lookbehind refuses ids preceded by ``/``,
    ``=`` or ``?``, which is where they sit inside a deep link.
    """

    def _rewrite_link(match: re.Match[str]) -> str:
        return f"[{match.group(1)}]({_deep_link(match.group(2), space_id)})"

    text = _MARKDOWN_ID_LINK.sub(_rewrite_link, text)

    def _rewrite_bare(match: re.Match[str]) -> str:
        object_id = match.group(1)
        return f"[{object_id[:8]}…]({_deep_link(object_id, space_id)})"

    return _BARE_ID.sub(_rewrite_bare, text)


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
    """Gate -> turn -> linkified chunked sends: the whole message policy.

    ``routes`` maps each served CHAT id to its runtime; ``spaces`` maps it
    back to the space id (deep links need it). ``send`` is injected per
    message and must return the posted message's id, which feeds the echo
    suppressor. ``bot_member_id`` is ``""`` until the sidecar's bot
    account exists (quirk C6) -- the posted-id set carries suppression
    alone until then.
    """

    routes: Mapping[str, ChannelRoute]
    spaces: Mapping[str, str]
    sent: SentMessages = field(default_factory=SentMessages)
    cursor: ChatCursor = field(default_factory=ChatCursor)
    bot_member_id: str = ""

    def accepts(self, message: InboundChatMessage) -> bool:
        """The gate, separate from the turn (mirrors DiscordTurnHandler)."""
        if message.chat_id not in self.routes:
            return False
        if message.message_id in self.sent:  # our own post, echoed back
            return False
        if self.bot_member_id and message.creator == self.bot_member_id:
            return False  # ours even if the send's id was never recorded
        if not self.cursor.is_new(message):  # backlog replay / reconnect
            return False
        if not message.text.strip():  # noqa: SIM103 -- gate reads as a checklist
            return False
        return True

    async def run_turn(
        self,
        message: InboundChatMessage,
        send: Callable[[str], Awaitable[str]],
    ) -> None:
        """One accepted message -> one orchestrator turn -> n sends.

        The cursor advances BEFORE the turn: a failing turn must not make
        the same message eligible again on the next stream event.
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
        space_id = self.spaces.get(message.chat_id, message.space_id)
        for event in events:
            text = linkify(render(event), space_id)
            for piece in chunk(text, ANYTYPE_MESSAGE_LIMIT):
                self.sent.add(await send(piece))
