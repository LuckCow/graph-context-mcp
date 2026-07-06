"""Live ClaudeAgentDriver E2E, gated behind ``GC_CLAUDE_E2E=1``.

Talks to the real model on the logged-in Claude subscription (it spends
plan quota, so it never runs by default)::

    GC_CLAUDE_E2E=1 python -m pytest tests/e2e/test_live_claude_driver.py -q

Pins the two decide() shapes against a real session: a prompt that needs
a lookup comes back as a captured tool call (never executed by the SDK),
and a transcript already carrying the tool result comes back as a reply.
Assertions stay loose on wording -- the model's prose is not a contract.
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("GC_CLAUDE_E2E") != "1":
    pytest.skip(
        "set GC_CLAUDE_E2E=1 to run live Claude-driver tests",
        allow_module_level=True,
    )
pytest.importorskip("claude_agent_sdk")

from graph_context.interface.profiles import get_profile  # noqa: E402
from graph_context.orchestrator.claude_driver import ClaudeAgentDriver  # noqa: E402
from graph_context.orchestrator.drivers import TranscriptEvent  # noqa: E402

GOAL = "You assist with a story-world knowledge graph. Use tools to look things up."


@pytest.fixture(scope="module")
def tools() -> dict[str, str]:
    profile = get_profile("fiction")
    return {"find_node": profile.tool_docs["find_node"]}


async def test_a_lookup_prompt_becomes_a_captured_tool_call(tools):
    driver = ClaudeAgentDriver()
    turn = await driver.decide(
        [TranscriptEvent("user", "Find the graph node for Mira. Use a tool.")],
        tools,
        goal=GOAL,
    )
    assert turn.tool_calls, f"expected a tool call, got reply: {turn.reply!r}"
    assert turn.tool_calls[0].name == "find_node"


async def test_a_transcript_with_the_result_becomes_a_reply(tools):
    driver = ClaudeAgentDriver()
    turn = await driver.decide(
        [
            TranscriptEvent("user", "Who is Mira? Answer from the tool result."),
            TranscriptEvent(
                "tool",
                "Mira (Character, id=n1): Exiled siege engineer of Brakk.",
                tool_name="find_node",
            ),
        ],
        tools,
        goal=GOAL,
    )
    assert turn.reply and not turn.tool_calls
    assert "mira" in turn.reply.lower()
