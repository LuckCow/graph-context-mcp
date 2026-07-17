"""Size-capped JSONL record of everything that crosses the turn seam.

Every message the orchestrator handles is logged as it flows: the user's
input, the turn's assembled prompt exactly as the driver sends it, each
driver decision (tool calls or the final reply), every executed tool call
with its FULL rendered result, and the turn's final reply events. One
JSON object per line so the file tails and greps cleanly; every entry
carries an ISO-8601 UTC timestamp.

The file is bounded, not rotated: once an append pushes it past
``max_bytes``, the OLDEST entries are dropped until the newest fit in
half the budget (halving amortizes the rewrite instead of trimming on
every subsequent append). A diagnostic must never take a turn down with
it, so write failures degrade to a logged warning -- the reply still
reaches the user. The missing-directory case, by contrast, fails loudly
at construction time, i.e. at startup.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graph_context.orchestrator.driver_common import search_digest
from graph_context.orchestrator.drivers import DecideUsage, LLMTurn, ToolCall


def _server_result(turn: LLMTurn, position: int) -> str:
    """The raw payload paired with the position-th server call, if any."""
    if position < len(turn.server_tool_results):
        return turn.server_tool_results[position]
    return ""

if TYPE_CHECKING:
    from graph_context.orchestrator.pipeline import ReplyEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 10_000_000  # ~10 MB of JSONL before old entries drop
DEFAULT_TURN_LOG = "logs/turns.jsonl"  # relative to the process cwd
OFF_VALUES = frozenset({"", "0", "false", "no", "off"})  # the repo-wide "knob off" spellings


def turn_log_path() -> str | None:
    """``GC_TURN_LOG`` resolution: the JSONL path, or None (diary off).

    The single home of the off-values rule -- the writer (bootstrap) and
    the viewer (turn_log_server) both resolve the path through here.
    """
    raw = os.environ.get("GC_TURN_LOG", DEFAULT_TURN_LOG).strip()
    if raw.lower() in OFF_VALUES:
        return None
    return raw


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TurnLog:
    """Append-only turn diary with a byte budget (see module docstring)."""

    def __init__(
        self,
        path: str | Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._now = now
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # Every entry names the active mode so a single grepped line is
    # self-describing -- no walking back to the turn's opening entry.
    # ``turn`` is the shared id of the handle_message call these records
    # belong to: it ties one user query to its driver decisions, tool
    # calls, and final replies so a reader can group by request even when
    # sessions interleave in the process-global file.

    def user_message(
        self, turn_id: str, session_id: str, mode: str, user_id: str,
        text: str, sender: str = "",
    ) -> None:
        entry: dict[str, Any] = {
            "event": "user", "turn": turn_id, "session": session_id,
            "mode": mode, "user": user_id, "text": text,
        }
        if sender:  # the display name shown to the model, when known
            entry["sender"] = sender
        self._append(entry)

    def prompt(
        self, turn_id: str, session_id: str, mode: str, goal: str,
        system_prompt: str, tools: Mapping[str, str],
    ) -> None:
        """The model's standing inputs: mode goal, the exact assembled
        system prompt, and the bound tool surface (name -> LLM-facing doc).

        Logged by the pipeline only when the combination CHANGES for a
        session (first turn, after a /mode switch) -- the content is
        per-mode-stable, so per-decision logging would only burn the byte
        budget repeating itself.
        """
        self._append({
            "event": "prompt", "turn": turn_id, "session": session_id,
            "mode": mode, "goal": goal, "system_prompt": system_prompt,
            "tools": dict(tools),
        })

    def context(
        self, turn_id: str, session_id: str, mode: str, text: str
    ) -> None:
        """The turn's rendered context block (scratchpad, working set,
        recent trail) -- a model input the transcript would otherwise
        hide from reviewers."""
        self._append({
            "event": "context", "turn": turn_id, "session": session_id,
            "mode": mode, "text": text,
        })

    def llm_prompt(
        self, turn_id: str, session_id: str, mode: str, text: str
    ) -> None:
        """The turn's assembled prompt exactly as the driver sends it --
        replayed conversation memory, the context block, and the live
        message, in the driver's own rendering (fences and all).

        Logged once per turn, before the first decision: later decisions
        within the turn send this same prompt plus the tool results
        already logged in full, so re-logging the growing transcript per
        decision would only burn the byte budget repeating itself.
        """
        self._append({
            "event": "llm_prompt", "turn": turn_id, "session": session_id,
            "mode": mode, "text": text,
        })

    def llm_turn(
        self, turn_id: str, session_id: str, mode: str, turn: LLMTurn
    ) -> None:
        """One driver decision, rationale included: ``thinking`` (the
        model's reasoning stream) and ``reply`` (text bundled with tool
        calls -- preamble, or the final answer) log whenever present, so
        a reader can see WHY each call was made, not just that it was."""
        entry: dict[str, Any] = {
            "event": "llm_turn", "turn": turn_id, "session": session_id,
            "mode": mode,
        }
        if turn.thinking:
            entry["thinking"] = turn.thinking
        if turn.server_tool_calls:
            # Provider-executed (web search, ADR 030): they already ran
            # inside the provider -- no tool_result event will follow.
            # Results log as DIGESTS (WP22): the raw payloads carry bulky
            # encrypted_content the diary has no use for.
            entry["server_tool_calls"] = [
                {
                    "name": call.name,
                    "arguments": dict(call.arguments),
                    **(
                        {"result": search_digest(raw)}
                        if (raw := _server_result(turn, position))
                        else {}
                    ),
                }
                for position, call in enumerate(turn.server_tool_calls)
            ]
        if turn.tool_calls:
            entry["tool_calls"] = [
                {"name": call.name, "arguments": dict(call.arguments)}
                for call in turn.tool_calls
            ]
            if turn.reply:
                entry["reply"] = turn.reply
        else:
            entry["reply"] = turn.reply
        self._append(entry)

    def usage(self, usage: DecideUsage) -> None:
        """One decide's cost/usage (ADR 037): tokens, cache stats, and --
        on the subscription driver -- dollars, which production
        previously computed and DISCARDED (``on_result`` was only ever
        wired by the eval harness). No turn id: the driver's callback
        fires outside the pipeline's turn scope, so adjacency to the
        surrounding ``llm_turn`` lines is the correlation."""
        entry: dict[str, Any] = {
            "event": "usage",
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
            "duration_ms": usage.duration_ms,
        }
        if usage.total_cost_usd is not None:
            entry["total_cost_usd"] = usage.total_cost_usd
        self._append(entry)

    def tool_result(
        self, turn_id: str, session_id: str, mode: str, call: ToolCall,
        result: str,
    ) -> None:
        self._append({
            "event": "tool_result", "turn": turn_id, "session": session_id,
            "mode": mode, "tool": call.name, "arguments": dict(call.arguments),
            "result": result,
        })

    def turn_end(
        self, turn_id: str, session_id: str, mode: str,
        events: Iterable[ReplyEvent],
    ) -> None:
        self._append({
            "event": "turn_end", "turn": turn_id, "session": session_id,
            "mode": mode,
            "replies": [{"kind": e.kind, "text": e.text} for e in events],
        })

    def _append(self, entry: Mapping[str, Any]) -> None:
        # default=str: tool arguments come from the model and may hold
        # shapes json.dumps does not know; a lossy string beats a lost turn.
        line = json.dumps(
            {"ts": self._now(), **entry}, ensure_ascii=False, default=str
        )
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self._path.stat().st_size > self._max_bytes:
                self._trim()
        except OSError as err:
            logger.warning("turn log write to %s failed: %s", self._path, err)

    def _trim(self) -> None:
        """Keep the newest entries that fit in half the budget.

        The newest entry survives unconditionally -- a single oversized
        record must not empty the log.
        """
        budget = self._max_bytes // 2
        lines = self._path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = lines[-1:]
        total = sum(len(line.encode("utf-8")) for line in kept)
        for line in reversed(lines[:-1]):
            size = len(line.encode("utf-8"))
            if total + size > budget:
                break
            kept.append(line)
            total += size
        self._path.write_text("".join(reversed(kept)), encoding="utf-8")
