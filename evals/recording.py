"""Trajectory capture: a wrapping driver plus the per-trial record.

``RecordingDriver`` wraps any :class:`LLMDriver` and observes what the
pipeline can't be asked to report: per-decision wall latency, the decision
count, and every tool call the driver proposed. Cost/token usage arrives
separately -- the runner hands ``ClaudeAgentDriver.on_result`` a sink for
:class:`DecideUsage` values -- because the SDK's result message is only
visible inside the real driver.

``TrialRecord`` is everything the graders read: the graph end-state, the
session the pipeline mutated, the reply events, and the trajectory. The
executed/attempted split is derived from the mode's binding, the same
table the pipeline enforces with -- a proposed call to an unbound tool was
rejected, not executed.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from graph_context.domain.graph import GraphIndex
from graph_context.domain.session import SessionState
from graph_context.orchestrator.drivers import (
    DEFAULT_OPTIONS,
    DecideOptions,
    DecideUsage,
    LLMDriver,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    latency_s: float
    tool_calls: tuple[ToolCall, ...]
    replied: bool


class RecordingDriver:
    """Transparent LLMDriver wrapper that keeps a trajectory ledger."""

    def __init__(self, inner: LLMDriver) -> None:
        self._inner = inner
        self.decisions: list[DecisionRecord] = []

    def system_prompt(self, goal: str) -> str:
        return self._inner.system_prompt(goal)

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        return self._inner.render_prompt(transcript)

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
        *,
        options: DecideOptions = DEFAULT_OPTIONS,
    ) -> LLMTurn:
        start = time.perf_counter()
        turn = await self._inner.decide(
            transcript, tools, goal, options=options
        )
        self.decisions.append(DecisionRecord(
            latency_s=time.perf_counter() - start,
            tool_calls=turn.tool_calls,
            replied=bool(turn.reply.strip()),
        ))
        return turn


@dataclass(slots=True)
class TrialRecord:
    """One trial's observable outcome, ready for grading."""

    case_id: str
    trial: int
    session_id: str
    graph: GraphIndex
    session: SessionState
    seed_ids: dict[str, str]
    baseline_nodes: int
    baseline_edges: int
    replies: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    final_reply: str = ""
    decisions: list[DecisionRecord] = field(default_factory=list)
    usages: list[DecideUsage] = field(default_factory=list)
    bound_tools: frozenset[str] = frozenset()
    system_prompt: str = ""  # the exact prompt the driver sends (format 2)
    harness_error: str = ""  # the harness (not the model) broke the trial

    @property
    def attempted_calls(self) -> list[ToolCall]:
        return [call for record in self.decisions for call in record.tool_calls]

    @property
    def executed_calls(self) -> list[ToolCall]:
        """Calls the pipeline actually ran: proposed AND bound (ADR 007 --
        the binding table is the boundary, so membership decides execution)."""
        return [c for c in self.attempted_calls if c.name in self.bound_tools]

    @property
    def node_delta(self) -> int:
        return self.graph.node_count() - self.baseline_nodes

    @property
    def total_latency_s(self) -> float:
        return sum(record.latency_s for record in self.decisions)

    @property
    def total_cost_usd(self) -> float:
        return sum(u.total_cost_usd or 0.0 for u in self.usages)

    @property
    def total_output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.usages)
