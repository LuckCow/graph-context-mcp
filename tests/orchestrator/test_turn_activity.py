"""WP19 (ADR 029): the live-activity renderer and sink, no I/O anywhere.

``ActivityLog`` is a pure fold (events in, chat text out) driven with
handcrafted LLMTurns; ``ChatActivity`` runs against recorder fakes and an
injected clock, so coalescing and degrade behavior test deterministically.
"""

from __future__ import annotations

from graph_context.orchestrator.anytype_chat_transport import (
    SentMessages,
    TurnReply,
)
from graph_context.orchestrator.drivers import LLMTurn, ToolCall
from graph_context.orchestrator.turn_activity import ActivityLog, ChatActivity


def _decision(*calls: ToolCall, thinking: str = "", reply: str = "") -> LLMTurn:
    return LLMTurn(reply=reply, tool_calls=tuple(calls), thinking=thinking)


def _log_with_results(detail: str, *specs: tuple[str, bool]) -> ActivityLog:
    """One decision calling each named tool, results applied in order."""
    log = ActivityLog(detail=detail)
    log.note_decision(_decision(*(ToolCall(name, {"q": 1}) for name, _ in specs)))
    for name, ok in specs:
        log.note_tool_result(name, f"{name} ran", ok)
    return log


class TestActivityLogMinimal:
    def test_header_counts_decisions_and_tallies_tool_names(self) -> None:
        log = ActivityLog(detail="minimal")
        log.note_decision(_decision(
            ToolCall("explore", {"node": "Mira"}),
            ToolCall("explore", {"node": "Kel"}),
        ))
        log.note_decision(_decision(ToolCall("create_node", {"name": "Kel"})))
        assert log.render() == (
            "working… decision 2\ntools: explore ×2, create_node"
        )

    def test_a_callless_turn_renders_the_header_alone(self) -> None:
        log = ActivityLog(detail="minimal")
        log.note_decision(_decision(reply="just thinking aloud"))
        assert log.render() == "working… decision 1"


class TestActivityLogTools:
    def test_each_call_gets_a_line_with_arguments_and_its_mark(self) -> None:
        log = _log_with_results("tools", ("explore", True), ("update_node", False))
        lines = log.render().splitlines()
        assert lines[0] == "working… decision 1"
        assert lines[1] == "-> explore(q=1) ✓"
        assert lines[2] == "-> update_node(q=1) ✗"

    def test_a_dispatched_call_without_a_result_is_pending(self) -> None:
        log = ActivityLog(detail="tools")
        log.note_decision(_decision(ToolCall("explore", {"node": "Mira"})))
        assert log.render().splitlines()[1] == "-> explore(node='Mira') …"

    def test_long_argument_values_are_capped(self) -> None:
        log = ActivityLog(detail="tools")
        log.note_decision(_decision(ToolCall("capture", {"text": "x" * 500})))
        line = log.render().splitlines()[1]
        assert len(line) < 120
        assert line.endswith("…) …")

    def test_no_interim_text_or_excerpts_at_this_level(self) -> None:
        log = ActivityLog(detail="tools")
        log.note_decision(_decision(
            ToolCall("explore", {}), thinking="secret plan", reply="on it",
        ))
        log.note_tool_result("explore", "42 nodes found", True)
        text = log.render()
        assert "secret plan" not in text
        assert "on it" not in text
        assert "42 nodes" not in text


class TestServerToolCalls:
    """WP20 (ADR 030): provider-executed calls (web search) arrive
    already resolved -- rendered like any call, never pending, and they
    must not disturb the FIFO pairing of harness-executed results."""

    def test_a_server_call_renders_resolved_and_tallies(self) -> None:
        log = ActivityLog(detail="tools")
        log.note_decision(LLMTurn(
            server_tool_calls=(ToolCall("web_search", {"query": "mira"}),),
        ))
        assert log.render().splitlines()[1] == "-> web_search(query='mira') ✓"
        assert log.tool_calls == 1

    def test_pairing_of_harness_results_is_undisturbed(self) -> None:
        log = ActivityLog(detail="tools")
        log.note_decision(LLMTurn(
            tool_calls=(ToolCall("explore", {"q": 1}),),
            server_tool_calls=(ToolCall("web_search", {"query": "kel"}),),
        ))
        # The one harness result pairs with explore, not the search.
        log.note_tool_result("explore", "found", False)
        lines = log.render().splitlines()
        assert "-> web_search(query='kel') ✓" in lines
        assert "-> explore(q=1) ✗" in lines


