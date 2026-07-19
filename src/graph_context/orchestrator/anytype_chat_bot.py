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
only a chat with NO position fast-forwards past its history. A chat the
rescan watcher discovers mid-run is adopted from its beginning instead
(``ChatCursor.begin``): it was born while the bot ran, so the messages
typed before the subscription opened are unanswered conversation, not
history.

Reconnects: the client's SSE read timeout is tied to the heartbeat, so a
half-dead stream raises instead of hanging; this loop reconnects with
capped exponential backoff + jitter, and the cursor makes reconnect
replays turn-free.

Config: ANYTYPE_API_KEY(_FILE) / ANYTYPE_BASE_URL family (endpoint-
agnostic: the desktop app today, the headless sidecar after cutover),
GC_SPACES_FILE (required), GC_CHAT_CURSOR, GC_CHAT_RESCAN_SECONDS (live
chat discovery), GC_GRAPH_RESYNC_SECONDS (periodic out-of-band resync;
both default 60, ``off`` disables), GC_SCHEDULE_TICK_SECONDS (scheduled-
event firing, ADR 027; default 30, ``off`` disables),
GC_RULE_TICK_SECONDS (automation-rule firing, ADR 039; default 5,
``off`` disables), plus the usual
GC_DRIVER / GC_PROFILE / GC_MODES_FILE / provenance knobs.

Run:  python -m graph_context.orchestrator.anytype_chat_bot
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import os
import random
from collections.abc import Mapping
from pathlib import Path

from graph_context import composition
from graph_context.application.scheduler import DueEvent
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
    IMAGE_MEDIA_TYPES,
    MAX_TEXT_BYTES,
    TITLE_GOAL,
    AnytypeChatTurnHandler,
    ChatCursor,
    ChatTitler,
    DeleteFn,
    EditFn,
    InboundAttachment,
    InboundChatMessage,
    SendFileFn,
    SendFn,
    SentMessages,
    attachment_note,
    classify_attachment,
    fenced_file,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import ImageAttachment
from graph_context.orchestrator.pipeline import ReplyEvent, is_command
from graph_context.orchestrator.rendering import TURN_FAILED_NOTICE
from graph_context.orchestrator.spaces import SpaceBinding, served_chat_ids
from graph_context.orchestrator.turn_activity import ChatActivity
from graph_context.orchestrator.turn_log import OFF_VALUES

logger = logging.getLogger(__name__)

DEFAULT_CURSOR_PATH = "logs/chat_cursor.json"
CATCHUP_WINDOW = 100  # the messages endpoint's recency window (C2)
_RECONNECT_CAP_SECONDS = 60.0
CHAT_RESCAN_SECONDS = 3  # live-discovery poll (WP8); GC_CHAT_RESCAN_SECONDS
# 3s makes new-chat pickup near-instant: sidecar reads are unthrottled
# (S7), so a tight re-list costs nothing. Raise this when pointing at a
# throttled desktop endpoint.
GRAPH_RESYNC_SECONDS = 60  # out-of-band edit poll; GC_GRAPH_RESYNC_SECONDS
SCHEDULE_TICK_SECONDS = 30  # scheduled-event scan (ADR 027); GC_SCHEDULE_TICK_SECONDS
RULE_TICK_SECONDS = 5  # automation-rule scan (ADR 039); GC_RULE_TICK_SECONDS
# 5s keeps reactions feeling immediate; the tick runs its own cheap
# modified-since resync (unthrottled sidecar), so it does not wait for
# the 60s graph poll. Raise this on a throttled desktop endpoint.


def _cursor_path() -> str | None:
    raw = os.environ.get("GC_CHAT_CURSOR", DEFAULT_CURSOR_PATH).strip()
    if raw.lower() in OFF_VALUES:
        return None
    return raw


def _interval_seconds(env: str, default: float) -> float | None:
    """A positive polling interval from ``env``; ``0``/``off`` disables.
    An empty value is NOT off here -- it errors loudly below, because a
    blank interval is more likely a broken export than a choice."""
    raw = os.environ.get(env, str(default)).strip()
    if raw.lower() in OFF_VALUES - {""}:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise GraphContextError(
            f"{env} must be a number or off, got {raw!r}"
        ) from None
    if seconds <= 0:
        raise GraphContextError(f"{env} must be positive or off")
    return seconds


def _rescan_seconds() -> float | None:
    """Live-discovery interval; ``0``/``off`` disables discovery."""
    return _interval_seconds("GC_CHAT_RESCAN_SECONDS", CHAT_RESCAN_SECONDS)


