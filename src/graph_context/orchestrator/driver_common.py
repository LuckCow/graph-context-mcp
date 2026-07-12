"""SDK-free code shared by the real drivers (claude_driver, anthropic_driver).

Both drivers send the same system prompt, derive tool schemas the same way,
and fence transcripts with the same conventions; this module holds that
shared logic so it is importable without either model SDK installed
(``claude_driver`` imports claude-agent-sdk at module level, so it cannot
be the import source for the anthropic driver -- or for CI, which installs
neither).
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable, Sequence
from typing import Any, Union, get_args, get_origin, get_type_hints

from graph_context.orchestrator.drivers import TranscriptEvent

# The "[from <name>]" tag is written by pipeline.sender_attributed; the
# description here and the format there must stay in lockstep.
_GUIDANCE = (
    "Call tools with a flat JSON object of their documented parameters. "
    "The harness executes every call; results arrive as <tool_result> "
    "blocks in the next message. Never repeat a call whose result is "
    "already in the transcript. User messages may open with a "
    'harness-added "[from <name>]" tag: that is the sender\'s display '
    "name, already resolved and authoritative -- use it as-is when a "
    "task needs the requester's name. Space members also exist as "
    "'Space member' nodes: to LINK a node to the sender or another "
    "member (e.g. an assignee-style relation), find_node their name "
    "and pass the node id as the link target."
)


def _json_type(annotation: Any) -> dict[str, Any]:
    """One Python annotation -> a JSON-schema fragment (best effort;
    an unknown shape degrades to unconstrained, never to wrong)."""
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        items = _json_type(args[0]) if args else {}
        return {"type": "array", "items": items} if items else {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    if origin in (types.UnionType, Union):
        members = [a for a in get_args(annotation) if a is not type(None)]
        fragments = [_json_type(m) for m in members]
        if len(fragments) == 1:
            return fragments[0]
        if all(list(f) == ["type"] for f in fragments):
            return {"type": [f["type"] for f in fragments]}
        return {"anyOf": fragments}
    return {}


def derive_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """A tool wrapper's signature as a JSON schema.

    Everything after the ``services`` parameter is a model-facing
    argument; no default means required. ``additionalProperties: false``
    is load-bearing -- it is what stops the model inventing keys.
    """
    hints = get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in inspect.signature(fn).parameters.items():
        if name == "services":
            continue
        properties[name] = _json_type(hints.get(name, Any))
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def render_transcript(events: Sequence[TranscriptEvent]) -> str:
    """The turn-local transcript as one prompt (fresh session per decide).

    Tool results are fenced and named so the model can tell its own
    earlier calls' output from user text. Assistant events with no text
    are skipped: the pipeline records the mid-turn tool-call decision as
    a text-empty assistant event (for drivers that round-trip native
    tool_use blocks), and an empty ``<assistant_earlier>`` fence would
    only add noise here.
    """
    parts: list[str] = []
    for event in events:
        if event.kind == "tool":
            parts.append(
                f'<tool_result tool="{event.tool_name}">\n{event.text}\n'
                "</tool_result>"
            )
        elif event.kind == "assistant":
            if event.text.strip():
                parts.append(
                    f"<assistant_earlier>\n{event.text}\n</assistant_earlier>"
                )
        else:
            parts.append(event.text)
    return "\n\n".join(parts)


def assembled_system_prompt(goal: str) -> str:
    """Goal + the static tool-calling guidance: the ENTIRE system prompt.

    The one assembly point -- each driver sends it and reports it to the
    turn diary from this same function, so the logged prompt can never
    drift from the sent one.
    """
    return f"{goal}\n\n{_GUIDANCE}".strip()
