"""The turn's background process, rendered for the intent node (ADR 038).

``ActivityLog``'s archive-grade sibling: the same fold over the turn's
decisions and tool results, but for the DURABLE record. Where the chat
activity stream fights a 2000-char message budget and deletes itself
once the reply lands (WP19), this render happens once at turn end and
becomes the intent node's ``gc:process`` section -- the "collapsible
thought process" behind the reply's object card. Nothing collapses;
per-item soft caps keep a pathological result from eating the intent
body cap on its own.

Pure fold, no I/O; the pipeline feeds it beside the observer and the
turn log, then passes ``render()`` to the IntentRecorder as plain data
(application code never imports the orchestrator).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from graph_context.orchestrator.driver_common import search_digest
from graph_context.orchestrator.drivers import LLMTurn

_THINKING_CAP = 4000
_SAID_CAP = 2000
_ARGS_CAP = 1000
_RESULT_CAP = 4000
_FENCE = "```"


def _clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _fenced(text: str) -> str:
    # The content must not be able to close its own fence early.
    safe = text.replace(_FENCE, "'''")
    return f"{_FENCE}\n{safe}\n{_FENCE}"


def _args(arguments: object) -> str:
    return _clip(json.dumps(arguments, default=str, ensure_ascii=False), _ARGS_CAP)


@dataclass
class ProcessTrace:
    """Fold one turn's background activity into intent-body markdown."""

    _parts: list[str] = field(default_factory=list)
    _decisions: int = 0
    _worked: bool = False

    @property
    def worked(self) -> bool:
        """Whether background process happened this turn -- thinking was
        produced, tools ran, or the provider searched. The WP30 gate:
        only working turns mint a trace (and carry the reply card); a
        plain answer stays a plain answer."""
        return self._worked

    def note_decision(self, turn: LLMTurn) -> None:
        """One driver decision: its thinking, interim text, and calls.

        A decision with nothing background about it (a plain final reply,
        no thinking) leaves no entry -- the reply is already chat history,
        and an empty header would be noise."""
        self._decisions += 1
        lines = [f"**Decision {self._decisions}**"]
        if turn.thinking:
            self._worked = True
            lines.append(
                f"thinking:\n{_fenced(_clip(turn.thinking, _THINKING_CAP))}"
            )
        if turn.reply.strip() and turn.tool_calls:
            # Interim text bundled with calls -- preamble the chat never
            # shows (the final reply is delivered, not traced).
            lines.append(f"said: {_clip(turn.reply, _SAID_CAP)}")
        for position, call in enumerate(turn.server_tool_calls):
            self._worked = True
            entry = f"-> {call.name}({_args(dict(call.arguments))}) [server-side]"
            raw = (
                turn.server_tool_results[position]
                if position < len(turn.server_tool_results) else ""
            )
            if raw:
                entry += f"\n{_fenced(_clip(search_digest(raw), _RESULT_CAP))}"
            lines.append(entry)
        for call in turn.tool_calls:
            self._worked = True
            lines.append(f"-> {call.name}({_args(dict(call.arguments))})")
        if len(lines) == 1:
            self._decisions -= 1
            return
        self._parts.append("\n\n".join(lines))

    def note_result(self, name: str, result: str, ok: bool) -> None:
        self._worked = True
        mark = "ok" if ok else "error"
        self._parts.append(
            f"result {name} ({mark}):\n{_fenced(_clip(result, _RESULT_CAP))}"
        )

    def render(self) -> str:
        """The whole background process as markdown, oldest first."""
        return "\n\n".join(self._parts)