def _graph_resync_seconds() -> float | None:
    """Out-of-band resync interval; ``0``/``off`` disables the poll."""
    return _interval_seconds("GC_GRAPH_RESYNC_SECONDS", GRAPH_RESYNC_SECONDS)


def _schedule_tick_seconds() -> float | None:
    """Scheduled-event scan interval; ``0``/``off`` disables firing."""
    return _interval_seconds("GC_SCHEDULE_TICK_SECONDS", SCHEDULE_TICK_SECONDS)


def _rule_tick_seconds() -> float | None:
    """Automation-rule scan interval; ``0``/``off`` disables the engine."""
    return _interval_seconds("GC_RULE_TICK_SECONDS", RULE_TICK_SECONDS)


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
        creator_name=message.creator_name,
        attachments=tuple(
            InboundAttachment(target=a.target, type=a.type)
            for a in message.attachments
        ),
    )


async def _resolve_attachments(
    chat_client: AnytypeChatClient, message: InboundChatMessage
) -> tuple[list[str], list[ImageAttachment]]:
    """A message's attachments -> (text parts, images) for the turn (WP23).

    Classification is the transport's pure policy; this owns the I/O:
    facts first (name/type/size -- no download), then bytes only for
    what the model will actually get. Text files inline as fenced
    blocks, images become native blocks, everything else a note -- and
    a single unreadable attachment degrades to its own note, never the
    turn."""
    parts: list[str] = []
    images: list[ImageAttachment] = []
    for attachment in message.attachments:
        try:
            facts = await chat_client.attachment_facts(attachment.target)
            name = str(facts["name"] or attachment.target)
            extension = str(facts["extension"] or "")
            display = f"{name}.{extension}" if extension else name
            size = int(facts["size_in_bytes"] or 0)
            kind = classify_attachment(
                str(facts["type_key"]), size, extension
            )
            if kind == "object":
                # An ordinary graph-object card: name it so the model can
                # find_node it; nothing to download.
                parts.append(f"[attached object: {name}]")
                continue
            if kind == "stub":
                reason = (
                    "too large to read here"
                    if str(facts["type_key"]) in ("image", "file")
                    and size > MAX_TEXT_BYTES
                    else "a type the assistant cannot read"
                )
                parts.append(attachment_note(display, size, reason))
                continue
            content, media = await chat_client.fetch_file(attachment.target)
            media = media.partition(";")[0].strip().lower()
            if kind == "image":
                if media not in IMAGE_MEDIA_TYPES:
                    parts.append(attachment_note(
                        display, len(content),
                        f"an image format the assistant cannot read ({media})",
                    ))
                    continue
                images.append(ImageAttachment(
                    name=display, media_type=media,
                    data_base64=base64.b64encode(content).decode("ascii"),
                ))
            else:  # text
                parts.append(fenced_file(
                    display, content.decode("utf-8", errors="replace")
                ))
        except GraphContextError as err:
            logger.warning(
                "attachment %s unreadable (chat=%s): %s",
                attachment.target, message.chat_id, err,
            )
            parts.append(
                f"[an attachment could not be read: {attachment.target}]"
            )
    return parts, images



def _reply_primitives(
    chat_client: AnytypeChatClient, chat_id: str
) -> tuple[SendFn, EditFn, SendFileFn, DeleteFn]:
    """The send/edit/send-file/delete primitives a turn needs, bound to
    one chat (delete serves the activity sink, not the reply)."""

    async def send(text: str, attachments: tuple[str, ...] = ()) -> str:
        return await chat_client.send(chat_id, text, attachments)

    async def edit(
        message_id: str, text: str, attachments: tuple[str, ...] = ()
    ) -> None:
        await chat_client.edit(chat_id, message_id, text, attachments)

    async def send_file(name: str, content: str) -> str:
        # WP23: upload, then one message carrying the file as a card.
        file_id = await chat_client.upload_file(
            name, content.encode("utf-8")
        )
        return await chat_client.send_file_message(
            chat_id, f"\N{PAPERCLIP} {name}", file_id
        )

    async def delete(message_id: str) -> None:
        await chat_client.delete(chat_id, message_id)

    return send, edit, send_file, delete


