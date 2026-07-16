"""ClaudeAgentDriver: the real model behind the LLM seam (ADR 007, WP6).

Model access is the user's Claude SUBSCRIPTION: ``claude-agent-sdk`` runs
the Claude Code CLI, which authenticates with the persisted ``claude
login`` OAuth credential (or ``CLAUDE_CODE_OAUTH_TOKEN``) -- never an API
key, which would bill credits instead of the plan. (The API-key path
exists as ``anthropic_driver.py``, an explicit opt-in via
``GC_DRIVER=anthropic_api``.)

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

Web search (ADR 030): when a mode admits it, ``WebSearch`` -- and only
``WebSearch`` -- is re-admitted from the CLI's built-in toolset and
allowed through the permission callback. It executes on Anthropic's
servers within the session (the firewall never sees search traffic), the
model reads the results and continues INSIDE the same decide; its
ToolUseBlocks surface as ``server_tool_calls`` so the pipeline never
mistakes them for harness work. Raw result payloads are captured too
(WP22): a fresh CLI session takes text, so the next decide's rendered
transcript replays each search as a call + result-digest pair -- the
model keeps what the search returned across decisions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    EffortLevel,
    McpSdkServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from graph_context.orchestrator.driver_common import (
    assembled_system_prompt,
    derive_schema,
    render_transcript,
)
from graph_context.orchestrator.drivers import (
    DecideUsage,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)

__all__ = [
    "WEB_SEARCH_TOOL",
    "BlockHarvest",
    "ClaudeAgentDriver",
    "assembled_system_prompt",
    "derive_schema",
    "harvest_assistant_blocks",
    "harvest_result_blocks",
    "local_tool_name",
    "permission_gate",
    "query_payload",
    "render_transcript",
    "sdk_tools",
    "session_options",
    "usage_from_result",
]

logger = logging.getLogger(__name__)

_SERVER_NAME = "gc"
_TOOL_PREFIX = f"mcp__{_SERVER_NAME}__"

# The CLI built-in admitted when a mode enables web search (ADR 030).
# Executed by Anthropic server-side; never by the harness.
WEB_SEARCH_TOOL = "WebSearch"


def local_tool_name(sdk_name: str) -> str:
    """``mcp__gc__get_node`` -> ``get_node`` (the binding's name)."""
    return sdk_name.removeprefix(_TOOL_PREFIX)


async def _never_executed(_args: Any) -> dict[str, Any]:
    # can_use_tool denies before any handler runs; this exists because the
    # SDK requires one.
    return {"content": [{"type": "text", "text": "tool execution is harness-owned"}]}


@dataclass
class BlockHarvest:
    """One decide's streamed blocks, accumulated into a decision.

    Extracted from the receive loop so the translation is testable
    without a live CLI. Server-executed searches arrive two ways --
    native ``ServerToolUseBlock``/``ServerToolResultBlock`` pairs, or
    (live-verified) a ``ToolUseBlock`` named ``WebSearch`` whose result
    comes back as a ``ToolResultBlock`` in a following user message --
    both land in ``server_calls``/``server_results`` (WP22: raw result
    payloads, replayed as a digest on the next decide's transcript).
    """

    reply_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    server_calls: list[ToolCall] = field(default_factory=list)
    server_results: dict[str, str] = field(default_factory=dict)

    def turn(self) -> LLMTurn:
        # Text alongside tool calls is usually preamble ("I'll look that
        # up"), but it travels with the calls anyway: on a turn's FINAL
        # decision the pipeline treats it as the bundled reply (see
        # pipeline.LAST_TURN_WARNING). Which text counts is the
        # pipeline's rule, not this adapter's.
        reply = "\n\n".join(p for p in self.reply_parts if p.strip()).strip()
        thinking = "\n\n".join(
            p for p in self.thinking_parts if p.strip()
        ).strip()
        return LLMTurn(
            reply=reply,
            tool_calls=tuple(self.calls),
            thinking=thinking,
            server_tool_calls=tuple(self.server_calls),
            server_tool_results=tuple(
                self.server_results.get(call.id, "")
                for call in self.server_calls
            ),
        )


def _result_payload(tool_use_id: str, content: Any) -> str:
    """A server tool's raw result -> the opaque stored payload (WP22).

    The SDK hands content as a raw dict/list/str; ``search_digest``
    reads the ``content`` key back out when rendering the replay."""
    return json.dumps(
        {"tool_use_id": tool_use_id, "content": content}, default=str
    )


def harvest_assistant_blocks(
    blocks: Sequence[Any], harvest: BlockHarvest
) -> None:
    """One assistant message's content blocks into the harvest."""
    for block in blocks:
        if isinstance(block, TextBlock):
            harvest.reply_parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            harvest.thinking_parts.append(block.thinking)
        elif isinstance(block, ServerToolUseBlock):
            harvest.server_calls.append(
                ToolCall(str(block.name), dict(block.input), id=block.id)
            )
        elif isinstance(block, ServerToolResultBlock):
            harvest.server_results[block.tool_use_id] = _result_payload(
                block.tool_use_id, block.content
            )
        elif isinstance(block, ToolUseBlock):
            if block.name == WEB_SEARCH_TOOL:
                # Already executed provider-side; diary + replay
                # material, never pipeline work.
                harvest.server_calls.append(
                    ToolCall(block.name, dict(block.input), id=block.id)
                )
            else:
                harvest.calls.append(
                    ToolCall(
                        local_tool_name(block.name),
                        dict(block.input),
                        id=block.id,
                    )
                )


def harvest_result_blocks(
    blocks: Sequence[Any], harvest: BlockHarvest
) -> None:
    """Tool results from a USER message: only the ones answering a
    harvested server-side search (the ``ToolUseBlock``-shaped WebSearch
    path returns its result this way); everything else in user messages
    is the session's own plumbing and stays ignored."""
    ids = {call.id for call in harvest.server_calls}
    for block in blocks:
        if isinstance(block, ToolResultBlock) and block.tool_use_id in ids:
            harvest.server_results[block.tool_use_id] = _result_payload(
                block.tool_use_id, block.content
            )


def query_payload(
    transcript: Sequence[TranscriptEvent],
) -> str | AsyncIterator[dict[str, Any]]:
    """What ``client.query`` sends for this transcript.

    Text-only turns stay the plain rendered string. A turn carrying
    inbound images (WP23) takes the SDK's message-dict form instead --
    the one shape that can carry native image content blocks into the
    CLI -- with the images ahead of the rendered text (which already
    notes each image by name, so the text half stays self-describing).
    """
    rendered = render_transcript(transcript)
    images = [image for event in transcript for image in event.images]
    if not images:
        return rendered
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": image.data_base64,
            },
        }
        for image in images
    ]
    content.append({"type": "text", "text": rendered})

    async def _messages() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    return _messages()


