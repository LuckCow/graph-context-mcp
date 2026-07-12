"""Transcript rendering for stateless drivers (driver_common).

SDK-free on purpose: ``render_transcript`` is what a fresh-session driver
(ClaudeAgentDriver) sends as its entire memory of the turn so far, so its
fidelity is pinned here where CI always runs it -- the SDK-specific seams
stay in ``test_claude_driver`` behind its importorskip.
"""

from __future__ import annotations

from graph_context.orchestrator.driver_common import (
    assembled_system_prompt,
    render_transcript,
)
from graph_context.orchestrator.drivers import ToolCall, TranscriptEvent


class TestGuidance:
    def test_the_guidance_names_the_fences_the_rendering_emits(self) -> None:
        """The system prompt's description of the transcript and the
        rendering below must stay in lockstep -- the fences it names are
        the ones render_transcript actually writes."""
        prompt = assembled_system_prompt("any goal")
        assert "<tool_result>" in prompt
        assert "<assistant_earlier>" in prompt
        assert "<tool_call>" in prompt


class TestTranscriptRendering:
    def test_the_user_message_renders_plain(self) -> None:
        prompt = render_transcript([TranscriptEvent("user", "Who is Mira?")])
        assert prompt == "Who is Mira?"

    def test_tool_results_are_fenced_and_named(self) -> None:
        prompt = render_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("tool", "Mira: exiled engineer.", tool_name="get_node"),
        ])
        assert prompt.startswith("Who is Mira?")
        assert '<tool_result tool="get_node">' in prompt
        assert "Mira: exiled engineer." in prompt

    def test_prior_assistant_text_is_marked_as_earlier(self) -> None:
        prompt = render_transcript([
            TranscriptEvent("assistant", "I looked her up already."),
        ])
        assert "<assistant_earlier>" in prompt

    def test_a_mid_turn_decision_replays_its_calls_with_arguments(self) -> None:
        """The anti-loop guarantee: the next (stateless) decision can see
        WHICH arguments were already tried, so 'never repeat a call' is
        followable -- name-only results left it guessing and it repeated
        the same fruitless search until the tool budget ran out."""
        prompt = render_transcript([
            TranscriptEvent("user", "Assign the task to Tati."),
            TranscriptEvent(
                "assistant", "",
                tool_calls=(ToolCall("find_node", {"name": "Tati"}),),
            ),
            TranscriptEvent(
                "tool", "find_node: 1 match(es).", tool_name="find_node"
            ),
        ])
        assert '<tool_call tool="find_node">{"name": "Tati"}</tool_call>' in prompt
        # The call precedes its result, so the pair reads in order.
        assert prompt.index("<tool_call") < prompt.index("<tool_result")

    def test_a_mid_turn_decision_replays_its_reasoning(self) -> None:
        """The model's own train of thought survives the fresh session:
        thinking and bundled preamble render inside the earlier-decision
        fence, ahead of the calls they produced."""
        prompt = render_transcript([
            TranscriptEvent(
                "assistant", "Checking Tati first.",
                tool_calls=(ToolCall("find_node", {"name": "Tati"}),),
                thinking="I need her node id to link the assignee edge.",
            ),
        ])
        fence = prompt[
            prompt.index("<assistant_earlier>"):
            prompt.index("</assistant_earlier>")
        ]
        assert (
            "<thinking>\nI need her node id to link the assignee edge."
            "\n</thinking>" in fence
        )
        assert "Checking Tati first." in fence
        assert fence.index("<thinking>") < fence.index("Checking Tati first.")
        assert fence.index("Checking Tati first.") < fence.index("<tool_call")

    def test_an_assistant_event_with_nothing_to_show_is_skipped(self) -> None:
        """Scripted decisions carry no text, thinking, or calls; an empty
        fence would only add noise."""
        prompt = render_transcript([
            TranscriptEvent("user", "hello"),
            TranscriptEvent("assistant", "   "),
        ])
        assert prompt == "hello"