class TestActivityLogFull:
    def test_thinking_interim_text_and_result_excerpts_render(self) -> None:
        log = ActivityLog(detail="full")
        log.note_decision(_decision(
            ToolCall("explore", {"node": "Mira"}),
            thinking="she would refuse the offer",
            reply="checking the graph first",
        ))
        log.note_tool_result("explore", "Mira: exiled siege engineer", True)
        text = log.render()
        assert "thinking: she would refuse the offer" in text
        assert "said: checking the graph first" in text
        assert "✓ — Mira: exiled siege engineer" in text

    def test_snippets_collapse_newlines_and_cap_length(self) -> None:
        log = ActivityLog(detail="full")
        log.note_decision(_decision(
            ToolCall("explore", {}), thinking="line one\nline two  " + "x" * 400,
        ))
        thinking_line = log.render().splitlines()[1]
        assert "\n" not in thinking_line
        assert "line one line two" in thinking_line
        assert len(thinking_line) < 220


class TestActivityLogTruncation:
    def _busy_log(self, decisions: int) -> ActivityLog:
        log = ActivityLog(detail="full")
        for i in range(decisions):
            log.note_decision(_decision(
                ToolCall(f"tool{i}", {"q": 1}), thinking="pondering " * 10,
            ))
            log.note_tool_result(f"tool{i}", "result " * 30, ok=i % 5 != 0)
        return log

    def test_excerpts_drop_from_all_but_the_newest_decision_first(self) -> None:
        log = self._busy_log(12)
        text = log.render(limit=800)
        assert len(text) <= 800
        # The newest decision keeps its excerpt; older ones degrade to
        # bare call lines before anything collapses wholesale.
        assert "tool11(q=1) ✓ — result" in text
        assert "tool5(q=1) ✗\n" in text or "tool5(q=1) ✓\n" in text

    def test_oldest_decisions_collapse_into_one_summary_line(self) -> None:
        log = self._busy_log(40)
        text = log.render(limit=500)
        assert len(text) <= 500
        assert "earlier steps (" in text  # the collapse line
        assert "tool39" in text           # the newest always survives
        assert "tool0(" not in text       # the oldest collapsed away

    def test_the_hard_floor_is_a_plain_truncation(self) -> None:
        log = self._busy_log(3)
        assert len(log.render(limit=60)) <= 60

    def test_minimal_never_exceeds_the_limit_either(self) -> None:
        log = ActivityLog(detail="minimal")
        log.note_decision(_decision(
            *(ToolCall(f"very_long_tool_name_{i}", {}) for i in range(120))
        ))
        assert len(log.render(limit=200)) <= 200


class TestDoneSummary:
    def test_counts_calls_and_decisions(self) -> None:
        log = _log_with_results("tools", ("explore", True), ("capture", True))
        log.note_decision(_decision(reply="done"))
        assert log.summary(ok=True) == "✓ 2 tool calls · 2 decisions"

    def test_singular_forms_read_naturally(self) -> None:
        log = _log_with_results("minimal", ("explore", True))
        assert log.summary(ok=True) == "✓ 1 tool call · 1 decision"

    def test_errors_are_appended_when_present(self) -> None:
        log = _log_with_results("tools", ("explore", True), ("capture", False))
        assert log.summary(ok=True).endswith("· 1 error")

    def test_a_failed_turn_says_so(self) -> None:
        log = _log_with_results("tools", ("explore", True))
        assert log.summary(ok=False).startswith("✗ turn failed ·")


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Recorder:
    def __init__(self, *, edit_raises: bool = False) -> None:
        self.messages: dict[str, str] = {}
        self.edits: list[tuple[str, str]] = []
        self._edit_raises = edit_raises

    async def send(self, text: str, attachments: tuple[str, ...] = ()) -> str:
        message_id = f"sent-{len(self.messages) + 1}"
        self.messages[message_id] = text
        return message_id

    async def edit(
        self, message_id: str, text: str, attachments: tuple[str, ...] = ()
    ) -> None:
        if self._edit_raises:
            raise RuntimeError("chat API down")
        self.messages[message_id] = text
        self.edits.append((message_id, text))


