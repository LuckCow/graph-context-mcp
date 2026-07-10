"""The Anytype chat transport adapter: composition root + stream loop (WP14).

The bot chats INSIDE Anytype spaces: each ``spaces.toml`` binding gets a
runtime (bootstrap.build_space_runtimes) and one SSE-driven serve task on
its chat. Every per-message decision -- echo/backlog gate, ``anytype:<id>``
identity, plain-text rendering + object-card attachments, chunking --
is plain logic in
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
from pathlib import Path

from graph_context import composition
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.chat import (
    AnytypeChatClient,
    ChatMessage,
    discover_bot_identity,
)
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.anytype_chat_transport import (
    AnytypeChatTurnHandler,
    ChatCursor,
    InboundChatMessage,
    SentMessages,
)
from graph_context.orchestrator.spaces import SpaceBinding, served_chat_ids

logger = logging.getLogger(__name__)

DEFAULT_CURSOR_PATH = "logs/chat_cursor.json"
CATCHUP_WINDOW = 100  # the messages endpoint's recency window (C2)
_RECONNECT_CAP_SECONDS = 60.0
CHAT_RESCAN_SECONDS = 60  # live-discovery poll (WP8); GC_CHAT_RESCAN_SECONDS


def _cursor_path() -> str | None:
    raw = os.environ.get("GC_CHAT_CURSOR", DEFAULT_CURSOR_PATH).strip()
    if raw.lower() in {"", "0", "false", "no", "off"}:
        return None
    return raw


def _rescan_seconds() -> float | None:
    """Live-discovery interval; ``0``/``off`` disables discovery."""
    raw = os.environ.get("GC_CHAT_RESCAN_SECONDS", str(CHAT_RESCAN_SECONDS)).strip()
    if raw.lower() in {"0", "false", "no", "off"}:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise GraphContextError(
            f"GC_CHAT_RESCAN_SECONDS must be a number or off, got {raw!r}"
        ) from None
    if seconds <= 0:
        raise GraphContextError("GC_CHAT_RESCAN_SECONDS must be positive or off")
    return seconds


def _sent_path(cursor_path: str | None) -> str | None:
    """The sent-message ledger rides next to the cursor (one knob)."""
    if cursor_path is None:
        return None
    path = Path(cursor_path)
    return str(path.with_name(f"{path.stem}-sent{path.suffix}"))


def _cleared_path(cursor_path: str | None) -> str | None:
    """The /clear watermark file rides next to the cursor too (WP15)."""
    if cursor_path is None:
        return None
    path = Path(cursor_path)
    return str(path.with_name(f"{path.stem}-cleared{path.suffix}"))


def _inbound(
    space_id: str, chat_id: str, message: ChatMessage
) -> InboundChatMessage:
    return InboundChatMessage(
        space_id=space_id,
        chat_id=chat_id,
        message_id=message.id,
        creator=message.creator,
        text=message.text,
        order_id=message.order_id,
    )


async def _maybe_turn(
    handler: AnytypeChatTurnHandler,
    space_id: str,
    chat_id: str,
    message: ChatMessage,
    chat_client: AnytypeChatClient,
) -> None:
    inbound = _inbound(space_id, chat_id, message)
    if not handler.accepts(inbound):
        return

    async def send(text: str, attachments: tuple[str, ...] = ()) -> str:
        return await chat_client.send(chat_id, text, attachments)

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
    # WP15: rebuild conversation memory from the already-answered slice of
    # the window (bounded by the /clear watermark) before taking turns, so
    # the first post-restart turn remembers the conversation.
    seed = handler.seed_events(chat_id, [
        _inbound(chat_client.space_id, chat_id, message) for message in window
    ])
    if seed:
        route = handler.routes[chat_id]
        await route.orchestrator.seed_memory(f"anytype:{chat_id}", seed)
        logger.info(
            "chat %s: seeded conversation memory with %d message(s)",
            chat_id, len(seed),
        )
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


async def _watch_chats(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    binding: SpaceBinding,
    runtimes: bootstrap.SpaceRuntimes,
    task_group: asyncio.TaskGroup,
    interval: float,
) -> None:
    """Live discovery (WP8): re-list a space's chats and serve new ones.

    Reads are unthrottled, so a periodic re-list is cheap. A newly created
    chat is registered (visible to the handler at once, aliased maps) and
    gets its own serve task with no restart. Never raises -- a failed
    re-list logs and retries -- so it is safe inside the bot's TaskGroup.
    """
    space_id = binding.space_id
    while True:
        await asyncio.sleep(interval)
        try:
            listed = await chat_client.list_chats()
        except GraphContextError as err:
            logger.warning("chat rescan for space %s failed: %s", space_id, err)
            continue
        names = dict(listed)
        for chat_id in served_chat_ids(binding, [cid for cid, _ in listed]):
            if chat_id in runtimes.routes:
                continue
            bootstrap.register_chat(runtimes, space_id, chat_id, names.get(chat_id, ""))
            logger.info("discovered chat %s in space %s; serving it", chat_id, space_id)
            task_group.create_task(
                _serve_chat(handler, chat_client, chat_id, handler.cursor)
            )


async def run() -> None:
    """Serve every bound space's chats until cancelled.

    Loop-composable: no logging setup, teardown in ``finally`` -- the
    consolidated server (``serve``) runs this next to the other
    transports; ``main()`` wraps it for standalone launches.
    """
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

    async def list_chats(binding: SpaceBinding) -> list[tuple[str, str]]:
        # A pinned chat needs no enumeration -- served_chat_ids ignores the
        # list for a pin; skip the API call and serve it by name-less id.
        if binding.chat_id:
            return [(binding.chat_id, "")]
        return await client_for(binding.space_id).list_chats()

    runtimes = await bootstrap.build_space_runtimes(list_chats)
    teardown = list(runtimes.teardown)
    teardown.extend(client.aclose for client in transport_clients)

    cursor_path = _cursor_path()
    handler = AnytypeChatTurnHandler(
        routes=runtimes.routes,
        spaces=runtimes.spaces,
        cursor=ChatCursor(cursor_path),
        sent=SentMessages(path=_sent_path(cursor_path)),
        clear_marks=ChatCursor(_cleared_path(cursor_path)),
        # Quirk C6 side door: the bot's own default space names its
        # identity. "" (e.g. desktop endpoint, shared account) degrades
        # to posted-id suppression alone.
        bot_identity=await discover_bot_identity(
            transport_clients[0]
        ) if transport_clients else "",
    )
    rescan = _rescan_seconds()
    try:
        served = "; ".join(
            f"{chat_id}: {desc}"
            for chat_id, desc in sorted(runtimes.descriptions.items())
        )
        logger.info("anytype chat: serving %s. %s", served, runtimes.help_line)
        # TaskGroup (not gather): the discovery watchers spawn serve tasks
        # into the same lifecycle. A brand-new chat's serve task fast-
        # forwards past its (empty) history via _catch_up's first-run path.
        async with asyncio.TaskGroup() as task_group:
            for chat_id, space_id in list(runtimes.spaces.items()):
                task_group.create_task(
                    _serve_chat(
                        handler, client_for(space_id), chat_id, handler.cursor
                    )
                )
            if rescan is not None:
                for space_id, binding in runtimes.space_bindings.items():
                    if binding.chat_id:
                        continue  # pinned: no discovery
                    task_group.create_task(_watch_chats(
                        handler, client_for(space_id), binding,
                        runtimes, task_group, rescan,
                    ))
    finally:
        await composition.run_teardown(teardown)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run()


if __name__ == "__main__":
    asyncio.run(main())
