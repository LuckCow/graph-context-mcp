"""ClaudeAgentDriver: the real model behind the LLM seam (ADR 007, WP6).

Model access is the user's Claude SUBSCRIPTION: ``claude-agent-sdk`` runs
the Claude Code CLI, which authenticates with the persisted ``claude
login`` OAuth credential (or ``CLAUDE_CODE_OAUTH_TOKEN``) -- never an API
key, which would bill credits instead of the plan.

The SDK ships its own agentic loop; this adapter fits it behind
``LLMDriver.decide`` (one decision in, tool calls OR a reply out -- the
PIPELINE executes tools) instead of letting it run:

* The active mode's bound tools are registered as in-process MCP tools,
  so the model sees native tool calling. Only the binding's tools exist
  in the session -- the ADR 007 boundary again, enforced a second time at
  registration. ``session_options`` is the single place capability is
  configured: Claude Code's own built-ins (Bash, Read, Write, ...) are
  disabled outright (``tools=[]``) and no filesystem settings are loaded
  (``setting_sources=[]``), so host-machine settings cannot inject MCP
  servers, permissions, or hooks into the session.
* The permission callback (``can_use_tool``) DENIES every call with
  ``interrupt=True``. The SDK never executes a handler; the requested
  calls are harvested from the streamed assistant message and returned as
  the decision. (Confirmed live 2026-07-06: handler never runs, the
  ToolUseBlocks still stream, the session ends cleanly.)
* Tool input schemas are DERIVED from the tool wrappers' Python
  signatures (one source of truth, no maintained table). An open
  ``additionalProperties`` schema was tried first and failed live: the
  model echoed the schema's own keys back as arguments. Real properties
  plus ``additionalProperties: false`` make that unrepresentable; the
  docstrings remain the semantic contract (WP2), and validation errors
  still echo allowed values so the model self-corrects.

Each ``decide`` is a fresh, stateless CLI session over the rendered
turn-local transcript. Cross-turn conversation memory is deliberately
absent for now -- the SDK's session-resume machinery is the obvious lever
when dogfooding asks for it.
"""

from __future__ import annotations

import inspect
import logging
import types
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Union, get_args, get_origin, get_type_hints

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    EffortLevel,
    McpSdkServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    SdkMcpTool,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from graph_context.orchestrator.drivers import LLMTurn, ToolCall, TranscriptEvent

logger = logging.getLogger(__name__)

_SERVER_NAME = "gc"
_TOOL_PREFIX = f"mcp__{_SERVER_NAME}__"

_GUIDANCE = (
    "Call tools with a flat JSON object of their documented parameters. "
    "The harness executes every call; results arrive as <tool_result> "
    "blocks in the next message. Never repeat a call whose result is "
    "already in the transcript."
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
    earlier calls' output from user text.
    """
    parts: list[str] = []
    for event in events:
        if event.kind == "tool":
            parts.append(
                f'<tool_result tool="{event.tool_name}">\n{event.text}\n'
                "</tool_result>"
            )
        elif event.kind == "assistant":
            parts.append(f"<assistant_earlier>\n{event.text}\n</assistant_earlier>")
        else:
            parts.append(event.text)
    return "\n\n".join(parts)


def local_tool_name(sdk_name: str) -> str:
    """``mcp__gc__get_node`` -> ``get_node`` (the binding's name)."""
    return sdk_name.removeprefix(_TOOL_PREFIX)


async def _never_executed(_args: Any) -> dict[str, Any]:
    # can_use_tool denies before any handler runs; this exists because the
    # SDK requires one.
    return {"content": [{"type": "text", "text": "tool execution is harness-owned"}]}


def sdk_tools(
    tools: Mapping[str, str], schemas: Mapping[str, Mapping[str, Any]]
) -> list[SdkMcpTool[Any]]:
    """One MCP tool per bound tool: the doc is the contract, the derived
    schema is the shape. A name without a schema degrades to a bare
    object (the doc still documents the parameters)."""
    return [
        tool(name, doc, dict(schemas.get(name, {"type": "object"})))(_never_executed)
        for name, doc in sorted(tools.items())
    ]


def session_options(
    server: McpSdkServerConfig,
    goal: str,
    *,
    model: str | None,
    effort: EffortLevel | None,
    can_use_tool: CanUseTool,
    cli_path: str | None,
) -> ClaudeAgentOptions:
    """The session's capability boundary, in one place.

    The bound graph-context tools are the WHOLE surface (ADR 007):

    * ``tools=[]`` disables every Claude Code built-in (Read, Write, Bash,
      WebSearch, ...). The empty list is load-bearing -- ``None`` would
      mean "the CLI's full default toolset".
    * ``setting_sources=[]`` is SDK isolation mode: without it the CLI
      loads user/project/local settings from the host filesystem, which
      can inject extra MCP servers, permission grants, and hooks into
      the session.
    """
    return ClaudeAgentOptions(
        mcp_servers={_SERVER_NAME: server},
        tools=[],
        setting_sources=[],
        system_prompt=f"{goal}\n\n{_GUIDANCE}".strip(),
        model=model,
        effort=effort,
        can_use_tool=can_use_tool,
        cli_path=cli_path,
    )


class ClaudeAgentDriver:
    """LLMDriver over the Claude Code CLI (subscription-authenticated).

    ``model=None`` uses the CLI's default for the logged-in account;
    ``effort`` maps straight to the SDK knob.
    """

    def __init__(
        self,
        model: str | None = None,
        effort: EffortLevel | None = None,
        cli_path: str | None = None,
        schemas: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._cli_path = cli_path
        if schemas is None:
            # Derived once from the full tool surface; decide() registers
            # only the names the active mode's binding hands it.
            from graph_context.orchestrator import modes

            schemas = {
                name: derive_schema(fn) for name, fn in modes.full_surface().items()
            }
        self._schemas = schemas

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
    ) -> LLMTurn:
        server = create_sdk_mcp_server(
            name=_SERVER_NAME, version="1.0.0", tools=sdk_tools(tools, self._schemas)
        )

        async def capture_and_stop(
            name: str, _input: dict[str, Any], _context: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            logger.debug("driver captured tool request %s", name)
            return PermissionResultDeny(
                message="the harness executes tool calls", interrupt=True
            )

        options = session_options(
            server,
            goal,
            model=self._model,
            effort=self._effort,
            can_use_tool=capture_and_stop,
            cli_path=self._cli_path,
        )
        reply_parts: list[str] = []
        calls: list[ToolCall] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(render_transcript(transcript))
            async for message in client.receive_response():
                if not isinstance(message, AssistantMessage):
                    continue
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reply_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        calls.append(
                            ToolCall(local_tool_name(block.name), dict(block.input))
                        )
        if calls:
            # Any text alongside tool calls is preamble ("I'll look that
            # up"), not the reply; the decision is the calls.
            return LLMTurn(tool_calls=tuple(calls))
        reply = "\n\n".join(part for part in reply_parts if part.strip()).strip()
        return LLMTurn(reply=reply)