async def _opened(recorder: _Recorder) -> tuple[TurnReply, ChatActivity, _Clock]:
    reply = TurnReply(
        send=recorder.send, edit=recorder.edit, sent=SentMessages()
    )
    await reply.open()
    clock = _Clock()
    activity = ChatActivity(reply=reply, edit=recorder.edit, now=clock)
    return reply, activity, clock


EXPLORE = ToolCall("explore", {"node": "Mira"})


class TestChatActivitySink:
    async def test_detail_off_leaves_the_placeholder_lifecycle_alone(self) -> None:
        recorder = _Recorder()
        reply, activity, _ = await _opened(recorder)
        await activity.turn_started("world_modeling", "off")
        await activity.decision(_decision(EXPLORE))
        await activity.close(ok=True)
        assert recorder.edits == []  # never touched the message
        await reply.deliver("the reply")  # still edits the placeholder
        assert recorder.messages["sent-1"] == "the reply"

    async def test_streaming_claims_the_placeholder_so_the_reply_posts_fresh(
        self,
    ) -> None:
        recorder = _Recorder()
        reply, activity, _ = await _opened(recorder)
        await activity.turn_started("world_modeling", "tools")
        await activity.decision(_decision(EXPLORE))
        await reply.deliver("the reply")
        await activity.close(ok=True)
        assert recorder.messages["sent-2"] == "the reply"  # fresh message
        assert recorder.messages["sent-1"] == "✓ 1 tool call · 1 decision"

    async def test_without_a_placeholder_the_sink_stays_inert(self) -> None:
        recorder = _Recorder()
        reply = TurnReply(
            send=recorder.send, edit=recorder.edit, sent=SentMessages()
        )  # open() never ran (it failed at the composition root)
        activity = ChatActivity(reply=reply, edit=recorder.edit)
        await activity.turn_started("world_modeling", "full")
        await activity.decision(_decision(EXPLORE))
        await activity.close(ok=False)
        assert recorder.messages == {} and recorder.edits == []

    async def test_edits_within_the_interval_coalesce_without_loss(self) -> None:
        recorder = _Recorder()
        _, activity, clock = await _opened(recorder)
        await activity.turn_started("world_modeling", "minimal")
        await activity.decision(_decision(EXPLORE))          # t=0: edits
        clock.t = 1.0
        await activity.tool_result(EXPLORE, "found", True)   # folded, no edit
        await activity.decision(_decision(EXPLORE))          # folded, no edit
        assert len(recorder.edits) == 1
        clock.t = 3.0
        await activity.tool_result(EXPLORE, "found", True)   # flushes the lot
        assert len(recorder.edits) == 2
        assert "decision 2" in recorder.edits[-1][1]
        assert "explore ×2" in recorder.edits[-1][1]

    async def test_close_flushes_even_inside_the_interval(self) -> None:
        recorder = _Recorder()
        _, activity, clock = await _opened(recorder)
        await activity.turn_started("world_modeling", "minimal")
        await activity.decision(_decision(EXPLORE))
        clock.t = 0.5  # well inside the coalescing window
        await activity.close(ok=True)
        assert recorder.messages["sent-1"].startswith("✓")

    async def test_a_failing_edit_never_escapes(self) -> None:
        broken = _Recorder(edit_raises=True)
        reply = TurnReply(
            send=broken.send, edit=broken.edit, sent=SentMessages()
        )
        await reply.open()
        activity = ChatActivity(reply=reply, edit=broken.edit)
        await activity.turn_started("world_modeling", "tools")
        await activity.decision(_decision(EXPLORE))  # swallowed
        await activity.close(ok=True)                # swallowed
        assert broken.edits == []

    async def test_a_second_turn_started_does_not_reset_the_log(self) -> None:
        recorder = _Recorder()
        _, activity, _ = await _opened(recorder)
        await activity.turn_started("world_modeling", "minimal")
        await activity.decision(_decision(EXPLORE))
        await activity.turn_started("world_modeling", "full")  # ignored
        await activity.close(ok=True)
        assert recorder.messages["sent-1"] == "✓ 1 tool call · 1 decision"
