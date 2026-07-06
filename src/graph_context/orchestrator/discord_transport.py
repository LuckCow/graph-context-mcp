"""Discord transport policy, kept free of discord.py (WP8).

Everything the bot decides per message -- the channel/author gate, the
``discord:<id>`` identity mapping (WP8's transport-scoped ids), the
2000-char chunking dialect shim, event rendering -- is plain logic over
primitives here, so the whole policy tests without the discord
dependency; only the composition root (``discord_bot``) imports the
library, and import-linter holds that line.

One message = one turn = at most one intent node. Turns are serialized
process-wide because the underlying focus-stack session still is too
(per-session ``SessionState`` is a later WP8 slice); the lock keeps two
users' turns from interleaving tool calls, and queue fairness stays a
WP8 open question.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from graph_context.errors import GraphContextError
from graph_context.orchestrator.pipeline import Orchestrator, ReplyEvent

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000  # hard per-message cap, enforced by Discord

_PREFIXES = {"reply": "", "notice": "[notice] ", "error": "[error] "}


def parse_channel_allowlist(raw: str | None) -> frozenset[int]:
    """``GC_DISCORD_CHANNELS`` -> channel ids; unset or garbage fails loudly.

    No allowlist is a config error, not "serve everywhere": an invited bot
    sees every channel in the server, and WP8's authz stance is that
    unauthorized surfaces get nothing bound -- here, no turns at all.
    """
    ids = (raw or "").replace(",", " ").split()
    if not ids:
        raise GraphContextError(
            "GC_DISCORD_CHANNELS is unset or empty; set it to the Discord "
            "channel id(s) the bot may serve (comma- or space-separated)"
        )
    try:
        return frozenset(int(i) for i in ids)
    except ValueError:
        raise GraphContextError(
            f"GC_DISCORD_CHANNELS must be numeric channel ids, got: {raw!r}"
        ) from None


def render(event: ReplyEvent) -> str:
    """Transport-neutral event -> Discord text (plain prefixes, like the CLI)."""
    return f"{_PREFIXES[event.kind]}{event.text}"


def chunk(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split into sendable pieces, preferring line then word boundaries."""
    text = text.strip()
    pieces: list[str] = []
    while len(text) > limit:
        window = text[: limit + 1]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """The slice of a Discord message the policy needs."""

    channel_id: int
    author_id: int
    author_is_bot: bool
    content: str


@dataclass
class DiscordTurnHandler:
    """Gate -> turn -> chunked sends: the transport's whole message policy.

    ``send`` is injected per message (the adapter binds it to
    ``message.channel.send``), so tests drive turns with a list-appending
    fake instead of a Discord connection.
    """

    orchestrator: Orchestrator
    allowed_channels: frozenset[int]
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def accepts(self, message: InboundMessage) -> bool:
        """The gate, separate from the turn so the adapter can decide
        whether to show a typing indicator before any work starts."""
        if message.author_is_bot:  # covers our own echoes and other bots
            return False
        if message.channel_id not in self.allowed_channels:
            return False
        if not message.content.strip():
            # In an allowed channel this usually means the message-content
            # privileged intent is off in the Discord developer portal.
            logger.warning(
                "empty message content in allowed channel %s -- is the "
                "message-content intent enabled?", message.channel_id,
            )
            return False
        return True

    async def run_turn(
        self,
        message: InboundMessage,
        send: Callable[[str], Awaitable[object]],
    ) -> None:
        """One accepted message -> one orchestrator turn -> n sends."""
        async with self._turn_lock:
            events = await self.orchestrator.handle_message(
                session_id=f"discord:{message.channel_id}",
                user_id=f"discord:{message.author_id}",
                text=message.content,
            )
        for event in events:
            for piece in chunk(render(event)):
                await send(piece)
