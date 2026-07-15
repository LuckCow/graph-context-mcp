"""``Orchestrator.handle_message``: the transport-agnostic entry seam (WP6).

One message = one turn: the pipeline runs the driver/tool loop until the
driver replies (or the tool budget runs out) and returns the turn's reply
events. Transports (CLI now; Telegram/Slack in WP8) stay thin adapters
over this function -- ``session_id`` is the transport's thread/channel,
``user_id`` its user handle (provenance attribution + logs), ``sender``
the human-readable display name the model gets to see (see
:func:`sender_attributed`).

Mode switching is an EXPLICIT user command (settled in WP6): ``/mode
<name>`` over whatever specs the deployment loaded (ADR 015), handled here
so every transport gets it for free. Every ``/mode`` first refreshes the
registry through the injected ``reload_registry`` hook (ADR 015 amendment:
in-space Activity Mode objects), so an edit made in Anytype applies
without a restart; a failed refresh degrades to the last good registry
with an actionable error. Mode is per-session; the underlying graph-session
session is still the process-wide one (per-session ``SessionState`` is
WP8).

Cross-turn context (WP15, ADR 020): each turn opens with the session's
context block -- scratchpad, curated working set, recent trail -- built
ONCE per turn by ``interface.context_block`` and prepended to the
turn-local transcript (every ``decide`` re-renders the transcript into a
fresh CLI session, so the block rides each decision without being
re-assembled). An empty session injects nothing. The block is state the
model curates via the ``context`` tool; :class:`ConversationMemory` is
the other half -- a bounded per-session ring of prior user/assistant
messages replayed ahead of the block, cleared by the ``/clear`` command
(handled here, like ``/mode``, so every transport gets it). Transports
can prime the ring after a restart via :meth:`Orchestrator.seed_memory`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from graph_context.application.intent_recorder import IntentRecorder, ToolTrace
from graph_context.application.scheduler import SchedulerTick
from graph_context.errors import GraphContextError
from graph_context.interface.context_block import build_turn_context
from graph_context.interface.profiles import DomainProfile, ModeSpec
from graph_context.interface.services import Services
from graph_context.interface.tools import is_error_result, resync_out_of_band
from graph_context.orchestrator import capture, modes
from graph_context.orchestrator.drivers import (
    LLMDriver,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.modes import ModeRegistry
from graph_context.orchestrator.turn_log import TurnLog

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_CALLS = 16  # per turn; a loop guard, not a feature

# Injected before the budget's final decide so the driver lands the turn
# instead of being cut off mid-plan; its consumer is an LLM.
LAST_TURN_WARNING = (
    "[harness] Tool budget: this is your FINAL decision for this turn. "
    "You may include one last batch of tool calls -- they WILL be "
    "executed, but no results will come back to you. Put your final "
    "answer to the user in this same message as text: whatever text you "
    "send now IS your reply."
)


@dataclass(frozen=True, slots=True)
class ReplyEvent:
    """One transport-neutral output of a turn.

    ``kind``: ``reply`` (the model's answer), ``notice`` (harness-produced
    -- mode switches, budget exhaustion), ``error`` (harness-produced,
    actionable).
    """

    text: str
    kind: str = "reply"


class TurnObserver(Protocol):
    """Async per-turn event tap for live activity surfaces (WP19, ADR 029).

    Passed per ``handle_message`` call (its identity is per-turn -- a chat
    transport binds it to one chat's activity message), unlike the
    process-lifetime ``turn_log`` field. ``None`` observers cost nothing.
    ``turn_started``'s ``detail`` is the ACTIVE MODE's ``activity_detail``
    (a ModeSpec property: picking a mode picks its verbosity; the renderer
    alone interprets the levels). Contract: implementations MUST NOT raise
    -- delivery failures degrade internally (the TurnLog posture: a
    diagnostic never takes a turn down). Command turns (``/mode``,
    ``/clear``) return before ``turn_started``, so they never stream.
    """

    async def turn_started(self, mode: str, detail: str) -> None: ...

    async def decision(self, turn: LLMTurn) -> None: ...

    async def tool_result(
        self, call: ToolCall, result: str, ok: bool
    ) -> None: ...


def scheduled_prompt(name: str, prompt: str) -> str:
    """The transcript form of a fired Scheduled Event (WP18, ADR 027).

    The consumer is the LLM waking up with no triggering user message: the
    framing says why the turn exists and that the stored instructions --
    written by its past self or the user -- are to be acted on now, in
    chat. The single format rule lives here (like ``sender_attributed``)
    so every transport fires events identically.
    """
    return (
        f"[scheduled event {name!r} fired] This turn was started by the "
        "scheduler, not by a user message. Follow these stored "
        f"instructions now, replying in the chat as usual: {prompt}"
    )


def sender_attributed(text: str, sender: str) -> str:
    """The transcript form of a user message with a known sender.

    A session can be a shared surface (an Anytype space chat, a Discord
    channel), so "assign this to me"-shaped requests are unanswerable
    unless each message says who sent it -- the model otherwise sees only
    bare text (live-caught: Task Creation Mode could not fill Assignee =
    the requester). The prefix rides into conversation memory and startup
    seeding too, so replayed history keeps its attribution; the single
    format rule lives here. An empty sender (CLI, MCP, name unknown)
    leaves the message bare.
    """
    return f"[from {sender}] {text}" if sender else text


DEFAULT_MEMORY_EVENTS = 16   # ~8 turns of (user, reply) pairs
DEFAULT_MEMORY_CHARS = 6000  # evict oldest beyond this total


class ConversationMemory:
    """Bounded ring of prior user/assistant messages (WP15).

    The pipeline replays it at the head of each turn's transcript so the
    driver sees the conversation so far; ``/clear`` empties it. Bounded
    twice -- event count and total characters -- because either alone
    lets one pathological turn crowd out everything else. Eviction is
    oldest-first and event-granular; a turn's user half may outlive its
    reply half, which reads fine in transcript form.
    """

    def __init__(
        self,
        max_events: int = DEFAULT_MEMORY_EVENTS,
        max_chars: int = DEFAULT_MEMORY_CHARS,
    ) -> None:
        self._max_chars = max_chars
        self._events: deque[tuple[str, str]] = deque(maxlen=max_events)

    def remember_turn(self, user_text: str, reply_text: str) -> None:
        self._events.append(("user", user_text))
        self._events.append(("assistant", reply_text))
        self._shrink()

    def seed(self, events: Sequence[tuple[str, str]]) -> None:
        """Replace the ring with reconstructed history (startup catch-up).

        ``events`` is (kind, text) oldest-first, kinds ``user`` /
        ``assistant``; the same bounds apply, so an oversized history
        keeps only its tail.
        """
        self._events.clear()
        for kind, text in events:
            self._events.append((kind, text))
        self._shrink()

    def clear(self) -> None:
        self._events.clear()

    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(
            TranscriptEvent(kind, text) for kind, text in self._events
        )

    def __len__(self) -> int:
        return len(self._events)

    def _shrink(self) -> None:
        while self._events and sum(
            len(text) for _, text in self._events
        ) > self._max_chars:
            self._events.popleft()


@dataclass(slots=True)
class _SessionState:
    mode: str  # a loaded ModeSpec name; authoritative in-memory (WP8)
    services: Services  # this session's view: own SessionState, shared space
    memory: ConversationMemory = field(default_factory=ConversationMemory)
    # The last (mode, goal, bound tools) logged as a `prompt` diary event;
    # a change (first turn, /mode switch, registry edit) re-logs so the
    # diary always holds the prompt the NEXT decisions actually run with.
    logged_prompt: tuple[str, str, tuple[str, ...]] | None = None


@dataclass(slots=True)
class Orchestrator:
    """The pipeline: modes bind tools, a driver decides, tools run here.

    ``provenance`` is the WP7 subsystem toggle: when an IntentRecorder is
    wired, every turn ends by draining the journal -- one intent node per
    mutating turn, nothing for read-only turns -- and authoring-mode
    replies that mention known nodes are auto-captured as Prose (the
    capture is journalled too, so the intent links the artifact). ``None``
    switches the whole subsystem off.

    ``turn_log`` is the same shape of toggle for the full-fidelity turn
    diary: when wired, every message logs its input, the assembled
    prompt the driver sends, each driver decision, each tool call with
    its complete output, and the final reply events (see
    ``turn_log.py``). ``None`` logs nothing.
    """

    services: Services
    driver: LLMDriver
    profile: DomainProfile
    registry: ModeRegistry
    provenance: IntentRecorder | None = None
    turn_log: TurnLog | None = None
    # ADR 015 amendment: re-reads every config source (profile defaults,
    # GC_MODES_FILE, in-space Activity Mode objects). None (tests, no
    # config store) keeps the loaded registry for the process lifetime.
    reload_registry: Callable[[], Awaitable[ModeRegistry]] | None = None
    model_name: str = ""  # attribution for intent nodes (gc_model)
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    # WP8 (ADR 021): the per-session-key Services seam wired by the
    # composition root. None (tests, bare construction) degrades to the
    # shared `services` bundle for every session -- pre-WP8 behavior.
    services_for: Callable[[str], Awaitable[Services]] | None = None
    _sessions: dict[str, _SessionState] = field(default_factory=dict)

    def mode_of(self, session_id: str) -> str:
        """Non-creating peek: the mode a session IS in (default if unseen)."""
        state = self._sessions.get(session_id)
        return state.mode if state is not None else self.registry.default

    def services_of(self, session_id: str) -> Services | None:
        """Non-creating peek: the Services view a session runs on, or
        ``None`` for a session no turn has touched yet (ADR 021)."""
        state = self._sessions.get(session_id)
        return state.services if state is not None else None

    async def resync_graph(self) -> frozenset[str]:
        """Pull edits made directly in Anytype into the shared index.

        The transports' periodic-refresh entry point (all sessions share
        one repository); the same path as the context tool's resync
        action, so the embedding cache stays in step with the graph.
        """
        return await resync_out_of_band(self.services)

    # -- scheduled events (WP18, ADR 027): the transports' scheduler
    # loop calls these; the rules live in application.scheduler. The
    # shared bundle is correct here (like resync_graph): scheduled
    # events belong to the space, not to any one session. --------------

    def scheduled_tick(self) -> SchedulerTick:
        """One due-scan over the shared graph (pure read)."""
        return self.services.scheduler.tick()

    async def arm_scheduled(self, node_id: str) -> None:
        """Anchor a UI-created recurring event without firing it."""
        await self.services.scheduler.arm(node_id)

    async def mark_scheduled_fired(self, node_id: str) -> None:
        """Stamp an event as fired (call BEFORE its turn runs)."""
        await self.services.scheduler.mark_fired(node_id)

    def _spec(self, state: _SessionState) -> ModeSpec:
        spec = self.registry.get(state.mode)
        if spec is None:
            # The mode vanished in a registry refresh (possibly another
            # session's /mode). Degrade to the default rather than dying.
            logger.warning(
                "mode %r no longer loaded; falling back to %r",
                state.mode, self.registry.default,
            )
            state.mode = self.registry.default
            state.services.session.mode = state.mode  # persisted mirror
            spec = self.registry.get(state.mode)
            assert spec is not None  # the default is always loaded
        return spec

    async def handle_message(
        self, session_id: str, user_id: str, text: str, origin: str = "",
        sender: str = "", observer: TurnObserver | None = None,
    ) -> list[ReplyEvent]:
        state = await self._session(session_id)
        stripped = text.strip()
        # One id per handle_message call ties this turn's diary records --
        # user query, driver decisions, tool calls, final replies -- into
        # one group even when sessions interleave in the shared log file.
        turn_id = uuid.uuid4().hex[:12]
        if self.turn_log:
            self.turn_log.user_message(
                turn_id, session_id, state.mode, user_id, stripped,
                sender=sender,
            )
        if stripped.startswith("/mode"):
            mode_events = await self._switch_mode(state, stripped)
            if self.turn_log:
                # state.mode is post-switch: the mode the session is IN now.
                self.turn_log.turn_end(
                    turn_id, session_id, state.mode, mode_events
                )
            return mode_events
        if stripped == "/clear":
            state.memory.clear()
            clear_events = [ReplyEvent(
                "conversation memory cleared. The scratchpad and working "
                "set are kept -- reset those with the context tool "
                "(action='note' with empty text / action='clear').",
                kind="notice",
            )]
            if self.turn_log:
                self.turn_log.turn_end(
                    turn_id, session_id, state.mode, clear_events
                )
            return clear_events

        spec = self._spec(state)
        logger.info(
            "turn session=%s user=%s mode=%s", session_id, user_id, spec.name
        )
        if observer:
            await observer.turn_started(spec.name, spec.activity_detail)
        tools = modes.tool_docs(spec, self.profile)
        if self.turn_log:
            fingerprint = (spec.name, spec.goal, tuple(sorted(tools)))
            if state.logged_prompt != fingerprint:
                self.turn_log.prompt(
                    turn_id, session_id, spec.name, spec.goal,
                    self.driver.system_prompt(spec.goal), tools,
                )
                state.logged_prompt = fingerprint
        # [prior conversation..., context block, the live message]: history
        # reads as conversation; the block stays adjacent to the message.
        transcript: list[TranscriptEvent] = list(state.memory.events())
        context_block = await build_turn_context(state.services)
        if context_block:
            transcript.append(TranscriptEvent("user", context_block))
            if self.turn_log:
                self.turn_log.context(
                    turn_id, session_id, spec.name, context_block
                )
        spoken = sender_attributed(stripped, sender)
        transcript.append(TranscriptEvent("user", spoken))
        if self.turn_log:
            # The assembled prompt as the driver will render it for the
            # FIRST decision; later decisions add only tool results, which
            # the diary already records in full.
            self.turn_log.llm_prompt(
                turn_id, session_id, spec.name,
                self.driver.render_prompt(transcript),
            )
        events: list[ReplyEvent] = []
        trace: list[ToolTrace] = []
        reply_text = ""
        for decisions_left in range(self.max_tool_calls, 0, -1):
            final_decision = decisions_left == 1
            if final_decision:
                transcript.append(TranscriptEvent("user", LAST_TURN_WARNING))
            turn = await self.driver.decide(transcript, tools, spec.goal)
            if self.turn_log:
                self.turn_log.llm_turn(turn_id, session_id, spec.name, turn)
            if observer:
                await observer.decision(turn)
            if not turn.tool_calls:
                reply_text = turn.reply
                events.append(ReplyEvent(reply_text))
                break
            # Record the decision itself before executing: drivers that
            # round-trip native tool_use/tool_result blocks need to see
            # their own calls in the transcript, paired to results by id.
            # Ids are guaranteed here (deterministic -- no clocks/random)
            # so downstream events always carry one even for drivers that
            # report none (scripted playback).
            tool_calls = tuple(
                call if call.id else dataclasses.replace(
                    call, id=f"toolu_gc_{decisions_left}_{index}"
                )
                for index, call in enumerate(turn.tool_calls)
            )
            transcript.append(
                TranscriptEvent(
                    "assistant", turn.reply, tool_calls=tool_calls,
                    thinking=turn.thinking,
                )
            )
            for call in tool_calls:
                trace.append(ToolTrace(
                    call.name, json.dumps(dict(call.arguments), default=str)
                ))
                result = await modes.invoke(
                    spec, call.name, state.services, call.arguments
                )
                unavailable = result is None
                if result is None:
                    # The binding boundary's runtime face: actionable for a
                    # driver, visible to the user as a notice.
                    result = (
                        f"tool {call.name!r} is not available in "
                        f"{spec.name} mode; available: "
                        f"{', '.join(sorted(tools))}"
                    )
                    events.append(ReplyEvent(result, kind="error"))
                if self.turn_log:
                    self.turn_log.tool_result(
                        turn_id, session_id, spec.name, call, result
                    )
                if observer:
                    await observer.tool_result(
                        call, result,
                        ok=not (unavailable or is_error_result(result)),
                    )
                transcript.append(
                    TranscriptEvent(
                        "tool", result, tool_name=call.name, tool_use_id=call.id
                    )
                )
            if final_decision and turn.reply.strip():
                # The warned driver bundled its answer with a last update:
                # the calls just ran, the text is the reply.
                reply_text = turn.reply
                events.append(ReplyEvent(reply_text))
                break
        else:
            events.append(ReplyEvent(
                f"tool budget exhausted ({self.max_tool_calls} decisions): "
                "the final tool calls ran, but the driver bundled no reply "
                "text despite the warning; the turn was cut short.",
                kind="notice",
            ))
        await self._finish_turn(
            state.services, spec, user_id, stripped, reply_text, trace, origin
        )
        if reply_text:
            # Error-only / budget-exhausted turns leave no useful memory.
            # The attributed form: replayed history must keep who spoke.
            state.memory.remember_turn(spoken, reply_text)
        if self.turn_log:
            self.turn_log.turn_end(turn_id, session_id, spec.name, events)
        return events

    async def seed_memory(
        self, session_id: str, events: Sequence[tuple[str, str]]
    ) -> None:
        """Prime a session's conversation memory from reconstructed history
        (transports call this once at startup, after a restart)."""
        (await self._session(session_id)).memory.seed(events)

    async def _finish_turn(
        self,
        services: Services,
        spec: ModeSpec,
        user_id: str,
        prompt: str,
        reply_text: str,
        trace: list[ToolTrace],
        origin: str = "",
    ) -> None:
        """WP7 turn boundary: auto-capture (per the spec's policy), then
        drain -> intent node. ``services`` is the session's view; its
        repository/capture/journal are the runtime's shared instances."""
        if self.provenance is None:
            return
        policy = spec.capture
        if policy is not None and reply_text:
            references = capture.entity_links(
                reply_text, services.repository.graph
            )
            if capture.should_capture(reply_text, references, policy.min_chars):
                # The captured artifact journals itself, so the intent node
                # below links prompt -> intent -> artifact. Its type comes
                # from the policy: gc_prose for fiction, a native type
                # (procedure, note, ...) for other activities (ADR 015).
                await services.capture.record(
                    text=reply_text,
                    summary=reply_text.strip().splitlines()[0][:200],
                    references=references,
                    artifact_type=policy.artifact_type,
                    references_label=policy.references_label,
                )
        mutations = services.journal.drain()
        intent = await self.provenance.record_turn(
            prompt=prompt,
            mutations=mutations,
            trace=trace,
            user_id=user_id,
            model=self.model_name,
            mode=spec.name,
            origin=origin,
        )
        if intent is not None:
            logger.info("provenance: recorded %s (%d touches)",
                        intent.id, len(mutations))

    async def _session(self, session_id: str) -> _SessionState:
        """The per-session state, created (with I/O) on first sight.

        WP8: the session's Services come from the injected ``services_for``
        factory (its own SessionState over the shared space); without a
        factory (tests, bare construction) every session shares the
        runtime bundle -- pre-WP8 behavior. The mode is restored from the
        persisted SessionState when it names a loaded spec, else the
        registry default; the in-memory ``_SessionState.mode`` stays
        authoritative from there (a shared-runtime session must keep its
        own mode even if the persisted mirror can't).
        """
        state = self._sessions.get(session_id)
        if state is not None:
            return state
        if self.services_for is not None:
            services = await self.services_for(session_id)
        else:
            services = self.services
        persisted = services.session.mode
        if persisted and self.registry.get(persisted) is not None:
            mode = persisted
        else:
            if persisted:
                logger.info(
                    "session %s persisted mode %r is not loaded; using %r",
                    session_id, persisted, self.registry.default,
                )
            mode = self.registry.default
            services.session.mode = mode
        state = _SessionState(mode=mode, services=services)
        self._sessions[session_id] = state
        return state

    async def _switch_mode(
        self, state: _SessionState, command: str
    ) -> list[ReplyEvent]:
        events = await self._refresh_registry()
        if self.registry.get(state.mode) is None:
            events.append(ReplyEvent(
                f"mode {state.mode!r} is no longer loaded; back to "
                f"{self.registry.default!r}",
                kind="notice",
            ))
            state.mode = self.registry.default
        argument = command.removeprefix("/mode").strip().lower()
        if not argument:
            current = self.registry.get(state.mode)
            detail = current.activity_detail if current else ""
            events.append(ReplyEvent(
                f"mode: {state.mode} (activity detail: {detail}); "
                f"switch with /mode {{{' | '.join(self.registry.names())}}}",
                kind="notice",
            ))
            return events
        spec = self.registry.get(argument)
        if spec is None:
            events.append(ReplyEvent(
                f"unknown mode {argument!r}; allowed: "
                f"{', '.join(self.registry.names())}",
                kind="error",
            ))
            return events
        state.mode = spec.name
        bound = ", ".join(sorted(modes.binding_for(spec)))
        events.append(ReplyEvent(
            f"mode switched to {spec.name}; bound tools: {bound}",
            kind="notice",
        ))
        events.extend(await self._persist_mode(state))
        return events

    async def _persist_mode(self, state: _SessionState) -> list[ReplyEvent]:
        """Mirror the switched mode into the session snapshot so it
        survives a restart (WP8). A store outage must not un-switch the
        mode or fail the turn -- it degrades to an in-memory-only switch
        with a notice."""
        state.services.session.mode = state.mode
        persister = state.services.persister
        if persister is None:
            return []
        try:
            await persister.flush()
        except GraphContextError as err:
            logger.warning("could not persist mode switch: %s", err)
            return [ReplyEvent(
                "mode switched, but it could not be saved; it will reset to "
                "the default on restart.",
                kind="notice",
            )]
        return []

    async def _refresh_registry(self) -> list[ReplyEvent]:
        """Re-read mode config; on failure keep the last good registry.

        Startup already validated the sources once (loudly); here a human
        may have broken an Activity Mode object in the Anytype UI while
        the server runs, so the turn loop must survive it -- the error
        names the object and field, and the next /mode retries.
        """
        if self.reload_registry is None:
            return []
        try:
            self.registry = await self.reload_registry()
        except Exception as err:  # noqa: BLE001  -- any refresh failure degrades
            logger.warning("mode registry refresh failed: %s", err, exc_info=True)
            return [ReplyEvent(
                f"mode reload failed, keeping the previously loaded modes: "
                f"{err}",
                kind="error",
            )]
        return []