async def _maybe_turn(
    handler: AnytypeChatTurnHandler,
    space_id: str,
    chat_id: str,
    message: ChatMessage,
    chat_client: AnytypeChatClient,
    titler: ChatTitler | None = None,
) -> None:
    inbound = _inbound(space_id, chat_id, message)
    if not handler.accepts(inbound):
        return

    send, edit, send_file, delete = _reply_primitives(chat_client, chat_id)

    # Errors deliver through the same reply, so they replace the turn's
    # "Processing…" placeholder instead of stranding it in the chat --
    # and when the turn streamed activity (WP19), the error posts fresh
    # (the sink claimed the placeholder) and the activity message is
    # deleted like on the happy path.
    reply = handler.reply(send, edit, send_file)
    activity = ChatActivity(reply=reply, edit=edit, delete=delete)
    try:
        images: list[ImageAttachment] = []
        if inbound.attachments:
            parts, images = await _resolve_attachments(chat_client, inbound)
            text = "\n\n".join(
                piece for piece in (inbound.text.strip(), *parts) if piece
            )
            if not text and images:
                text = "(the user sent the attached image(s))"
            inbound = dataclasses.replace(inbound, text=text)
        events = await handler.run_turn(inbound, reply, activity, images=images)
    except GraphContextError as err:
        # Config-shaped errors are actionable; show them in-chat.
        await reply.deliver(f"[error] {err}")
        await activity.close(ok=False)
        return
    except Exception:  # a turn must never take the serve loop down
        logger.exception("turn failed (chat=%s)", chat_id)
        await reply.deliver(TURN_FAILED_NOTICE)
        await activity.close(ok=False)
        return
    if titler is not None:
        await _maybe_title(titler, handler, inbound, events, chat_client)


async def _maybe_title(
    titler: ChatTitler,
    handler: AnytypeChatTurnHandler,
    inbound: InboundChatMessage,
    events: list[ReplyEvent],
    chat_client: AnytypeChatClient,
) -> None:
    """Claude-app-style auto-title after a chat's first real exchange
    (WP21, ADR 031). One driver side-call + one rename PATCH, once per
    chat lifetime, AFTER the reply is already delivered -- off the
    user-visible path, and a failure never fails the turn.
    """
    if is_command(inbound.text) or not titler.needs_title(inbound.chat_id):
        return
    reply_text = next(
        (event.text for event in events if event.kind == "reply"), ""
    )
    if not reply_text.strip():
        return  # error/notice-only turn: wait for a real exchange
    titler.mark_attempted(inbound.chat_id)  # win or lose, one attempt
    route = handler.routes[inbound.chat_id]
    try:
        turn = await route.orchestrator.driver.decide(
            titler.prompt_events(inbound.text, reply_text), {}, TITLE_GOAL
        )
        title = titler.sanitize(turn.reply)
        if not title:
            logger.warning(
                "chat %s: title side-call produced nothing usable",
                inbound.chat_id,
            )
            return
        await chat_client.rename(inbound.chat_id, title)
        titler.record(inbound.chat_id, title)
        logger.info("titled chat %s: %r", inbound.chat_id, title)
    except GraphContextError as err:
        logger.warning("chat titling failed (chat=%s): %s", inbound.chat_id, err)
    except Exception:  # titling must never take the serve loop down
        logger.exception("chat titling failed (chat=%s)", inbound.chat_id)


async def _catch_up(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    cursor: ChatCursor,
    titler: ChatTitler | None = None,
) -> None:
    """First-run chats skip history; resumed chats answer the offline gap.
    (A live-discovered chat counts as resumed: discovery positions its
    cursor at the beginning, making the pre-subscription messages the gap.)
    """
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
            handler, chat_client.space_id, chat_id, message, chat_client,
            titler,
        )


async def _maybe_reaction(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    message_id: str,
    reactions: Mapping[str, tuple[str, ...]],
) -> None:
    """Route one reaction change into the WP33 confirm handler; a failure
    must never take the serve loop down (the _maybe_turn discipline)."""
    send, _, _, _ = _reply_primitives(chat_client, chat_id)
    try:
        await handler.handle_reaction(chat_id, message_id, reactions, send)
    except Exception:
        logger.exception("reaction handling failed (chat=%s)", chat_id)


async def _sweep_confirms(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
) -> None:
    """Re-read tracked confirm messages after a stream (re)connect: C12
    reaction frames are NOT replayed with the backlog, so a 👍 made
    during a drop is only visible on the message list."""
    tracked = set(handler.confirms_in(chat_id))
    if not tracked:
        return
    for message in await chat_client.recent_messages(chat_id):
        if message.id in tracked and message.reactions:
            await _maybe_reaction(
                handler, chat_client, chat_id, message.id, message.reactions
            )


