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
class TranscriptEvent:
    """One entry of a turn's working transcript.

    ``kind``: ``user`` (the message that started the turn), ``tool`` (a
    tool's rendered result -- or the pipeline's unavailable-tool notice,
    so a driver can self-correct), ``assistant`` (a prior reply).
    """

    kind: str
    text: str
    tool_name: str = ""


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMTurn:
    """A driver's decision: tool calls to run, the final reply, or both.

    Text bundled with tool calls is normally preamble the pipeline
    ignores; on a turn's final decision (after ``LAST_TURN_WARNING``) it
    counts as the reply, so a warned driver can land one last update AND
    answer."""

    reply: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


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


class LLMDriver(Protocol):
    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str,
    ) -> LLMTurn:
        """Choose the next step given the transcript, bound tools, and the
        active mode's goal prompt (ADR 015 -- the system-prompt fragment)."""
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

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
    ) -> LLMTurn:
        if self._cursor >= len(self._turns):
            return LLMTurn(reply="(script exhausted)")
        turn = self._turns[self._cursor]
        self._cursor += 1
        return turn