def permission_gate(web_search: bool) -> CanUseTool:
    """The deny-all permission callback, with the one ADR 030 exception.

    Graph-tool calls are denied with ``interrupt=True`` -- the harness
    executes them (the calls are harvested from the streamed assistant
    message). ``WebSearch``, when a mode admits it, is ALLOWED: it
    executes on Anthropic's servers inside the session, so the search
    stays within one decide and nothing runs on the harness.
    """

    async def capture_and_stop(
        name: str, _input: dict[str, Any], _context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if web_search and name == WEB_SEARCH_TOOL:
            logger.debug("driver allowed server-side %s", name)
            return PermissionResultAllow()
        logger.debug("driver captured tool request %s", name)
        return PermissionResultDeny(
            message="the harness executes tool calls", interrupt=True
        )

    return capture_and_stop


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
    web_search: bool = False,
) -> ClaudeAgentOptions:
    """The session's capability boundary, in one place.

    The bound graph-context tools are the WHOLE surface (ADR 007):

    * ``tools=[]`` disables every Claude Code built-in (Read, Write, Bash,
      WebSearch, ...). The empty list is load-bearing -- ``None`` would
      mean "the CLI's full default toolset". The one mode-gated exception
      (ADR 030): ``web_search=True`` admits exactly ``WebSearch`` --
      server-side execution, so the boundary still excludes everything
      that touches the host.
    * ``setting_sources=[]`` is SDK isolation mode: without it the CLI
      loads user/project/local settings from the host filesystem, which
      can inject extra MCP servers, permission grants, and hooks into
      the session.
    """
    return ClaudeAgentOptions(
        mcp_servers={_SERVER_NAME: server},
        tools=[WEB_SEARCH_TOOL] if web_search else [],
        setting_sources=[],
        system_prompt=assembled_system_prompt(goal),
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
        on_result: Callable[[DecideUsage], None] | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._cli_path = cli_path
        # Cost/usage observer (the eval harness's metrics tap): called once
        # per decide with the session's translated ResultMessage. The
        # pipeline never sees usage -- it is diagnostics, not a decision.
        self._on_result = on_result
        if schemas is None:
            # Derived once from the full tool surface; decide() registers
            # only the names the active mode's binding hands it.
            from graph_context.orchestrator import modes

            schemas = {
                name: derive_schema(fn) for name, fn in modes.full_surface().items()
            }
        self._schemas = schemas

    def system_prompt(self, goal: str) -> str:
        return assembled_system_prompt(goal)

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        return render_transcript(transcript)  # what decide() queries with

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
        *,
        web_search: bool = False,
    ) -> LLMTurn:
        server = create_sdk_mcp_server(
            name=_SERVER_NAME, version="1.0.0", tools=sdk_tools(tools, self._schemas)
        )
        options = session_options(
            server,
            goal,
            model=self._model,
            effort=self._effort,
            can_use_tool=permission_gate(web_search),
            cli_path=self._cli_path,
            web_search=web_search,
        )
        harvest = BlockHarvest()
        last_result: ResultMessage | None = None
        async with ClaudeSDKClient(options=options) as client:
            await client.query(query_payload(transcript))
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    last_result = message
                elif isinstance(message, AssistantMessage):
                    harvest_assistant_blocks(message.content, harvest)
                elif isinstance(message, UserMessage) and isinstance(
                    message.content, list
                ):
                    # WebSearch results for ToolUseBlock-shaped calls come
                    # back as tool results in a user message (WP22).
                    harvest_result_blocks(message.content, harvest)
        if self._on_result is not None and last_result is not None:
            self._on_result(usage_from_result(last_result))
        return harvest.turn()


def usage_from_result(result: ResultMessage) -> DecideUsage:
    """SDK ResultMessage -> the pure DecideUsage value.

    The ``usage`` dict is the CLI's passthrough of the API usage block;
    absent keys read as zero rather than failing -- usage is diagnostics
    and must never take a decision down.
    """
    usage = result.usage or {}
    return DecideUsage(
        duration_ms=result.duration_ms,
        duration_api_ms=result.duration_api_ms,
        total_cost_usd=result.total_cost_usd,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        num_turns=result.num_turns,
    )