async def _serve_chat(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    cursor: ChatCursor,
    titler: ChatTitler | None = None,
) -> None:
    space_id = chat_client.space_id
    await _catch_up(handler, chat_client, chat_id, cursor, titler)
    delay = 1.0
    while True:
        try:
            await _sweep_confirms(handler, chat_client, chat_id)
            async for event in chat_client.stream(chat_id):
                delay = 1.0  # a live stream resets the backoff
                if event.kind == "reactions_updated" and event.message_id:
                    # WP33: a 👍 on a tracked confirm message applies the
                    # schema proposal -- harness-executed, no model turn.
                    await _maybe_reaction(
                        handler, chat_client, chat_id,
                        event.message_id, dict(event.reactions),
                    )
                    continue
                if event.kind != "message_added" or event.message is None:
                    continue  # edits/deletes/heartbeats: no turns
                await _maybe_turn(
                    handler, space_id, chat_id, event.message, chat_client,
                    titler,
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
    titler: ChatTitler | None = None,
) -> None:
    """Live discovery (WP8): re-list a space's chats and serve new ones.

    Reads are unthrottled, so a periodic re-list is cheap. A newly created
    chat is registered (visible to the handler at once, aliased maps) and
    gets its own serve task with no restart -- adopted from its beginning,
    so the message that opened the thread is answered even though it
    predates the subscription. Already-served chats get
    their listed NAME refreshed (WP21: a human's rename must reach the
    titler's untitled test). Never raises -- a failed re-list logs and
    retries -- so it is safe inside the bot's TaskGroup.
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
                runtimes.chat_names[chat_id] = names.get(chat_id, "").strip()
                continue
            bootstrap.register_chat(runtimes, space_id, chat_id, names.get(chat_id, ""))
            logger.info("discovered chat %s in space %s; serving it", chat_id, space_id)
            # A discovered chat was born while the bot ran: adopt it from
            # its beginning, so the message(s) typed before this
            # subscription opened -- the thread's opener, typically --
            # count as offline backlog for _catch_up, not skippable
            # first-run history.
            handler.cursor.begin(chat_id)
            task_group.create_task(
                _serve_chat(handler, chat_client, chat_id, handler.cursor, titler)
            )


async def _watch_graph(
    route: ChannelRoute, space_id: str, interval: float
) -> None:
    """Periodic resync (the graph-side sibling of :func:`_watch_chats`).

    Humans edit the space in the Anytype UI while the bot runs; without a
    poll the shared index only refreshes when a turn happens to resync
    (a stale index once answered "no match" for a project created two
    minutes earlier -- and minted a duplicate). Holds the route's turn
    lock so a resync never interleaves with a turn on the same space.
    Never raises -- a failed poll logs and retries -- so it is safe
    inside the bot's TaskGroup.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with route.lock:
                changed = await route.orchestrator.resync_graph()
        except GraphContextError as err:
            logger.warning("graph resync for space %s failed: %s", space_id, err)
            continue
        if changed:
            logger.info(
                "graph resync for space %s: %d node(s) changed out-of-band",
                space_id, len(changed),
            )


async def _fire_scheduled(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    due: DueEvent,
) -> None:
    """Deliver one due Scheduled Event's turn (same error posture as
    ``_maybe_turn``: the failure replaces the placeholder, never the loop)."""

    send, edit, send_file, _ = _reply_primitives(chat_client, chat_id)

    reply = handler.reply(send, edit, send_file)
    try:
        await handler.run_scheduled(chat_id, due, reply)
    except GraphContextError as err:
        await reply.deliver(f"[error] scheduled event {due.name!r}: {err}")
    except Exception:  # a fired event must never take the serve loop down
        logger.exception(
            "scheduled event %s failed (chat=%s)", due.node_id, chat_id
        )
        await reply.deliver(
            f"[error] scheduled event {due.name!r}: the turn failed; see "
            "the bot log for the traceback"
        )


