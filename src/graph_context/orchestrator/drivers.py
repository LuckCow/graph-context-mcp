"""The LLM seam: a driver decides each pipeline step (ADR 007 quarantine).

The pipeline never talks to a model framework directly; it hands a driver
the turn's transcript so far plus the tools the ACTIVE MODE binds (name ->
LLM-facing doc), and the driver answers with either tool calls or a final
reply. Everything framework-shaped -- LangGraph, the Anthropic client,
prompt assembly -- lives behind this protocol, inside this package, so a
framework swap stays orchestrator-internal.

``ScriptedDriver`` is the deterministic implementation for tests and the
demo: WP6's acceptance ("authoring mode cannot mutate") is proven by a
script that TRIES, not by trusting a model's manners.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One requested tool invocation. ``id`` is the provider's tool_use
    id when the driver reports one (the Messages API pairs results to
    calls by it); the pipeline synthesizes a deterministic id when a
    driver leaves it empty, so downstream events always carry one."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """An inbound image riding a user event (WP23).

    ``media_type`` is the exact provider-required MIME type (png / jpeg /
    gif / webp -- the transport's classification enforces the set);
    ``data_base64`` is the encoded bytes. Turn-local: cross-turn
    conversation memory keeps only the spoken text, so a follow-up turn
    no longer sees the pixels (like ``thinking``)."""

    name: str
    media_type: str
    data_base64: str


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One entry of a turn's working transcript.

    ``kind``: ``user`` (the message that started the turn), ``tool`` (a
    tool's rendered result -- or the pipeline's unavailable-tool notice,
    so a driver can self-correct), ``assistant`` (a prior reply, or the
    mid-turn tool-call decision the pipeline records before executing).

    ``tool_calls`` is set on the mid-turn ``assistant`` event (the calls
    that decision made); ``tool_use_id`` is set on ``tool`` result events
    and matches the originating call's id. Together they let a driver
    reconstruct a native tool_use/tool_result conversation.

    ``thinking`` is the reasoning that produced a mid-turn ``assistant``
    decision. Stateless drivers (a fresh session per decide) replay it so
    the model keeps its own train of thought across decisions; it stays
    turn-local -- cross-turn memory keeps only the spoken halves.

    ``server_tool_calls``/``server_tool_results`` (WP22, ADR 030
    amendment) carry a decision's provider-executed searches so the NEXT
    decide can replay what the search returned, not just what the model
    wrote about it. Results are OPAQUE provider-shaped payloads
    (JSON-serialized raw blocks), position-paired with the calls; ``""``
    means the result was never captured, and drivers must never replay
    an unpaired half. Turn-local, like ``thinking``.
    """

    kind: str
    text: str
    tool_name: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_use_id: str = ""
    thinking: str = ""
    server_tool_calls: tuple[ToolCall, ...] = ()
    server_tool_results: tuple[str, ...] = ()
    # Inbound images on a user event (WP23); drivers turn them into
    # provider image blocks. Text-file attachments need no field -- they
    # arrive folded into the text as fenced blocks.
    images: tuple[ImageAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMTurn:
    """A driver's decision: tool calls to run, the final reply, or both.

    Text bundled with tool calls is normally preamble the pipeline
    ignores; on a turn's final decision (after ``LAST_TURN_WARNING``) it
    counts as the reply, so a warned driver can land one last update AND
    answer.

    ``thinking`` is the model's reasoning text when the provider streams
    extended-thinking blocks. Diagnostics only: the turn diary records it
    so a human can see WHY a decision was made; it never re-enters the
    transcript and never counts as reply text.

    ``server_tool_calls`` are provider-executed tool invocations (web
    search, ADR 030) that ALREADY RAN inside the provider before the
    decision came back. The pipeline must never execute them; it copies
    them (with ``server_tool_results``, the position-paired opaque raw
    result payloads -- ``""`` = not captured) onto the recorded decision
    event so the next decide replays what the search returned (WP22),
    and the turn log / activity stream show them."""

    reply: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    thinking: str = ""
    server_tool_calls: tuple[ToolCall, ...] = ()
    server_tool_results: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DecideUsage:
    """What one ``decide`` call cost, translated off the SDK's result.

    Pure data so consumers (the eval harness's RecordingDriver) never need
    the claude-agent-sdk installed; ``ClaudeAgentDriver`` fills it from the
    session's ResultMessage and hands it to its ``on_result`` callback.
    ``None`` fields are values the SDK did not report.
    """

    duration_ms: int = 0
    duration_api_ms: int = 0
    total_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0


def plain_transcript(transcript: Sequence[TranscriptEvent]) -> str:
    """The trivial transcript rendering: event texts, blank-line joined.

    The ``render_prompt`` answer for drivers that send nothing anywhere
    (scripted, manual) -- the assembled prompt IS the transcript."""
    return "\n\n".join(event.text for event in transcript)


class LLMDriver(Protocol):
    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str,
        *,
        web_search: bool = False,
    ) -> LLMTurn:
        """Choose the next step given the transcript, bound tools, and the
        active mode's goal prompt (ADR 015 -- the system-prompt fragment).

        ``web_search`` admits the provider's server-side web search tool
        for this decision (ADR 030); drivers without one ignore it."""
        ...

    def system_prompt(self, goal: str) -> str:
        """The exact system prompt this driver sends for ``goal``.

        The prompt-capture seam: the turn diary logs what the model
        actually received, so a driver that assembles more than the goal
        (ClaudeAgentDriver appends its guidance) must answer with the
        assembled string -- from the same code path that sends it."""
        ...

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        """The exact prompt string this driver sends for ``transcript``.

        The other half of the prompt-capture seam: ``system_prompt`` is
        the standing input, this is the per-turn one. A driver must answer
        from the same rendering code path ``decide`` sends through
        (ClaudeAgentDriver fences tool results and prior replies), so the
        diary shows the model's true input, not a reconstruction."""
        ...


class ScriptedDriver:
    """Plays back a fixed list of turns; deterministic by construction.

    The script does not consume transcript or tool docs -- that blindness
    is the point: tests assert on what the PIPELINE does with each
    decision, including decisions the binding must reject.
    """

    def __init__(self, turns: Sequence[LLMTurn]) -> None:
        self._turns = list(turns)
        self._cursor = 0

    def system_prompt(self, goal: str) -> str:
        return goal  # nothing sends it anywhere; the goal IS the prompt

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        return plain_transcript(transcript)  # ditto

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
        *,
        web_search: bool = False,
    ) -> LLMTurn:
        if self._cursor >= len(self._turns):
            return LLMTurn(reply="(script exhausted)")
        turn = self._turns[self._cursor]
        self._cursor += 1
        return turn
