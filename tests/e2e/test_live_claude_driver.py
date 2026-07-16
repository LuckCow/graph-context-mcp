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

from claude_agent_sdk import (  # noqa: E402
    ClaudeSDKClient,
    PermissionResultDeny,
    SystemMessage,
    create_sdk_mcp_server,
)

from graph_context.interface.profiles import get_profile  # noqa: E402
from graph_context.orchestrator.claude_driver import (  # noqa: E402
    ClaudeAgentDriver,
    sdk_tools,
    session_options,
)
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


async def test_web_search_answers_a_current_information_question(tools):
    """WP20 (ADR 030): the WebSearch built-in works headless on
    subscription auth -- the search runs server-side INSIDE the decide
    and surfaces as server_tool_calls, never as pipeline work."""
    driver = ClaudeAgentDriver()
    turn = await driver.decide(
        [TranscriptEvent(
            "user",
            "Use web search to find what today's date is according to any "
            "news site, then answer with what you found.",
        )],
        tools,
        goal=GOAL,
        web_search=True,
    )
    assert turn.reply, "expected a searched answer, got no reply"
    assert not turn.tool_calls, (
        f"WebSearch leaked into pipeline tool_calls: {turn.tool_calls!r}"
    )
    assert turn.server_tool_calls, "the model never searched"
    assert turn.server_tool_calls[0].name == "WebSearch"
    # WP22: the raw result payload rides along for next-decide replay.
    assert any(raw for raw in turn.server_tool_results), (
        "no search result payload was captured from the stream"
    )


async def test_the_live_session_exposes_only_the_bound_gc_tools(tools):
    """The capability boundary, checked against the CLI's own init report:
    no Read/Write/Bash/... -- the binding's mcp__gc__* tools are the whole
    surface."""

    async def deny(name, tool_input, context):
        return PermissionResultDeny(
            message="the harness executes tool calls", interrupt=True
        )

    server = create_sdk_mcp_server(
        name="gc", version="1.0.0", tools=sdk_tools(tools, {})
    )
    options = session_options(
        server, GOAL, model=None, effort=None, can_use_tool=deny, cli_path=None
    )
    exposed: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Reply with the single word: ok")
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                exposed = list(message.data.get("tools", []))
    assert exposed, "the CLI init message never arrived"
    offenders = [name for name in exposed if not name.startswith("mcp__gc__")]
    assert not offenders, f"non-gc tools exposed to the model: {offenders}"
