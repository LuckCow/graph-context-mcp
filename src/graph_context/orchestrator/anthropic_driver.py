"""AnthropicDriver: the LLM seam over the raw Anthropic Messages API.

WHY THIS EXISTS NEXT TO ``claude_driver``: the claude driver runs the
Claude Code CLI on the user's SUBSCRIPTION (``claude login`` OAuth) and
needs the CLI installed and logged in. This driver speaks the Messages
API directly with ``ANTHROPIC_API_KEY`` and therefore **bills API
credits, not the plan** -- it is an explicit opt-in
(``GC_DRIVER=anthropic_api``) for deployments where the CLI/subscription
path is unavailable: headless servers, CI, or per-request cost accounting.

It is also structurally simpler than the claude driver. Tool calls come
back as first-class ``tool_use`` content blocks, so none of the
in-process-MCP-server / deny-all-permission machinery is needed; and the
conversation goes up as a native ``messages=[...]`` list rather than one
flattened prompt string. The pipeline records each mid-turn tool-call
decision as an ``assistant`` TranscriptEvent (paired to results by
``tool_use_id``), which this driver round-trips as real ``tool_use`` /
``tool_result`` blocks -- the idiomatic Messages API shape.

Like every driver, ``decide`` is single-decision and stateless: the
pipeline owns the agentic loop (ADR 007), executes the returned calls,
and feeds results back on the next decide.

Web search (ADR 030): when a mode admits it, the request carries the
provider's SERVER-SIDE web search tool -- searches run on Anthropic's
infrastructure inside the same ``messages.create`` call and never
round-trip through the pipeline. A searching decision's raw result
blocks are captured as opaque payloads on the decision event (WP22),
so when the same decision ALSO called local tools, the transcript
rebuilt for the next decide replays the search verbatim
(``encrypted_content`` untouched) -- the model keeps what the search
returned, not just what it wrote about it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)

from graph_context.errors import GraphContextError
from graph_context.orchestrator.driver_common import (
    assembled_system_prompt,
    derive_schema,
    fenced_tool_result,
)
from graph_context.orchestrator.drivers import (
    DecideUsage,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 16000

# Server-side web search paused mid-turn (the provider's internal loop hit
# its iteration cap): resume by re-sending with the partial assistant
# content appended. Bounded -- a turn that keeps pausing is cut off.
_MAX_PAUSE_RESUMES = 5

# Models whose web search tool supports dynamic filtering (the _20260209
# variant); everything older takes the basic _20250305 tool. Prefix match:
# dated snapshots and future point releases of these lines stay covered.
_DYNAMIC_FILTER_MODELS = (
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable",
    "claude-mythos",
)


def web_search_tool(model: str) -> dict[str, Any]:
    """The server-side web search tool definition for ``model``."""
    kind = (
        "web_search_20260209"
        if model.startswith(_DYNAMIC_FILTER_MODELS)
        else "web_search_20250305"
    )
    return {"type": kind, "name": "web_search"}


def _as_data(value: Any) -> Any:
    """A response content block -> plain JSON-serializable data.

    Real SDK blocks are pydantic models (``model_dump``); test fixtures
    are namespaces; either way the captured payload must round-trip
    ``json.dumps``/``loads`` byte-faithfully (``encrypted_content`` is
    the part the API requires back verbatim)."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(value, Mapping):
        return {key: _as_data(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_as_data(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _as_data(item) for key, item in vars(value).items()}
    return value

_REFUSAL_NOTICE = (
    "(the model declined this request for safety reasons; rephrase or "
    "narrow the ask)"
)
_TRUNCATION_NOTE = "(reply truncated: the model hit its output-token limit)"


def messages_from_transcript(
    events: Sequence[TranscriptEvent],
) -> list[dict[str, Any]]:
    """TranscriptEvents -> a Messages-API ``messages`` list.

    * ``user`` -> a user turn of plain text.
    * ``assistant`` with ``tool_calls`` -> an assistant turn of
      [text?, tool_use...] blocks; without -> plain assistant text.
    * consecutive ``tool`` events -> ONE user turn holding all their
      ``tool_result`` blocks (parallel-tool-use convention: results
      return together in a single user message).

    Guards for shapes the API rejects or that lost their pairing:

    * ``messages[0]`` must be a user turn, but memory eviction is
      event-granular -- a replayed history can open with an orphaned
      assistant reply. A synthetic opener keeps the API happy.
    * A ``tool_result`` block is only valid against a ``tool_use`` block
      already in the list; a tool event whose ``tool_use_id`` matches
      nothing (or is empty) falls back to fenced text in a user turn.
    * A decision's provider-executed searches (WP22) replay as
      ``server_tool_use`` + raw result block PAIRS, each result verbatim
      from capture (``encrypted_content`` untouched -- the API's
      multi-turn requirement). An unpaired half is never sent: a call
      whose result was not captured is omitted whole (the pre-WP22
      behavior for that search), because a dangling block is a 400.
    """
    messages: list[dict[str, Any]] = []
    known_tool_use_ids: set[str] = set()

    def append_user_text(text: str) -> None:
        messages.append({"role": "user", "content": text})

    index = 0
    while index < len(events):
        event = events[index]
        if event.kind == "assistant":
            if event.tool_calls or event.server_tool_calls:
                content: list[dict[str, Any]] = []
                if event.text.strip():
                    content.append({"type": "text", "text": event.text})
                for position, call in enumerate(event.server_tool_calls):
                    raw = (
                        event.server_tool_results[position]
                        if position < len(event.server_tool_results) else ""
                    )
                    if not raw:
                        continue  # never replay an unpaired half
                    content.append(
                        {
                            "type": "server_tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": dict(call.arguments),
                        }
                    )
                    content.append(json.loads(raw))
                for call in event.tool_calls:
                    known_tool_use_ids.add(call.id)
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": dict(call.arguments),
                        }
                    )
                if not content:
                    # Every search was unpaired and the decision carried
                    # no text or local calls: nothing valid to replay.
                    index += 1
                    continue
                messages.append({"role": "assistant", "content": content})
            elif not messages:
                # Orphaned reply half at the top of replayed history.
                append_user_text("(conversation resumes mid-thread)")
                messages.append({"role": "assistant", "content": event.text})
            else:
                messages.append({"role": "assistant", "content": event.text})
            index += 1
            continue
        if event.kind == "tool":
            results: list[dict[str, Any]] = []
            while index < len(events) and events[index].kind == "tool":
                tool_event = events[index]
                if tool_event.tool_use_id in known_tool_use_ids:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_event.tool_use_id,
                            "content": tool_event.text,
                        }
                    )
                else:
                    if results:
                        messages.append({"role": "user", "content": results})
                        results = []
                    append_user_text(
                        fenced_tool_result(tool_event.tool_name, tool_event.text)
                    )
                index += 1
            if results:
                messages.append({"role": "user", "content": results})
            continue
        if event.images:
            # WP23: inbound images ride the user turn as native blocks,
            # ahead of the text (the API's recommended order).
            blocks: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.data_base64,
                    },
                }
                for image in event.images
            ]
            blocks.append({"type": "text", "text": event.text})
            messages.append({"role": "user", "content": blocks})
            index += 1
            continue
        append_user_text(event.text)
        index += 1
    return messages


