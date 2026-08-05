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

Stream roster (WP35, ADR 043): only the ``GC_CHAT_STREAM_CAP`` (default
20) most recently active chats per space hold live SSE streams -- each
stream pins a pooled connection for its lifetime, so a space with
hundreds of chats must not stream them all. Hibernated chats stay
registered (sessions, scheduled events, and replies work stream-less)
and the rescan watcher wakes them the tick after their
``last_message_date`` (quirk C13) makes them the newest; catch-up then
answers the message. Capping requires the rescan watcher; with it off
the cap is ignored loudly.

Config: ANYTYPE_API_KEY(_FILE) / ANYTYPE_BASE_URL family (endpoint-
agnostic: the desktop app today, the headless sidecar after cutover),
GC_SPACES_FILE (required), GC_CHAT_CURSOR, GC_CHAT_RESCAN_SECONDS (live
chat discovery), GC_CHAT_STREAM_CAP (live streams per space, WP35;
default 20, ``off`` streams every chat),
GC_GRAPH_RESYNC_SECONDS (periodic out-of-band resync;
both default 60, ``off`` disables), GC_SCHEDULE_TICK_SECONDS (scheduled-
event firing, ADR 027; default 30, ``off`` disables),
GC_CHANGE_TICK_SECONDS (the unified change tick, ADR 044: automation
rules ADR 039 + mode auto-refresh; default 5, ``off`` disables both;
GC_RULE_TICK_SECONDS, the pre-ADR-044 name, honored as a compat
alias), plus the usual
GC_DRIVER / GC_PROFILE / GC_MODES_FILE / provenance knobs.

Run:  python -m graph_context.orchestrator.anytype_chat_bot
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import logging
import os
import random
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path

from graph_context import composition
from graph_context.application.scheduler import DueEvent
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.chat import (
    AnytypeChatClient,
    ChatMessage,
    ChatSummary,
    discover_bot_identity,
)
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.logging_setup import configure_logging
from graph_context.orchestrator import bootstrap, prose_bridge
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
    plan_streams,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import ImageAttachment
from graph_context.orchestrator.pipeline import ReplyEvent, is_command
from graph_context.orchestrator.prose_bridge import ProseBridge
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
STREAM_CAP = 20  # live SSE streams per space (WP35); GC_CHAT_STREAM_CAP
# Every open stream pins one connection from the space client's httpx
# pool (default 100) for its lifetime, shared with the request path --
# uncapped, a space with ~90+ chats silently starves its own writes. 20
# leaves ample headroom; hibernated chats wake within one rescan tick.
GRAPH_RESYNC_SECONDS = 60  # out-of-band edit poll; GC_GRAPH_RESYNC_SECONDS
SCHEDULE_TICK_SECONDS = 30  # scheduled-event scan (ADR 027); GC_SCHEDULE_TICK_SECONDS
CHANGE_TICK_SECONDS = 5  # change-listener scan (ADR 044); GC_CHANGE_TICK_SECONDS
# 5s keeps rule reactions feeling immediate; the tick runs its own cheap
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


def _stream_cap() -> int | None:
    """Live streams per space (WP35); ``0``/``off`` streams every chat."""
    raw = os.environ.get("GC_CHAT_STREAM_CAP", str(STREAM_CAP)).strip()
    if raw.lower() in OFF_VALUES - {""}:
        return None
    try:
        cap = int(raw)
    except ValueError:
        raise GraphContextError(
            f"GC_CHAT_STREAM_CAP must be an integer or off, got {raw!r}"
        ) from None
    if cap <= 0:
        raise GraphContextError("GC_CHAT_STREAM_CAP must be positive or off")
    return cap


def _graph_resync_seconds() -> float | None:
    """Out-of-band resync interval; ``0``/``off`` disables the poll."""
    return _interval_seconds("GC_GRAPH_RESYNC_SECONDS", GRAPH_RESYNC_SECONDS)


def _schedule_tick_seconds() -> float | None:
    """Scheduled-event scan interval; ``0``/``off`` disables firing."""
    return _interval_seconds("GC_SCHEDULE_TICK_SECONDS", SCHEDULE_TICK_SECONDS)


def _change_tick_seconds() -> float | None:
    """Change-tick interval; ``0``/``off`` disables every change listener
    (automation rules AND mode auto-refresh). GC_RULE_TICK_SECONDS -- the
    pre-ADR-044 name for what was then only the rule tick -- is honored
    when the new name is unset, so existing deployments keep their
    setting."""
    if (
        "GC_CHANGE_TICK_SECONDS" not in os.environ
        and "GC_RULE_TICK_SECONDS" in os.environ
    ):
        return _interval_seconds("GC_RULE_TICK_SECONDS", CHANGE_TICK_SECONDS)
    return _interval_seconds("GC_CHANGE_TICK_SECONDS", CHANGE_TICK_SECONDS)


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


