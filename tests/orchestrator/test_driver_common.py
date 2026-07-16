"""Transcript rendering for stateless drivers (driver_common).

SDK-free on purpose: ``render_transcript`` is what a fresh-session driver
(ClaudeAgentDriver) sends as its entire memory of the turn so far, so its
fidelity is pinned here where CI always runs it -- the SDK-specific seams
stay in ``test_claude_driver`` behind its importorskip.
"""

from __future__ import annotations

import json

from graph_context.orchestrator.driver_common import (
    SEARCH_DIGEST_MAX_RESULTS,
    assembled_system_prompt,
    render_transcript,
    search_digest,
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


class TestSearchDigest:
    """WP22: opaque raw search payloads -> the plain-text replay/diary
    form. Parsing is defensive -- provider shapes vary and must never
    raise."""

    def test_a_result_list_digests_to_title_url_lines(self) -> None:
        raw = json.dumps({"content": [
            {"type": "web_search_result", "title": "Anytype API",
             "url": "https://developers.anytype.io",
             "encrypted_content": "OPAQUE"},
            {"type": "web_search_result", "title": "Changelog",
             "url": "https://anytype.io/changelog"},
        ]})
        digest = search_digest(raw)
        assert "- Anytype API (https://developers.anytype.io)" in digest
        assert "- Changelog (https://anytype.io/changelog)" in digest
        assert "OPAQUE" not in digest  # encrypted payloads never surface

    def test_an_error_object_names_its_code(self) -> None:
        raw = json.dumps({"content": {"type": "web_search_tool_result_error",
                                      "error_code": "max_uses_exceeded"}})
        assert search_digest(raw) == "search failed: max_uses_exceeded"

    def test_text_shaped_content_is_kept_snipped(self) -> None:
        # The SDK's ToolResultBlock can carry a plain string result.
        raw = json.dumps({"content": "Web search results: x" + "x" * 5000})
        digest = search_digest(raw)
        assert digest.startswith("Web search results:")
        assert len(digest) < 2000

    def test_long_result_lists_are_capped_with_a_drop_note(self) -> None:
        raw = json.dumps({"content": [
            {"title": f"r{i}", "url": f"https://x/{i}"} for i in range(20)
        ]})
        digest = search_digest(raw)
        assert digest.count("\n") == SEARCH_DIGEST_MAX_RESULTS  # + drop note
        assert "12 more result(s)" in digest

    def test_garbage_degrades_never_raises(self) -> None:
        assert search_digest("not json") == "(unreadable search result payload)"
        assert search_digest(json.dumps({"content": None})) == "(no results)"
        assert search_digest(json.dumps({"content": []})) == "(no results)"


class TestServerActivityRendering:
    """WP22: a searching decision replays as call + digest pairs in the
    text transcript, so a fresh session keeps what the search returned."""

    def test_a_search_replays_as_call_and_digest(self) -> None:
        raw = json.dumps({"content": [
            {"title": "Anytype API", "url": "https://developers.anytype.io"},
        ]})
        rendered = render_transcript([
            TranscriptEvent("user", "What changed upstream?"),
            TranscriptEvent(
                "assistant", "Checking.",
                tool_calls=(ToolCall("find_node", {"name": "API"}, id="t1"),),
                server_tool_calls=(
                    ToolCall("web_search", {"query": "anytype api"}, id="s1"),
                ),
                server_tool_results=(raw,),
            ),
        ])
        assert '<tool_call tool="web_search">' in rendered
        assert '<tool_result tool="web_search">' in rendered
        assert "Anytype API (https://developers.anytype.io)" in rendered
        assert '<tool_call tool="find_node">' in rendered

    def test_a_search_without_a_captured_result_renders_the_call_alone(
        self,
    ) -> None:
        rendered = render_transcript([
            TranscriptEvent(
                "assistant", "",
                server_tool_calls=(
                    ToolCall("web_search", {"query": "q"}, id="s1"),
                ),
                server_tool_results=("",),
            ),
        ])
        assert '<tool_call tool="web_search">' in rendered
        assert '<tool_result tool="web_search">' not in rendered