def anthropic_tools(
    tools: Mapping[str, str], schemas: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One native tool definition per bound tool, sorted by name
    (deterministic order keeps requests cache-friendly).

    A derived schema carries ``additionalProperties: false`` +
    ``required``, so it qualifies for ``strict`` validation. A name
    without a schema degrades to a bare object -- WITHOUT ``strict``,
    which the API rejects on schemas lacking those keys."""
    definitions: list[dict[str, Any]] = []
    for name, doc in sorted(tools.items()):
        schema = schemas.get(name)
        definition: dict[str, Any] = {
            "name": name,
            "description": doc,
            "input_schema": dict(schema) if schema else {"type": "object"},
        }
        if schema:
            definition["strict"] = True
        definitions.append(definition)
    return definitions


def turn_from_response(response: Any) -> LLMTurn:
    """A Messages-API response -> the driver's decision.

    Text blocks join into the reply, ``tool_use`` blocks become
    ToolCalls (real API ids preserved -- the pipeline echoes them back on
    the result events), ``thinking`` blocks land in ``LLMTurn.thinking``
    when non-empty (adaptive thinking streams them with empty text by
    default) -- diagnostics for the turn diary, never reply text.
    ``server_tool_use`` blocks (web search, ADR 030) already ran on the
    provider's side: they surface as ``server_tool_calls`` for the turn
    diary and activity stream, never as pipeline work. Their result
    companions (``web_search_tool_result`` -- error-object results
    included) are captured as OPAQUE raw payloads in
    ``server_tool_results``, position-paired by ``tool_use_id`` (WP22:
    the next decide replays them so the model keeps what the search
    returned; ``""`` marks a result that never arrived).
    ``stop_reason`` outcomes the pipeline should see are folded into the
    reply text: a refusal yields a harness-visible notice instead of
    silence, and a ``max_tokens`` cut is annotated so truncation is not
    mistaken for a complete answer."""
    reply_parts: list[str] = []
    thinking_parts: list[str] = []
    calls: list[ToolCall] = []
    server_calls: list[ToolCall] = []
    results_by_id: dict[str, str] = {}
    for block in response.content:
        if block.type == "text":
            reply_parts.append(block.text)
        elif block.type == "thinking":
            thinking_parts.append(block.thinking)
        elif block.type == "tool_use":
            calls.append(ToolCall(block.name, dict(block.input), id=block.id))
        elif block.type == "server_tool_use":
            server_calls.append(
                ToolCall(block.name, dict(block.input), id=block.id)
            )
        elif block.type.endswith("_tool_result"):
            # A server tool's result: keep the WHOLE raw block (the API
            # wants encrypted_content back verbatim on replay).
            results_by_id[str(getattr(block, "tool_use_id", ""))] = json.dumps(
                _as_data(block), default=str
            )
    reply = "\n\n".join(part for part in reply_parts if part.strip()).strip()
    thinking = "\n\n".join(part for part in thinking_parts if part.strip()).strip()
    if response.stop_reason == "refusal":
        return LLMTurn(reply=_REFUSAL_NOTICE)
    if response.stop_reason == "max_tokens":
        reply = f"{reply}\n\n{_TRUNCATION_NOTE}".strip()
    return LLMTurn(
        reply=reply,
        tool_calls=tuple(calls),
        thinking=thinking,
        server_tool_calls=tuple(server_calls),
        server_tool_results=tuple(
            results_by_id.get(call.id, "") for call in server_calls
        ),
    )


def usage_from_response(response: Any, duration_ms: int) -> DecideUsage:
    """API usage block -> the pure DecideUsage value.

    The API reports tokens, never dollars, so ``total_cost_usd`` stays
    ``None`` (eval reports show token totals; cost reads 0.0). Absent
    cache fields read as zero -- usage is diagnostics and must never take
    a decision down."""
    usage = response.usage
    return DecideUsage(
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        total_cost_usd=None,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        num_turns=1,
    )


def usage_from_responses(responses: Sequence[Any], duration_ms: int) -> DecideUsage:
    """Summed usage across one decide's requests (pause_turn resumes
    included) -- the metrics tap fires ONCE per decide, so eval reports
    keep counting decides, not wire round trips."""
    parts = [usage_from_response(response, duration_ms) for response in responses]
    return DecideUsage(
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        total_cost_usd=None,
        input_tokens=sum(p.input_tokens for p in parts),
        output_tokens=sum(p.output_tokens for p in parts),
        cache_read_tokens=sum(p.cache_read_tokens for p in parts),
        cache_creation_tokens=sum(p.cache_creation_tokens for p in parts),
        num_turns=1,
    )


def merged_turn(turns: Sequence[LLMTurn]) -> LLMTurn:
    """One logical decision from a pause_turn chain of responses.

    The provider pauses MID-decision (its server-tool loop hit an
    iteration cap), so each resumed response holds only continuation
    content -- reply text, thinking, and (server) tool calls concatenate
    in order to reconstruct the whole decision."""
    if len(turns) == 1:
        return turns[0]
    return LLMTurn(
        reply="\n\n".join(t.reply for t in turns if t.reply).strip(),
        tool_calls=tuple(c for t in turns for c in t.tool_calls),
        thinking="\n\n".join(t.thinking for t in turns if t.thinking).strip(),
        server_tool_calls=tuple(
            c for t in turns for c in t.server_tool_calls
        ),
        server_tool_results=tuple(
            r for t in turns for r in t.server_tool_results
        ),
    )


class AnthropicDriver:
    """LLMDriver over the Messages API (API-key authenticated, bills
    credits).

    The zero-arg ``AsyncAnthropic()`` resolves ``ANTHROPIC_API_KEY`` /
    ``ANTHROPIC_AUTH_TOKEN`` from the environment; ``build_driver``
    refuses to construct this driver unless one is set, so the billing
    switch is always a conscious choice. ``client`` is injectable for
    tests."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        schemas: Mapping[str, Mapping[str, Any]] | None = None,
        on_result: Callable[[DecideUsage], None] | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        # Cost/usage observer (the eval harness's metrics tap): called once
        # per decide. The pipeline never sees usage -- it is diagnostics,
        # not a decision.
        self._on_result = on_result
        if schemas is None:
            # Derived once from the full tool surface; decide() sends only
            # the names the active mode's binding hands it.
            from graph_context.orchestrator import modes

            schemas = {
                name: derive_schema(fn) for name, fn in modes.full_surface().items()
            }
        self._schemas = schemas
        self._client = client or AsyncAnthropic()

    def system_prompt(self, goal: str) -> str:
        return assembled_system_prompt(goal)

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        # The diary shows the true wire shape: the same mapping decide()
        # sends, serialized -- except image data (WP23), which is redacted
        # to a size note; megabytes of base64 are noise the diary's budget
        # would immediately evict everything else for.
        messages = messages_from_transcript(transcript)
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                source = block.get("source") if isinstance(block, dict) else None
                if isinstance(source, dict) and "data" in source:
                    source = dict(source)
                    source["data"] = f"<{len(source['data'])} base64 chars>"
                    block["source"] = source
        return json.dumps(messages, indent=2)

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
        *,
        web_search: bool = False,
        model: str = "",
    ) -> LLMTurn:
        # ADR 033: the active mode's pinned model wins over the
        # constructor default for this decision.
        effective_model = model or self._model
        tool_defs = anthropic_tools(tools, self._schemas)
        if web_search:
            # Deterministic tail position (after the sorted graph tools)
            # keeps requests cache-friendly across decides.
            tool_defs.append(web_search_tool(effective_model))
        messages = messages_from_transcript(transcript)
        request: dict[str, Any] = {
            "model": effective_model,
            "max_tokens": self._max_tokens,
            "system": assembled_system_prompt(goal),
            # Explicit even where it is the model default: omitting it
            # runs WITHOUT thinking on some current models.
            "thinking": {"type": "adaptive"},
            "tools": tool_defs,
            "messages": messages,
            # No temperature/top_p/top_k: removed on current models (400).
            # Prompt caching deferred: once turn transcripts routinely
            # exceed the model's cacheable minimum, a top-level
            # cache_control={"type": "ephemeral"} is the one-kwarg upgrade.
        }
        if self._effort is not None:
            request["output_config"] = {"effort": self._effort}
        started = time.perf_counter()
        responses: list[Any] = []
        for _ in range(_MAX_PAUSE_RESUMES + 1):
            responses.append(await self._create(request))
            if responses[-1].stop_reason != "pause_turn":
                break
            # A server-tool turn paused mid-decision: re-send with the
            # partial assistant content appended; the server resumes where
            # it left off (no synthetic user nudge).
            messages = [
                *messages,
                {"role": "assistant", "content": responses[-1].content},
            ]
            request["messages"] = messages
        duration_ms = int((time.perf_counter() - started) * 1000)
        if self._on_result is not None:
            self._on_result(usage_from_responses(responses, duration_ms))
        if responses[-1].stop_reason == "refusal":
            # A mid-chain partial before a refusal is discarded, not
            # presented as an answer.
            return turn_from_response(responses[-1])
        return merged_turn([turn_from_response(r) for r in responses])

    async def _create(self, request: dict[str, Any]) -> Any:
        try:
            return await self._client.messages.create(**request)
        except RateLimitError as err:
            raise GraphContextError(
                "anthropic API rate limit exhausted (after SDK retries); "
                "wait and retry"
            ) from err
        except APIStatusError as err:
            raise GraphContextError(
                f"anthropic API error {err.status_code}: {err.message}"
            ) from err
        except APIConnectionError as err:
            raise GraphContextError(
                "could not reach api.anthropic.com (network/egress?)"
            ) from err