@dataclasses.dataclass
class _SpaceStreams:
    """One space's live-stream bookkeeping (WP35, ADR 043).

    ``tasks`` maps streamed chats to their serve tasks; ``busy`` holds
    chats currently inside catch-up or a turn (marked by the serve task
    itself -- single-threaded asyncio makes a sync check-then-cancel
    race-free); ``catch_up`` holds chats hibernated at startup whose
    offline backlog (ADR 019) the watcher still owes one stream-less
    catch-up. ``cap=None`` streams everything (pre-WP35 behavior).
    """

    cap: int | None = None
    tasks: dict[str, asyncio.Task[None]] = dataclasses.field(
        default_factory=dict
    )
    busy: set[str] = dataclasses.field(default_factory=set)
    catch_up: set[str] = dataclasses.field(default_factory=set)


@contextlib.contextmanager
def _busy(marks: set[str], chat_id: str) -> Iterator[None]:
    """Mark the chat unevictable while a turn or catch-up runs; the
    roster tick skips busy chats so a hibernation never aborts a turn."""
    marks.add(chat_id)
    try:
        yield
    finally:
        marks.discard(chat_id)


async def _serve_chat(
    handler: AnytypeChatTurnHandler,
    chat_client: AnytypeChatClient,
    chat_id: str,
    cursor: ChatCursor,
    titler: ChatTitler | None = None,
    busy: set[str] | None = None,
) -> None:
    space_id = chat_client.space_id
    marks = set() if busy is None else busy
    with _busy(marks, chat_id):
        await _catch_up(handler, chat_client, chat_id, cursor, titler)
    delay = 1.0
    while True:
        try:
            with _busy(marks, chat_id):
                await _sweep_confirms(handler, chat_client, chat_id)
            async for event in chat_client.stream(chat_id):
                delay = 1.0  # a live stream resets the backoff
                with _busy(marks, chat_id):
                    if event.kind == "reactions_updated" and event.message_id:
                        # WP33: a 👍 on a tracked confirm message applies
                        # the schema proposal -- harness-executed, no
                        # model turn.
                        await _maybe_reaction(
                            handler, chat_client, chat_id,
                            event.message_id, dict(event.reactions),
                        )
                        continue
                    if event.kind != "message_added" or event.message is None:
                        continue  # edits/deletes/heartbeats: no turns
                    await _maybe_turn(
                        handler, space_id, chat_id, event.message,
                        chat_client, titler,
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
    streams: _SpaceStreams | None = None,
) -> None:
    """Live discovery (WP8) + the stream roster (WP35): re-list a space's
    chats, serve new ones, and keep live streams on the most recent.

    Reads are unthrottled, so a periodic re-list is cheap. A newly created
    chat is registered (visible to the handler at once, aliased maps) and
    adopted from its beginning, so the message that opened the thread is
    answered even though it predates the subscription. Already-served
    chats get their listed NAME refreshed (WP21: a human's rename must
    reach the titler's untitled test).

    Which chats hold serve tasks is ``plan_streams``' verdict each tick,
    ranked on the same re-list's ``last_message_date`` (C13): a message
    into a hibernated chat makes it the newest, so the wake IS the
    discovery poll -- no extra requests. Stops only hit idle chats
    (``streams.busy``, plus chats holding a pending schema confirm whose
    👍 only arrives over SSE); the plan is computed and applied with no
    await in between, so a turn can never start on a chat between the
    busy check and the cancel. Before the first tick the watcher pays the
    ADR 019 debt for chats hibernated at startup: one stream-less
    catch-up each, so offline backlog is answered even where no stream
    opens (retried until it lands; a chat the roster wakes first is
    dropped here -- its serve task catches up instead). Never raises --
    a failed re-list logs and retries -- so it is safe inside the bot's
    TaskGroup.
    """
    space_id = binding.space_id
    streams = streams if streams is not None else _SpaceStreams()
    while True:
        for chat_id in sorted(streams.catch_up):
            try:
                await _catch_up(
                    handler, chat_client, chat_id, handler.cursor, titler
                )
            except GraphContextError as err:
                logger.warning(
                    "hibernated chat %s catch-up failed: %s; retrying next "
                    "tick", chat_id, err,
                )
            else:
                streams.catch_up.discard(chat_id)
        await asyncio.sleep(interval)
        try:
            listed = await chat_client.list_chats()
        except GraphContextError as err:
            logger.warning("chat rescan for space %s failed: %s", space_id, err)
            continue
        names = {c.id: c.name for c in listed}
        served = served_chat_ids(binding, [c.id for c in listed])
        for chat_id in served:
            if chat_id in runtimes.routes:
                runtimes.chat_names[chat_id] = names.get(chat_id, "").strip()
                continue
            bootstrap.register_chat(runtimes, space_id, chat_id, names.get(chat_id, ""))
            logger.info("discovered chat %s in space %s", chat_id, space_id)
            # A discovered chat was born while the bot ran: adopt it from
            # its beginning, so the message(s) typed before this
            # subscription opened -- the thread's opener, typically --
            # count as offline backlog for _catch_up, not skippable
            # first-run history. The roster below decides its stream.
            handler.cursor.begin(chat_id)
        for chat_id in [c for c, t in streams.tasks.items() if t.done()]:
            del streams.tasks[chat_id]
        activity = {c.id: c.last_message_date for c in listed}
        plan = plan_streams(
            {chat_id: activity.get(chat_id, "") for chat_id in served},
            active=set(streams.tasks),
            busy=streams.busy | {
                chat_id for chat_id in streams.tasks
                if handler.confirms_in(chat_id)
            },
            cap=streams.cap,
        )
        for chat_id in plan.stop:
            streams.tasks.pop(chat_id).cancel()
            logger.info(
                "chat %s hibernated (space %s: %d stream(s) live)",
                chat_id, space_id, len(streams.tasks),
            )
        for chat_id in plan.start:
            streams.catch_up.discard(chat_id)  # the serve task catches up
            streams.tasks[chat_id] = task_group.create_task(_serve_chat(
                handler, chat_client, chat_id, handler.cursor, titler,
                busy=streams.busy,
            ))
            logger.info("serving chat %s in space %s", chat_id, space_id)


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
        logger.info(
            "scheduled event %r (%s) delivered to chat %s",
            due.name, due.node_id, chat_id,
        )
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


