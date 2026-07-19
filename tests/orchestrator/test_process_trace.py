"""ADR 038: the archive-grade fold of a turn's background process.

The render becomes the intent node's ``gc:process`` section -- every
decision and result, per-item soft caps, no collapsing. ``worked`` is
the gate for minting an intent node on a non-mutating turn.
"""

from __future__ import annotations

import json

from graph_context.orchestrator.drivers import LLMTurn, ToolCall
from graph_context.orchestrator.process_trace import ProcessTrace


class TestWorked:
    def test_a_plain_reply_is_not_work(self) -> None:
        trace = ProcessTrace()
        trace.note_decision(LLMTurn(reply="Just an answer."))
        assert not trace.worked
        assert trace.render() == ""

    def test_thinking_counts_as_work(self) -> None:
        trace = ProcessTrace()
        trace.note_decision(LLMTurn(reply="ok", thinking="hmm"))
        assert trace.worked

    def test_tool_calls_and_searches_count_as_work(self) -> None:
        calls = ProcessTrace()
        calls.note_decision(LLMTurn(tool_calls=(ToolCall("get_node", {}),)))
        assert calls.worked
        searches = ProcessTrace()
        searches.note_decision(LLMTurn(
            server_tool_calls=(ToolCall("web_search", {"query": "q"}),),
        ))
        assert searches.worked


class TestRender:
    def test_decisions_and_results_read_in_order(self) -> None:
        trace = ProcessTrace()
        trace.note_decision(LLMTurn(
            reply="Checking the vault first.",
            thinking="Need the node before linking.",
            tool_calls=(ToolCall("get_node", {"node_id": "n1"}),),
        ))
        trace.note_result("get_node", "Mira: exiled engineer.", ok=True)
        trace.note_decision(LLMTurn(reply="Done.", thinking="All set."))
        rendered = trace.render()
        assert rendered.index("**Decision 1**") < rendered.index(
            "Need the node before linking."
        ) < rendered.index("said: Checking the vault first.") < rendered.index(
            '-> get_node({"node_id": "n1"})'
        ) < rendered.index("result get_node (ok):") < rendered.index(
            "**Decision 2**"
        ) < rendered.index("All set.")

    def test_failed_results_are_marked(self) -> None:
        trace = ProcessTrace()
        trace.note_result("explore", "no such node", ok=False)
        assert "result explore (error):" in trace.render()

    def test_searches_render_with_digested_results(self) -> None:
        raw = json.dumps({"content": [
            {"type": "web_search_result", "title": "Docs", "url": "https://d"},
        ]})
        trace = ProcessTrace()
        trace.note_decision(LLMTurn(
            server_tool_calls=(ToolCall("web_search", {"query": "anytype"}),),
            server_tool_results=(raw,),
        ))
        rendered = trace.render()
        assert '-> web_search({"query": "anytime"})' not in rendered  # sanity
        assert '-> web_search({"query": "anytype"}) [server-side]' in rendered
        assert "Docs" in rendered and "encrypted" not in rendered

    def test_items_are_capped_and_fences_cannot_break_out(self) -> None:
        trace = ProcessTrace()
        trace.note_result("query", "```\nx\n```" + "y" * 10_000, ok=True)
        rendered = trace.render()
        assert len(rendered) < 5_000  # per-item soft cap, not the raw size
        # A result containing a fence must not close the trace's fence.
        assert "```\n'''" in rendered

    def test_a_reply_only_decision_leaves_no_empty_header(self) -> None:
        trace = ProcessTrace()
        trace.note_decision(LLMTurn(
            tool_calls=(ToolCall("get_node", {}),), thinking="t",
        ))
        trace.note_decision(LLMTurn(reply="final answer"))
        assert "**Decision 2**" not in trace.render()
