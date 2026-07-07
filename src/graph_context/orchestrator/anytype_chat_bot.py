"""The Anytype chat transport adapter: composition root + stream loop (WP14).

The bot chats INSIDE Anytype spaces: each ``spaces.toml`` binding gets a
runtime (bootstrap.build_space_runtimes) and one SSE-driven serve task on
its chat. Every per-message decision -- echo/backlog gate, ``anytype:<id>``
identity, deep links, chunking -- is plain logic in
``anytype_chat_transport``; here we only read config, wire clients, and
pump events. Only this module (a composition root, like ``discord_bot``)
touches ``infrastructure`` on the chat path.

Startup catch-up (user requirement, ADR 019): the chat cursor persists
(``GC_CHAT_CURSOR``, default ``logs/chat_cursor.json``; ``0``/``off``
disables). A chat WITH a persisted position first answers every message
that arrived while the bot was down (up to the API's recency window);
only a chat with NO position fast-forwards past its history.

Reconnects: the client's SSE read timeout is tied to the heartbeat, so a
half-dead stream raises instead of hanging; this loop reconnects with
capped exponential backoff + jitter, and the cursor makes reconnect
replays turn-free.

Config: ANYTYPE_API_KEY(_FILE) / ANYTYPE_BASE_URL family (endpoint-
agnostic: the desktop app today, the headless sidecar after cutover),
GC_SPACES_FILE (required), GC_CHAT_CURSOR, plus the usual GC_DRIVER /
GC_PROFILE / GC_MODES_FILE / provenance knobs.

Run:  python -m graph_context.orchestrator.anytype_chat_bot
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

from graph_context import composition
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.chat import AnytypeChatClient, ChatMessage
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.anytype_chat_transport import (
    AnytypeChatTurnHandler,
    ChatCursor,
    InboundChatMessage,
)
from graph_context.orchestrator.spaces import SpaceBinding

logger = logging.getLogger(__name__)

DEFAULT_CURSOR_PATH = "logs/chat_cursor.json"
CATCHUP_WINDOW = 100  # the messages endpoint's recency window (C2)
_RECONNECT_CAP_SECONDS = 60.0


def _cursor_path() -> str | None:
    raw = os.environ.get("GC_CHAT_CURSOR", DEFAULT_CURSOR_PATH).strip()
    if raw.lower() in {"", "0", "false", "no", "off"}:
        return None
    return raw


async def _maybe_turn(
    handler: AnytypeChatTurnHandler,
    space_id: str,
    chat_id: str,
    message: ChatMessage,
    chat_client: AnytypeChatClient,
) -> None:
    inbound = InboundChatMessage(
        space_id=space_id,
        chat_id=chat_id,
        message_id=message.id,
        creator=message.creator,
        text=message.text,
        order_id=message.order_id,
    )
    if not handler.accepts(inbound):
        return

    async def send(text: str) -> str:
        return await chat_client.send(chat_id, text)

    try:
        await handler.run_turn(inbound, send)
    except GraphContextError as err:
        # Config-shaped errors are actionable; show them in-chat.
        await send(f"[error] {err}")
    except Exception:  # a turn must never take the serve loop down
        logger.exception("turn failed (chat=%s)", chat_id)
        await send("[error] the turn failed; see the bot log for the traceback")


async def _catch_up(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    cursor: ChatCursor,
) -> None:
    """First-run chats skip history; resumed chats answer the offline gap."""
    window = await chat_client.recent_messages(chat_id, limit=CATCHUP_WINDOW)
    if not cursor.has(chat_id):
        if window:
            cursor.fast_forward(chat_id, window[-1].order_id)
            logger.info(
                "chat %s: first run -- skipping %d historical message(s)",
                chat_id, len(window),
            )
        return
    for message in window:  # the gate drops everything <= the cursor
        await _maybe_turn(
            handler, chat_client.space_id, chat_id, message, chat_client
        )


async def _serve_chat(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    cursor: ChatCursor,
) -> None:
    space_id = chat_client.space_id
    await _catch_up(handler, chat_client, chat_id, cursor)
    delay = 1.0
    while True:
        try:
            async for event in chat_client.stream(chat_id):
                delay = 1.0  # a live stream resets the backoff
                if event.kind != "message_added" or event.message is None:
                    continue  # edits/deletes/reactions/heartbeats: no turns
                await _maybe_turn(
                    handler, space_id, chat_id, event.message, chat_client
                )
        except GraphContextError as err:
            logger.warning(
                "chat %s stream failed (%s); reconnecting in %.1fs",
                chat_id, err, delay,
            )
        else:
            logger.warning(
                "chat %s stream ended; reconnecting in %.1fs", chat_id, delay
            )
        # Jittered, capped backoff; the cursor makes replays turn-free.
        await asyncio.sleep(delay * (1.0 + random.random() * 0.25))
        delay = min(delay * 2, _RECONNECT_CAP_SECONDS)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    chat_clients: dict[str, AnytypeChatClient] = {}  # space id -> client
    transport_clients: list[AnytypeClient] = []

    def client_for(space_id: str) -> AnytypeChatClient:
        # Transport-side clients, one per space (the client is space-
        # scoped), separate from each runtime's repository client.
        if space_id not in chat_clients:
            client = AnytypeClient(AnytypeConfig.from_env(space_id))
            transport_clients.append(client)
            chat_clients[space_id] = AnytypeChatClient(client)
        return chat_clients[space_id]

    async def resolve_chat_id(binding: SpaceBinding) -> str:
        return await client_for(binding.space_id).resolve_chat_id(binding.chat_id)

    runtimes = await bootstrap.build_space_runtimes(resolve_chat_id)
    teardown = list(runtimes.teardown)
    teardown.extend(client.aclose for client in transport_clients)

    handler = AnytypeChatTurnHandler(
        routes=runtimes.routes,
        spaces=runtimes.spaces,
        cursor=ChatCursor(_cursor_path()),
        # bot_member_id stays "" until the sidecar's bot account exists
        # (quirk C6); posted-id echo suppression carries it alone.
    )
    try:
        served = "; ".join(
            f"{chat_id}: {desc}"
            for chat_id, desc in sorted(runtimes.descriptions.items())
        )
        logger.info("anytype chat: serving %s. %s", served, runtimes.help_line)
        await asyncio.gather(*(
            _serve_chat(handler, client_for(space_id), chat_id, handler.cursor)
            for chat_id, space_id in runtimes.spaces.items()
        ))
    finally:
        await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