async def _watch_schedule(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    route: ChannelRoute,
    space_id: str,
    interval: float,
) -> None:
    """Fire due Scheduled Events (ADR 027; third sibling of the watchers).

    Every tick scans the shared index (a pure read; ``_watch_graph``'s
    resync keeps it fresh for events humans create/edit in the Anytype
    UI), arms recurring strays, and fires what is due -- the fired turn
    itself takes the route's turn lock inside ``run_scheduled``. Never
    raises -- a failed tick logs and retries -- so it is safe inside the
    bot's TaskGroup.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            tick = route.orchestrator.scheduled_tick()
        except GraphContextError as err:
            logger.warning("schedule scan for space %s failed: %s", space_id, err)
            continue
        for node_id in tick.arm:
            try:
                async with route.lock:
                    await route.orchestrator.arm_scheduled(node_id)
                logger.info("armed recurring scheduled event %s", node_id)
            except GraphContextError as err:
                logger.warning(
                    "could not arm scheduled event %s: %s", node_id, err
                )
        for due in tick.fire:
            chat_id = handler.target_chat(space_id, due.session_key)
            if chat_id is None:
                logger.warning(
                    "scheduled event %r (%s) is due but space %s serves no "
                    "chat; retrying next tick", due.name, due.node_id, space_id,
                )
                continue
            logger.info(
                "firing scheduled event %r (%s) into chat %s",
                due.name, due.node_id, chat_id,
            )
            try:
                await _fire_scheduled(handler, chat_client, chat_id, due)
            except GraphContextError as err:
                # Even the error DELIVERY failed (e.g. the chat API is
                # down). Already marked fired unless marking itself
                # failed; either way the loop must survive.
                logger.warning(
                    "scheduled event %s could not be delivered: %s",
                    due.node_id, err,
                )


async def _watch_rules(
    route: ChannelRoute, space_id: str, interval: float
) -> None:
    """Fire due Automation Rules (ADR 039; fourth sibling watcher).

    Unlike ``_watch_schedule`` -- a pure read riding ``_watch_graph``'s
    resync -- each rule tick runs its OWN resync first, under the turn
    lock: reacting to a checkbox a minute late reads as broken, and the
    modified-since search is a few localhost calls against the
    unthrottled sidecar. The engine's baseline diff makes the tick
    idempotent and loop-free (its own writes never read as transitions).
    Never raises -- a failed tick logs and retries -- so it is safe
    inside the bot's TaskGroup.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with route.lock:
                await route.orchestrator.resync_graph()
                report = await route.orchestrator.rule_tick()
        except GraphContextError as err:
            logger.warning("rule tick for space %s failed: %s", space_id, err)
            continue
        except Exception:  # the engine must never take the serve loop down
            logger.exception("rule tick for space %s crashed", space_id)
            continue
        for firing in report.fired:
            logger.info(
                "rule %r fired %r on %r (%s)",
                firing.rule_name, firing.action, firing.node_name,
                firing.node_id,
            )
        for problem in report.errors:
            logger.warning(
                "rule %r (%s) recorded an error: %s",
                problem.rule_name, problem.rule_id, problem.message,
            )
        for node_id in report.healed:
            logger.info("rule %s healed: config parses again", node_id)


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
    # WP21: Claude-app-style auto-titling. The names map is the same
    # object register_chat and the rescan watcher write, so a human's
    # title is always respected.
    titler = ChatTitler(names=runtimes.chat_names)
    rescan = _rescan_seconds()
    graph_resync = _graph_resync_seconds()
    schedule_tick = _schedule_tick_seconds()
    rule_tick = _rule_tick_seconds()
    try:
        served = "; ".join(
            f"{chat_id}: {desc}"
            for chat_id, desc in sorted(runtimes.descriptions.items())
        )
        logger.info("anytype chat: serving %s. %s", served, runtimes.help_line)
        # TaskGroup (not gather): the discovery watchers spawn serve tasks
        # into the same lifecycle. A discovered chat is adopted from its
        # beginning (cursor.begin), so _catch_up answers anything typed
        # before the subscription opened instead of skipping it.
        async with asyncio.TaskGroup() as task_group:
            for chat_id, space_id in list(runtimes.spaces.items()):
                task_group.create_task(
                    _serve_chat(
                        handler, client_for(space_id), chat_id,
                        handler.cursor, titler,
                    )
                )
            if rescan is not None:
                for space_id, binding in runtimes.space_bindings.items():
                    if binding.chat_id:
                        continue  # pinned: no discovery
                    task_group.create_task(_watch_chats(
                        handler, client_for(space_id), binding,
                        runtimes, task_group, rescan, titler,
                    ))
            if graph_resync is not None:
                for space_id, route in runtimes.space_routes.items():
                    task_group.create_task(
                        _watch_graph(route, space_id, graph_resync)
                    )
            if schedule_tick is not None:
                for space_id, route in runtimes.space_routes.items():
                    task_group.create_task(_watch_schedule(
                        handler, client_for(space_id), route, space_id,
                        schedule_tick,
                    ))
            if rule_tick is not None:
                for space_id, route in runtimes.space_routes.items():
                    task_group.create_task(
                        _watch_rules(route, space_id, rule_tick)
                    )
    finally:
        await composition.run_teardown(teardown)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run()


if __name__ == "__main__":
    asyncio.run(main())
