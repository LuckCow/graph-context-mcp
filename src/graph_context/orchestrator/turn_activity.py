"""Live turn activity for the Anytype chat (WP19, ADR 029).

While a turn runs, the "Processing…" placeholder becomes an ACTIVITY
message: :class:`ChatActivity` (a pipeline ``TurnObserver``) folds each
driver decision and tool result into :class:`ActivityLog` and PATCHes the
rendered text in place. Anytype has no write-side streaming API, but every
edit reaches watching clients instantly as a ``message_updated`` SSE
event -- edit-in-place IS the streaming mechanism. The final reply posts
as a fresh message (the transport claims the placeholder away from
:class:`TurnReply`), and ``close`` collapses the activity message into a
compact done-summary.

Detail levels are interpreted HERE and nowhere else (the ACTIVE MODE
owns the setting -- ``ModeSpec.activity_detail``, so switching modes
switches verbosity; this renderer owns what each level shows):

* ``minimal`` -- decision counter plus a deduped tool-name tally.
* ``tools`` -- one line per tool call with an argument summary and its
  ok/error mark.
* ``full`` -- also thinking snippets, interim model text, and result
  excerpts (for the newest decision when the budget bites).

Rate-limit hygiene (the API allows a burst of 60 requests, then ~1
request/second sustained, shared with the turn's own graph writes):
edits coalesce on the leading edge -- at most one per
:data:`ACTIVITY_EDIT_SECONDS`, later events fold silently and the next
edit (or the closing collapse, which is unconditional) flushes them.
An activity edit failure never fails the turn: every PATCH degrades to a
logged warning.

Pure logic like the transport itself: no httpx, no infrastructure
imports; the send/edit primitives arrive from the composition root.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from graph_context.orchestrator.anytype_chat_transport import (
    ANYTYPE_MESSAGE_LIMIT,
    EditFn,
    TurnReply,
)
from graph_context.orchestrator.drivers import LLMTurn, ToolCall

logger = logging.getLogger(__name__)

# Leading-edge coalescing floor between activity edits. Worst case (a
# 16-decision turn with a couple of tool calls each) stays under half the
# sustained request budget, leaving room for the turn's own writes.
ACTIVITY_EDIT_SECONDS = 2.0

_THINKING_SNIP = 200   # chars of thinking / interim text at `full`
_RESULT_SNIP = 150     # chars of a tool result excerpt at `full`
_ARGS_SNIP = 80        # chars of an argument summary per call line


def _snip(text: str, limit: int) -> str:
    """One display line: whitespace collapsed, hard-capped with an ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _args_summary(arguments: Mapping[str, Any]) -> str:
    parts = (f"{key}={value!r}" for key, value in arguments.items())
    return _snip(", ".join(parts), _ARGS_SNIP)


def _mark(ok: bool | None) -> str:
    if ok is None:
        return "…"  # dispatched, result not back yet
    return "✓" if ok else "✗"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


@dataclass
class _Call:
    name: str
    arguments: Mapping[str, Any]
    ok: bool | None = None
    result: str = ""


@dataclass
class _Decision:
    thinking: str = ""
    preamble: str = ""  # interim model text riding a tool-calling decision
    calls: list[_Call] = field(default_factory=list)


