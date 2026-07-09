"""The turn diary: full-fidelity logging with a byte budget.

File tests pin the TurnLog contract directly (JSONL shape, oldest-first
trimming, write failures degrade). Pipeline tests drive a scripted turn
through the orchestrator and assert the diary tells the WHOLE story --
input, every driver decision, every tool call with its complete output,
and the final replies.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import get_profile
from graph_context.interface.tools import Services, build_services
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver, ToolCall
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator
from graph_context.orchestrator.turn_log import TurnLog

FICTION = get_profile("fiction")


def _entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestTurnLogFile:
    def test_entries_are_timestamped_jsonl_naming_the_mode(self, tmp_path) -> None:
        path = tmp_path / "logs" / "turns.jsonl"  # parent is created
        log = TurnLog(path, now=lambda: "T0")
        log.user_message("t0", "s1", "world_modeling", "u1", "Add Mira.")
        log.llm_turn("t0", "s1", "world_modeling", LLMTurn(reply="done"))
        first, second = _entries(path)
        assert first == {
            "ts": "T0", "event": "user", "turn": "t0", "session": "s1",
            "mode": "world_modeling", "user": "u1", "text": "Add Mira.",
        }
        assert second == {
            "ts": "T0", "event": "llm_turn", "turn": "t0", "session": "s1",
            "mode": "world_modeling", "reply": "done",
        }

    def test_tool_calls_and_results_are_logged_in_full(self, tmp_path) -> None:
        path = tmp_path / "turns.jsonl"
        log = TurnLog(path, now=lambda: "T0")
        call = ToolCall("create_node", {"type": "Character", "name": "Mira"})
        log.llm_turn("t0", "s1", "world_modeling", LLMTurn(tool_calls=(call,)))
        log.tool_result("t0", "s1", "world_modeling", call, "created char-1 'Mira'")
        decision, result = _entries(path)
        assert decision["tool_calls"] == [
            {"name": "create_node",
             "arguments": {"type": "Character", "name": "Mira"}},
        ]
        assert result["tool"] == "create_node"
        assert result["arguments"] == {"type": "Character", "name": "Mira"}
        assert result["result"] == "created char-1 'Mira'"
        assert decision["turn"] == result["turn"] == "t0"  # one request

    def test_oldest_entries_drop_once_the_budget_is_exceeded(
        self, tmp_path
    ) -> None:
        path = tmp_path / "turns.jsonl"
        log = TurnLog(path, max_bytes=600, now=lambda: "T0")
        for index in range(20):
            log.user_message("t0", "s1", "m", "u1", f"message number {index:02d}")
        kept = _entries(path)
        assert kept  # never trimmed to nothing
        assert path.stat().st_size <= 600
        assert kept[-1]["text"] == "message number 19"  # newest survives
        assert all(e["text"] != "message number 00" for e in kept)  # oldest gone

    def test_an_oversized_newest_entry_survives_a_trim(self, tmp_path) -> None:
        path = tmp_path / "turns.jsonl"
        log = TurnLog(path, max_bytes=200, now=lambda: "T0")
        log.user_message("t0", "s1", "m", "u1", "small")
        log.user_message("t1", "s1", "m", "u1", "x" * 500)
        (only,) = _entries(path)
        assert only["text"] == "x" * 500

    def test_a_write_failure_degrades_to_a_warning(self, tmp_path, caplog) -> None:
        path = tmp_path / "turns.jsonl"
        path.mkdir()  # opening a directory for append raises OSError
        log = TurnLog(path, now=lambda: "T0")
        with caplog.at_level(logging.WARNING):
            log.user_message("t0", "s1", "m", "u1", "hello")  # must not raise
        assert any("turn log write" in r.message for r in caplog.records)


class TestBuildTurnLog:
    def test_default_is_on_at_the_default_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GC_TURN_LOG", raising=False)
        log = bootstrap.build_turn_log()
        assert isinstance(log, TurnLog)
        assert (tmp_path / "logs").is_dir()

    def test_off_values_disable_it(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_TURN_LOG", "0")
        assert bootstrap.build_turn_log() is None

    def test_a_bad_max_bytes_fails_loudly(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GC_TURN_LOG", str(tmp_path / "t.jsonl"))
        monkeypatch.setenv("GC_TURN_LOG_MAX_BYTES", "lots")
        with pytest.raises(GraphContextError, match="GC_TURN_LOG_MAX_BYTES"):
            bootstrap.build_turn_log()
        monkeypatch.setenv("GC_TURN_LOG_MAX_BYTES", "-1")
        with pytest.raises(GraphContextError, match="positive"):
            bootstrap.build_turn_log()


@pytest.fixture
def services() -> Services:
    return build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
    )


def _orchestrator(
    services: Services, turns: list[LLMTurn], log: TurnLog
) -> Orchestrator:
    return Orchestrator(
        services=services, driver=ScriptedDriver(turns), profile=FICTION,
        registry=load_registry(FICTION), turn_log=log,
    )


CREATE_MIRA = ToolCall("create_node", {
    "type": "Character", "name": "Mira", "summary": "Exiled siege engineer.",
})


class TestPipelineTurnLogging:
    async def test_a_turn_logs_its_whole_story(
        self, services: Services, tmp_path
    ) -> None:
        path = tmp_path / "turns.jsonl"
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
        ], TurnLog(path, now=lambda: "T0"))
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        entries = _entries(path)
        assert [e["event"] for e in entries] == [
            "user", "llm_turn", "tool_result", "llm_turn", "turn_end",
        ]
        assert all(e["mode"] == "world_modeling" for e in entries)
        assert entries[0]["text"] == "Add Mira."
        assert entries[1]["tool_calls"][0]["name"] == "create_node"
        assert "Mira" in entries[2]["result"]  # the tool's full output
        assert entries[3]["reply"] == "Mira now exists."
        assert entries[4]["replies"] == [
            {"kind": "reply", "text": "Mira now exists."},
        ]
        # Every record of one handle_message call shares one turn id, so a
        # reader can group the whole story by request.
        turn_ids = {e["turn"] for e in entries}
        assert len(turn_ids) == 1 and turn_ids.pop()  # one, non-empty

    async def test_mode_commands_are_logged_too(
        self, services: Services, tmp_path
    ) -> None:
        path = tmp_path / "turns.jsonl"
        orchestrator = _orchestrator(services, [], TurnLog(path, now=lambda: "T0"))
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        user, end = _entries(path)
        assert user["event"] == "user" and user["text"] == "/mode authoring"
        assert user["mode"] == "world_modeling"  # the mode the command found
        assert end["event"] == "turn_end"
        assert end["mode"] == "authoring"  # the mode the session is in now
        assert "authoring" in end["replies"][0]["text"]

    async def test_a_rejected_tool_call_is_logged_with_the_notice(
        self, services: Services, tmp_path
    ) -> None:
        """The binding boundary's runtime face lands in the diary as the
        tool result the driver actually saw."""
        path = tmp_path / "turns.jsonl"
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="I tried."),
        ], TurnLog(path, now=lambda: "T0"))
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        entries = _entries(path)
        rejected = [e for e in entries if e["event"] == "tool_result"]
        assert len(rejected) == 1
        assert rejected[0]["mode"] == "authoring"
        assert "not available in authoring mode" in rejected[0]["result"]
        # The two messages are two turns with two distinct ids.
        assert len({e["turn"] for e in entries}) == 2

    async def test_no_turn_log_means_no_file_and_no_crash(
        self, services: Services, tmp_path
    ) -> None:
        orchestrator = Orchestrator(
            services=services, driver=ScriptedDriver([LLMTurn(reply="ok")]),
            profile=FICTION, registry=load_registry(FICTION),
        )
        events = await orchestrator.handle_message("s1", "u1", "hello")
        assert events[-1].kind == "reply"
        assert list(tmp_path.iterdir()) == []
