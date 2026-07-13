"""Live AnthropicDriver E2E, gated behind ``GC_ANTHROPIC_E2E=1``.

Talks to the real model over the Messages API -- it SPENDS API CREDITS
(not subscription quota), so it never runs by default and additionally
requires the key to be set::

    GC_ANTHROPIC_E2E=1 ANTHROPIC_API_KEY=... \
        python -m pytest tests/e2e/test_live_anthropic_driver.py -q

Pins the two decide() shapes against the real API: a prompt that needs a
lookup comes back as a captured tool call (with a real ``toolu_`` id),
and a transcript already carrying the native tool_use/tool_result
round-trip comes back as a reply. This is also where the SimpleNamespace
response fixtures of the unit suite are validated against the real SDK
objects. Assertions stay loose on wording -- the model's prose is not a
contract.
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("GC_ANTHROPIC_E2E") != "1":
    pytest.skip(
        "set GC_ANTHROPIC_E2E=1 to run live anthropic-driver tests "
        "(spends API credits)",
        allow_module_level=True,
    )
if not (
    os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
):
    pytest.skip(
        "live anthropic-driver tests need ANTHROPIC_API_KEY (bills credits)",
        allow_module_level=True,
    )
pytest.importorskip("anthropic")

from graph_context.interface.profiles import get_profile  # noqa: E402
from graph_context.orchestrator.anthropic_driver import (  # noqa: E402
    AnthropicDriver,
)
from graph_context.orchestrator.drivers import (  # noqa: E402
    ToolCall,
    TranscriptEvent,
)

GOAL = "You assist with a story-world knowledge graph. Use tools to look things up."


@pytest.fixture(scope="module")
def tools() -> dict[str, str]:
    profile = get_profile("fiction")
    return {"find_node": profile.tool_docs["find_node"]}


async def test_a_lookup_prompt_becomes_a_captured_tool_call(tools):
    driver = AnthropicDriver()
    turn = await driver.decide(
        [TranscriptEvent("user", "Find the graph node for Mira. Use a tool.")],
        tools,
        goal=GOAL,
    )
    assert turn.tool_calls, f"expected a tool call, got reply: {turn.reply!r}"
    assert turn.tool_calls[0].name == "find_node"
    assert turn.tool_calls[0].id.startswith("toolu_")


async def test_a_native_round_trip_transcript_becomes_a_reply(tools):
    """The transcript shape the pipeline builds mid-turn: the assistant's
    tool-call decision (recorded with the API's own id) followed by the
    paired result -- sent back as real tool_use/tool_result blocks."""
    call = ToolCall("find_node", {"query": "Mira"}, id="toolu_01AAAAAAAAAAAAAAAAAAAAAA")
    driver = AnthropicDriver()
    turn = await driver.decide(
        [
            TranscriptEvent("user", "Who is Mira? Answer from the tool result."),
            TranscriptEvent("assistant", "", tool_calls=(call,)),
            TranscriptEvent(
                "tool",
                "Mira (Character, id=n1): Exiled siege engineer of Brakk.",
                tool_name="find_node",
                tool_use_id=call.id,
            ),
        ],
        tools,
        goal=GOAL,
    )
    assert turn.reply and not turn.tool_calls
    assert "mira" in turn.reply.lower()


async def test_usage_reports_tokens(tools):
    seen = []
    driver = AnthropicDriver(on_result=seen.append)
    await driver.decide(
        [TranscriptEvent("user", "Reply with the single word: ok")], {}, goal=GOAL
    )
    assert seen
    assert seen[0].input_tokens > 0
    assert seen[0].output_tokens > 0
    assert seen[0].total_cost_usd is None