@dataclass
class ActivityLog:
    """The pure fold: turn events in, the activity message's text out.

    Keeps structured entries so :meth:`render` can degrade deterministically
    under the message-size budget -- excerpts drop from all but the newest
    decision first, then the oldest decisions collapse wholesale into one
    ``… n earlier steps`` line; the header and the newest decision always
    survive.
    """

    detail: str  # "minimal" | "tools" | "full" -- "off" never builds one
    _decisions: list[_Decision] = field(default_factory=list)
    _pending: deque[_Call] = field(default_factory=deque)

    @property
    def decisions(self) -> int:
        return len(self._decisions)

    @property
    def tool_calls(self) -> int:
        return sum(len(d.calls) for d in self._decisions)

    @property
    def errors(self) -> int:
        return sum(
            1 for d in self._decisions for c in d.calls if c.ok is False
        )

    def note_decision(self, turn: LLMTurn) -> None:
        decision = _Decision(
            thinking=turn.thinking,
            preamble=turn.reply,
            calls=[_Call(c.name, dict(c.arguments)) for c in turn.tool_calls],
        )
        self._decisions.append(decision)
        self._pending.extend(decision.calls)

    def note_tool_result(self, name: str, result: str, ok: bool) -> None:
        """Results arrive in dispatch order (the pipeline executes a
        decision's calls sequentially), so pairing is first-in-first-out."""
        if self._pending:
            call = self._pending.popleft()
        else:  # a result with no announced call: keep the record anyway
            if not self._decisions:
                self._decisions.append(_Decision())
            call = _Call(name, {})
            self._decisions[-1].calls.append(call)
        call.ok = ok
        call.result = result

    # -- rendering ---------------------------------------------------------

    def render(self, limit: int = ANYTYPE_MESSAGE_LIMIT) -> str:
        header = f"working… decision {self.decisions}"
        if self.detail == "minimal":
            tally = self._tally()
            text = header + (f"\ntools: {tally}" if tally else "")
            return text[:limit]
        acting = [d for d in self._decisions if d.calls]
        for all_detailed in (True, False):
            blocks = [
                self._block(d, detailed=all_detailed or d is acting[-1])
                for d in acting
            ]
            text = self._assemble(header, [], blocks)
            if len(text) <= limit:
                return text
            if self.detail != "full":
                break  # tools-level blocks have nothing to degrade
        for start in range(1, len(blocks)):
            dropped = acting[:start]
            text = self._assemble(
                header, [self._collapsed(dropped)], blocks[start:]
            )
            if len(text) <= limit:
                return text
        return text[:limit]

    def summary(self, ok: bool) -> str:
        parts = [
            "✓" if ok else "✗ turn failed ·",
            _count(self.tool_calls, "tool call"),
            "·",
            _count(self.decisions, "decision"),
        ]
        if self.errors:
            parts += ["·", _count(self.errors, "error")]
        return " ".join(parts)

    def _tally(self) -> str:
        counts = Counter(
            c.name for d in self._decisions for c in d.calls
        )
        return ", ".join(
            name if n == 1 else f"{name} ×{n}" for name, n in counts.items()
        )

    def _block(self, decision: _Decision, detailed: bool) -> list[str]:
        lines: list[str] = []
        rich = self.detail == "full" and detailed
        if rich and decision.thinking:
            lines.append("thinking: " + _snip(decision.thinking, _THINKING_SNIP))
        if rich and decision.preamble:
            lines.append("said: " + _snip(decision.preamble, _THINKING_SNIP))
        for call in decision.calls:
            line = f"-> {call.name}({_args_summary(call.arguments)}) {_mark(call.ok)}"
            if rich and call.result and call.ok is not None:
                line += " — " + _snip(call.result, _RESULT_SNIP)
            lines.append(line)
        return lines

    def _collapsed(self, dropped: list[_Decision]) -> str:
        calls = sum(len(d.calls) for d in dropped)
        errors = sum(1 for d in dropped for c in d.calls if c.ok is False)
        line = (
            f"… {_count(len(dropped), 'earlier step')} "
            f"({_count(calls, 'tool call')}"
        )
        if errors:
            line += f", {_count(errors, 'error')}"
        return line + ")"

    def _assemble(
        self, header: str, extra: list[str], blocks: list[list[str]]
    ) -> str:
        lines = [header, *extra]
        for block in blocks:
            lines.extend(block)
        return "\n".join(lines)


@dataclass
class ChatActivity:
    """The sink: a ``TurnObserver`` bound to one chat's activity message.

    Inert until ``turn_started`` claims the placeholder (and stays inert
    at detail ``off``, or when the placeholder never posted -- then the
    turn behaves exactly as before WP19). ``close`` is called by the
    transport AFTER the reply is delivered (or by the composition root on
    the error paths), never by the pipeline: "collapse after the reply
    posts" is transport sequencing the pipeline cannot see.
    """

    reply: TurnReply
    edit: EditFn
    now: Callable[[], float] = time.monotonic  # injectable for tests
    min_interval: float = ACTIVITY_EDIT_SECONDS
    _log: ActivityLog | None = None
    _message_id: str | None = None
    _last_edit: float = float("-inf")

    async def turn_started(self, mode: str, detail: str) -> None:
        if detail == "off" or self._log is not None:
            return
        message_id = self.reply.claim_placeholder()
        if message_id is None:
            return  # open() failed; stay inert rather than posting anew
        self._message_id = message_id
        self._log = ActivityLog(detail=detail)
        # No first paint: "Processing…" reads fine until the first event.

    async def decision(self, turn: LLMTurn) -> None:
        if self._log is None:
            return
        self._log.note_decision(turn)
        await self._maybe_edit()

    async def tool_result(self, call: ToolCall, result: str, ok: bool) -> None:
        if self._log is None:
            return
        self._log.note_tool_result(call.name, result, ok)
        await self._maybe_edit()

    async def close(self, ok: bool) -> None:
        """Collapse to the done-summary; unconditional (no coalescing) --
        the final state must always land. A no-op when nothing streamed."""
        if self._log is None or self._message_id is None:
            return
        await self._push(self._log.summary(ok))

    async def _maybe_edit(self) -> None:
        if self._message_id is None or self._log is None:
            return
        if self.now() - self._last_edit < self.min_interval:
            return  # folded already; the next edit or close() flushes it
        await self._push(self._log.render())

    async def _push(self, text: str) -> None:
        self._last_edit = self.now()
        assert self._message_id is not None
        try:
            await self.edit(self._message_id, text, ())
        except Exception:  # noqa: BLE001 -- a diagnostic never fails a turn
            logger.warning(
                "activity edit failed (message %s); the turn continues",
                self._message_id, exc_info=True,
            )