ChangeListener = Callable[[frozenset[str]], Awaitable[None]]


async def _watch_changes(
    route: ChannelRoute,
    space_id: str,
    interval: float,
    listeners: Sequence[tuple[str, ChangeListener]],
) -> None:
    """React to out-of-band space edits (ADR 044; fourth sibling watcher).

    One tick = one modified-since resync + the ordered listeners, all
    under the turn lock. Unlike ``_watch_schedule`` -- a pure read riding
    ``_watch_graph``'s resync -- the tick runs its OWN resync first:
    reacting to a checkbox a minute late reads as broken, and the
    modified-since search is a few localhost calls against the
    unthrottled sidecar. Each listener keeps its own baseline diff, so
    the tick is idempotent and loop-free (a listener's writes never read
    as changes). A failing listener logs and never starves the next one,
    and nothing here raises -- safe inside the bot's TaskGroup. A future
    on-change feature is one listener in ``_change_listeners``, not a
    new watcher.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with route.lock:
                changed = await route.orchestrator.resync_graph()
                for name, listener in listeners:
                    try:
                        await listener(changed)
                    except GraphContextError as err:
                        logger.warning(
                            "%s listener for space %s failed: %s",
                            name, space_id, err,
                        )
                    except Exception:  # never take the serve loop down
                        logger.exception(
                            "%s listener for space %s crashed",
                            name, space_id,
                        )
        except GraphContextError as err:
            logger.warning(
                "change tick for space %s failed: %s", space_id, err
            )
            continue
        except Exception:  # never take the serve loop down
            logger.exception("change tick for space %s crashed", space_id)
            continue


def _change_listeners(route: ChannelRoute) -> list[tuple[str, ChangeListener]]:
    """The unified tick's reactions, in order (ADR 044).

    Rules run first -- reaction latency is their 5s contract (ADR 039) --
    then the mode-registry refresh (fingerprint-gated, free on the
    no-change tick), then the revision historian (WP41: compares changed
    tracked nodes to its baselines; free when nothing tracked changed).
    Names are for the watcher's per-listener failure logs.
    """

    async def rules(_changed: frozenset[str]) -> None:
        report = await route.orchestrator.rule_tick()
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

    async def refresh_modes(_changed: frozenset[str]) -> None:
        await route.orchestrator.refresh_modes()

    async def history(changed: frozenset[str]) -> None:
        # WP41 (ADR 049): human edits to tracked nodes become revisions.
        await route.orchestrator.history_tick(changed)

    return [("rules", rules), ("modes", refresh_modes), ("history", history)]


async def run(prose: ProseBridge | None = None) -> None:
    """Serve every bound space's chats until cancelled.

    Loop-composable: no logging setup, teardown in ``finally`` -- the
    consolidated server (``serve``) runs this next to the other
    transports; ``main()`` wraps it for standalone launches. ``prose``
    (WP43) is the inspection server's space registry: each bootstrapped
    space registers its historian/repository handles so the prose page
    can read blame and write marks through the bot loop; None (the
    standalone bot, viewer off) skips registration entirely.
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

    # The startup listing, kept per space: the roster ranks its initial
    # stream selection on the same last_message_date (C13) the bootstrap
    # enumeration already fetched.
    latest: dict[str, list[ChatSummary]] = {}

    async def list_chats(binding: SpaceBinding) -> list[tuple[str, str]]:
        # A pinned chat needs no enumeration -- served_chat_ids ignores the
        # list for a pin; skip the API call and serve it by name-less id.
        if binding.chat_id:
            return [(binding.chat_id, "")]
        summaries = await client_for(binding.space_id).list_chats()
        latest[binding.space_id] = summaries
        return [(c.id, c.name) for c in summaries]

    runtimes = await bootstrap.build_space_runtimes(list_chats)
    teardown = list(runtimes.teardown)
    teardown.extend(client.aclose for client in transport_clients)

    if prose is not None:
        # WP43: hand each live space to the prose page. Registration
        # runs ON this serving loop -- that captured loop is what the
        # inspection server's thread schedules calls onto.
        for space_id, route in runtimes.space_routes.items():
            if route.orchestrator.historian is None:
                continue
            binding = runtimes.space_bindings[space_id]
            prose_bridge.register_space(
                prose,
                space_id=space_id,
                label=binding.project or space_id,
                historian=route.orchestrator.historian,
                repository=route.orchestrator.services.repository,
                route_lock=route.lock,
            )

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
    change_tick = _change_tick_seconds()
    cap = _stream_cap()
    if cap is not None and rescan is None:
        # The rescan watcher is the only wake mechanism; capping without
        # it would leave hibernated chats deaf forever.
        logger.warning(
            "GC_CHAT_STREAM_CAP ignored: GC_CHAT_RESCAN_SECONDS is off, so "
            "hibernated chats could never wake; streaming every chat"
        )
        cap = None
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
            space_streams: dict[str, _SpaceStreams] = {}
            for space_id, binding in runtimes.space_bindings.items():
                # WP35: stream the cap most recently active chats; the
                # rest stay registered (sessions, scheduled events, and
                # replies all work stream-less) and wake through the
                # rescan watcher, which also owes them one catch-up for
                # any offline backlog (ADR 019).
                streams = _SpaceStreams(cap=None if binding.chat_id else cap)
                space_streams[space_id] = streams
                space_chats = [
                    cid for cid, sid in runtimes.spaces.items()
                    if sid == space_id
                ]
                activity = {
                    c.id: c.last_message_date
                    for c in latest.get(space_id, [])
                }
                plan = plan_streams(
                    {cid: activity.get(cid, "") for cid in space_chats},
                    active=set(), cap=streams.cap,
                )
                for chat_id in plan.start:
                    streams.tasks[chat_id] = task_group.create_task(
                        _serve_chat(
                            handler, client_for(space_id), chat_id,
                            handler.cursor, titler, busy=streams.busy,
                        )
                    )
                streams.catch_up = set(space_chats) - set(plan.start)
                if streams.catch_up:
                    logger.info(
                        "space %s: streaming %d of %d chats (cap %s); the "
                        "rest hibernate and wake on activity",
                        space_id, len(streams.tasks), len(space_chats),
                        streams.cap,
                    )
            if rescan is not None:
                for space_id, binding in runtimes.space_bindings.items():
                    if binding.chat_id:
                        continue  # pinned: no discovery
                    task_group.create_task(_watch_chats(
                        handler, client_for(space_id), binding,
                        runtimes, task_group, rescan, titler,
                        streams=space_streams[space_id],
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
            if change_tick is not None:
                for space_id, route in runtimes.space_routes.items():
                    task_group.create_task(_watch_changes(
                        route, space_id, change_tick,
                        _change_listeners(route),
                    ))
    finally:
        await composition.run_teardown(teardown)


async def main() -> None:
    configure_logging()
    await run()


if __name__ == "__main__":
    asyncio.run(main())
