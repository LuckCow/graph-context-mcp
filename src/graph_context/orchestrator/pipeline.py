"""``Orchestrator.handle_message``: the transport-agnostic entry seam (WP6).

One message = one turn: the pipeline runs the driver/tool loop until the
driver replies (or the tool budget runs out) and returns the turn's reply
events. Transports (CLI now; Telegram/Slack in WP8) stay thin adapters
over this function -- ``session_id`` is the transport's thread/channel,
``user_id`` its user handle (unused until WP8's authz/attribution, logged
today).

Mode switching is an EXPLICIT user command (settled in WP6): ``/mode
<name>`` over whatever specs the deployment loaded (ADR 015), handled here
so every transport gets it for free. Mode is per-session; the underlying
focus-stack session is still the process-wide one (per-session
``SessionState`` is WP8).

Turn-local transcripts: the driver sees the current turn's events only.
Cross-turn conversation memory is deliberately the DRIVER's concern (the
LangGraph thread arrives with the framework) -- the seam's contract stays
"message in, reply events out".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from graph_context.application.intent_recorder import IntentRecorder, ToolTrace
from graph_context.interface.profiles import DomainProfile, ModeSpec
from graph_context.interface.tools import Services
from graph_context.orchestrator import capture, modes
from graph_context.orchestrator.drivers import LLMDriver, TranscriptEvent
from graph_context.orchestrator.modes import ModeRegistry

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_CALLS = 8  # per turn; a loop guard, not a feature


@dataclass(frozen=True, slots=True)
class ReplyEvent:
    """One transport-neutral output of a turn.

    ``kind``: ``reply`` (the model's answer), ``notice`` (harness-produced
    -- mode switches, budget exhaustion), ``error`` (harness-produced,
    actionable).
    """

    text: str
    kind: str = "reply"


@dataclass(slots=True)
class _SessionState:
    mode: str  # a loaded ModeSpec name


@dataclass(slots=True)
class Orchestrator:
    """The pipeline: modes bind tools, a driver decides, tools run here.

    ``provenance`` is the WP7 subsystem toggle: when an IntentRecorder is
    wired, every turn ends by draining the journal -- one intent node per
    mutating turn, nothing for read-only turns -- and authoring-mode
    replies that mention known nodes are auto-captured as Prose (the
    capture is journalled too, so the intent links the artifact). ``None``
    switches the whole subsystem off.
    """

    services: Services
    driver: LLMDriver
    profile: DomainProfile
    registry: ModeRegistry
    provenance: IntentRecorder | None = None
    model_name: str = ""  # attribution for intent nodes (gc_model)
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    _sessions: dict[str, _SessionState] = field(default_factory=dict)

    def mode_of(self, session_id: str) -> str:
        return self._session(session_id).mode

    def _spec(self, state: _SessionState) -> ModeSpec:
        spec = self.registry.get(state.mode)
        assert spec is not None  # sessions only ever hold loaded names
        return spec

    async def handle_message(
        self, session_id: str, user_id: str, text: str
    ) -> list[ReplyEvent]:
        state = self._session(session_id)
        stripped = text.strip()
        if stripped.startswith("/mode"):
            return [self._switch_mode(state, stripped)]

        spec = self._spec(state)
        logger.info(
            "turn session=%s user=%s mode=%s", session_id, user_id, spec.name
        )
        tools = modes.tool_docs(spec, self.profile)
        transcript: list[TranscriptEvent] = [TranscriptEvent("user", stripped)]
        events: list[ReplyEvent] = []
        trace: list[ToolTrace] = []
        reply_text = ""
        for _ in range(self.max_tool_calls):
            turn = await self.driver.decide(transcript, tools, spec.goal)
            if not turn.tool_calls:
                reply_text = turn.reply
                events.append(ReplyEvent(reply_text))
                break
            for call in turn.tool_calls:
                trace.append(ToolTrace(
                    call.name, json.dumps(dict(call.arguments), default=str)
                ))
                result = await modes.invoke(
                    spec, call.name, self.services, call.arguments
                )
                if result is None:
                    # The binding boundary's runtime face: actionable for a
                    # driver, visible to the user as a notice.
                    result = (
                        f"tool {call.name!r} is not available in "
                        f"{spec.name} mode; available: "
                        f"{', '.join(sorted(tools))}"
                    )
                    events.append(ReplyEvent(result, kind="error"))
                transcript.append(TranscriptEvent("tool", result, tool_name=call.name))
        else:
            events.append(ReplyEvent(
                f"tool budget exhausted ({self.max_tool_calls} calls) before "
                "the driver replied; the turn was cut short.",
                kind="notice",
            ))
        await self._finish_turn(spec, user_id, stripped, reply_text, trace)
        return events

    async def _finish_turn(
        self,
        spec: ModeSpec,
        user_id: str,
        prompt: str,
        reply_text: str,
        trace: list[ToolTrace],
    ) -> None:
        """WP7 turn boundary: auto-capture (per the spec's policy), then
        drain -> intent node."""
        if self.provenance is None:
            return
        policy = spec.capture
        if policy is not None and reply_text:
            references = capture.entity_links(
                reply_text, self.services.repository.graph
            )
            if capture.should_capture(reply_text, references, policy.min_chars):
                # The captured artifact journals itself, so the intent node
                # below links prompt -> intent -> artifact.
                await self.services.prose.record(
                    text=reply_text,
                    summary=reply_text.strip().splitlines()[0][:200],
                    references=references,
                )
        mutations = self.services.journal.drain()
        intent = await self.provenance.record_turn(
            prompt=prompt,
            mutations=mutations,
            trace=trace,
            user_id=user_id,
            model=self.model_name,
        )
        if intent is not None:
            logger.info("provenance: recorded %s (%d touches)",
                        intent.id, len(mutations))

    def _session(self, session_id: str) -> _SessionState:
        return self._sessions.setdefault(
            session_id, _SessionState(mode=self.registry.default)
        )

    def _switch_mode(self, state: _SessionState, command: str) -> ReplyEvent:
        argument = command.removeprefix("/mode").strip().lower()
        if not argument:
            return ReplyEvent(
                f"mode: {state.mode}; switch with /mode "
                f"{{{' | '.join(self.registry.names())}}}",
                kind="notice",
            )
        spec = self.registry.get(argument)
        if spec is None:
            return ReplyEvent(
                f"unknown mode {argument!r}; allowed: "
                f"{', '.join(self.registry.names())}",
                kind="error",
            )
        state.mode = spec.name
        bound = ", ".join(sorted(modes.binding_for(spec)))
        return ReplyEvent(
            f"mode switched to {spec.name}; bound tools: {bound}",
            kind="notice",
        )
