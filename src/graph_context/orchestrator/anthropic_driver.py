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
)
from graph_context.orchestrator.drivers import (
    DecideUsage,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 16000

_REFUSAL_NOTICE = (
    "(the model declined this request for safety reasons; rephrase or "
    "narrow the ask)"
)
_TRUNCATION_NOTE = "(reply truncated: the model hit its output-token limit)"


def _fenced_tool_result(tool_name: str, text: str) -> str:
    """The claude driver's fencing, reused as the orphan-result fallback."""
    return f'<tool_result tool="{tool_name}">\n{text}\n</tool_result>'


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
    """
    messages: list[dict[str, Any]] = []
    known_tool_use_ids: set[str] = set()

    def append_user_text(text: str) -> None:
        messages.append({"role": "user", "content": text})

    index = 0
    while index < len(events):
        event = events[index]
        if event.kind == "assistant":
            if event.tool_calls:
                content: list[dict[str, Any]] = []
                if event.text.strip():
                    content.append({"type": "text", "text": event.text})
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
                        _fenced_tool_result(tool_event.tool_name, tool_event.text)
                    )
                index += 1
            if results:
                messages.append({"role": "user", "content": results})
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
    the result events), ``thinking`` blocks are skipped (adaptive
    thinking streams them with empty text by default). ``stop_reason``
    outcomes the pipeline should see are folded into the reply text:
    a refusal yields a harness-visible notice instead of silence, and a
    ``max_tokens`` cut is annotated so truncation is not mistaken for a
    complete answer."""
    reply_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            reply_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(block.name, dict(block.input), id=block.id))
    reply = "\n\n".join(part for part in reply_parts if part.strip()).strip()
    if response.stop_reason == "refusal":
        return LLMTurn(reply=_REFUSAL_NOTICE)
    if response.stop_reason == "max_tokens":
        reply = f"{reply}\n\n{_TRUNCATION_NOTE}".strip()
    return LLMTurn(reply=reply, tool_calls=tuple(calls))


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
        # sends, serialized.
        return json.dumps(messages_from_transcript(transcript), indent=2)

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
    ) -> LLMTurn:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": assembled_system_prompt(goal),
            # Explicit even where it is the model default: omitting it
            # runs WITHOUT thinking on some current models.
            "thinking": {"type": "adaptive"},
            "tools": anthropic_tools(tools, self._schemas),
            "messages": messages_from_transcript(transcript),
            # No temperature/top_p/top_k: removed on current models (400).
            # Prompt caching deferred: once turn transcripts routinely
            # exceed the model's cacheable minimum, a top-level
            # cache_control={"type": "ephemeral"} is the one-kwarg upgrade.
        }
        if self._effort is not None:
            request["output_config"] = {"effort": self._effort}
        started = time.perf_counter()
        try:
            response = await self._client.messages.create(**request)
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
        duration_ms = int((time.perf_counter() - started) * 1000)
        if self._on_result is not None:
            self._on_result(usage_from_response(response, duration_ms))
        return turn_from_response(response)
