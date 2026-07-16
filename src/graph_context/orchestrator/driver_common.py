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
import json
import types
from collections.abc import Callable, Sequence
from typing import Any, Union, get_args, get_origin, get_type_hints

from graph_context.orchestrator.drivers import ToolCall, TranscriptEvent

# The "[from <name>]" tag is written by pipeline.sender_attributed; the
# description here and the format there must stay in lockstep.
_GUIDANCE = (
    "Call tools with a flat JSON object of their documented parameters. "
    "The harness executes every call; results arrive as <tool_result> "
    "blocks in the next message. Your own earlier decisions this turn "
    "are replayed as <assistant_earlier> blocks -- your reasoning plus "
    "the <tool_call> lines you already issued, each followed by its "
    "<tool_result>. Never repeat a call whose result is already in the "
    "transcript: same tool, same arguments means the answer is already "
    "there. User messages may open with a "
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


def fenced_tool_result(tool_name: str, text: str) -> str:
    """The one tool-result fence every driver renders: named so the model
    can tell its own earlier calls' output from user text."""
    return f'<tool_result tool="{tool_name}">\n{text}\n</tool_result>'


SEARCH_DIGEST_MAX_RESULTS = 8
_SEARCH_DIGEST_LINE_CHARS = 200


def search_digest(result_json: str) -> str:
    """One captured server-tool result payload -> a plain-text digest.

    WP22's text-transcript replay (and the turn diary): the payload is an
    OPAQUE provider-shaped raw block, so parsing is defensive -- a result
    list yields one ``- title (url)`` line per hit (a ``text`` item yields
    its snipped text), an error object names its code, and anything
    unrecognizable degrades to a placeholder rather than raising. Raw
    payloads (with their bulky ``encrypted_content``) never surface here.
    """
    try:
        payload = json.loads(result_json)
    except ValueError:
        return "(unreadable search result payload)"
    content = payload.get("content") if isinstance(payload, dict) else payload
    if isinstance(content, dict):
        code = content.get("error_code")
        return f"search failed: {code}" if code else "(no results)"
    if isinstance(content, str):  # SDK ToolResultBlock text-shaped result
        text = content.strip()
        if len(text) > _SEARCH_DIGEST_LINE_CHARS * SEARCH_DIGEST_MAX_RESULTS:
            text = text[: _SEARCH_DIGEST_LINE_CHARS * SEARCH_DIGEST_MAX_RESULTS - 1] + "…"
        return text or "(no results)"
    if not isinstance(content, list):
        return "(no results)"
    lines: list[str] = []
    for item in content[:SEARCH_DIGEST_MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        text = str(item.get("text") or "").strip()
        if title or url:
            line = " ".join(p for p in (title, f"({url})" if url else "") if p)
        elif text:
            line = text
        else:
            continue
        if len(line) > _SEARCH_DIGEST_LINE_CHARS:
            line = line[: _SEARCH_DIGEST_LINE_CHARS - 1] + "…"
        lines.append(f"- {line}")
    dropped = len(content) - SEARCH_DIGEST_MAX_RESULTS
    if dropped > 0:
        lines.append(f"- … {dropped} more result(s)")
    return "\n".join(lines) or "(no results)"


def fenced_tool_call(call: ToolCall) -> str:
    """A prior call rendered WITH its arguments: without them the model
    cannot tell which searches it already tried, so a fruitless call gets
    repeated verbatim until the tool budget runs out (turn 69f1a23b0d67).
    Arguments render as the element body -- JSON stays quoting-safe there,
    and the shape parallels ``<tool_result>``."""
    arguments = json.dumps(dict(call.arguments), default=str)
    return f'<tool_call tool="{call.name}">{arguments}</tool_call>'


def render_transcript(events: Sequence[TranscriptEvent]) -> str:
    """The turn-local transcript as one prompt (fresh session per decide).

    Tool results are fenced and named so the model can tell its own
    earlier calls' output from user text. A mid-turn assistant decision
    replays everything the (stateless) next session needs to continue its
    own train of thought: the reasoning that chose the calls, any bundled
    text, provider-executed searches as call + digest pairs (WP22 -- a
    text transcript cannot carry raw provider blocks, so the digest is
    what the search returned), and the calls themselves with their
    arguments -- each followed in order by its ``<tool_result>``. An
    assistant event with nothing to show (scripted decisions carry no
    text) is skipped rather than fenced empty.
    """
    parts: list[str] = []
    for event in events:
        if event.kind == "tool":
            parts.append(fenced_tool_result(event.tool_name, event.text))
        elif event.kind == "assistant":
            inner: list[str] = []
            if event.thinking.strip():
                inner.append(f"<thinking>\n{event.thinking}\n</thinking>")
            if event.text.strip():
                inner.append(event.text)
            for index, call in enumerate(event.server_tool_calls):
                inner.append(fenced_tool_call(call))
                raw = (
                    event.server_tool_results[index]
                    if index < len(event.server_tool_results) else ""
                )
                if raw:
                    inner.append(
                        fenced_tool_result(call.name, search_digest(raw))
                    )
            inner.extend(fenced_tool_call(call) for call in event.tool_calls)
            if inner:
                joined = "\n\n".join(inner)
                parts.append(
                    f"<assistant_earlier>\n{joined}\n</assistant_earlier>"
                )
        else:
            text = event.text
            if event.images:
                # WP23: the pixels travel as native blocks (each driver's
                # concern); the transcript notes their presence so replays
                # and the diary stay legible.
                notes = "\n".join(
                    f'[image attached: {image.name or "(unnamed)"} '
                    f"({image.media_type})]"
                    for image in event.images
                )
                text = f"{text}\n\n{notes}".strip()
            parts.append(text)
    return "\n\n".join(parts)


def assembled_system_prompt(goal: str) -> str:
    """Goal + the static tool-calling guidance: the ENTIRE system prompt.

    The one assembly point -- each driver sends it and reports it to the
    turn diary from this same function, so the logged prompt can never
    drift from the sent one.
    """
    return f"{goal}\n\n{_GUIDANCE}".strip()
