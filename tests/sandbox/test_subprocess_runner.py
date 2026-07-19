"""Process discipline of the REAL sandbox subprocess (WP32, ADR 040).

Local and fast (no network): each test spawns the actual child under
``python -I -S``. The accident classes the sandbox must contain --
runaway loops, memory bombs, output floods, secrets in the env -- each
get a live demonstration here.
"""

import os

import pytest

from graph_context.errors import GraphContextError
from graph_context.infrastructure.sandbox.runner import SubprocessScriptRunner


def payload() -> dict:
    return {
        "now": "2026-07-19 12:00:00",
        "rule": {"id": "r1", "name": "probe"},
        "trigger": "t1",
        "before": "",
        "after": "true",
        "nodes": [
            {"id": "t1", "type": "Task", "name": "ship it",
             "summary": "s", "fields": {"Done": "true"}},
        ],
        "edges": [],
        "caps": {"max_sets": 20},
    }


@pytest.fixture
def runner() -> SubprocessScriptRunner:
    return SubprocessScriptRunner(timeout_seconds=10.0)


class TestHappyPath:
    async def test_a_script_round_trips_sets_and_logs(
        self, runner: SubprocessScriptRunner
    ) -> None:
        outcome = await runner.run(
            "set(trigger, 'Note', 'done at ' + now)\nlog('fired')",
            payload(),
        )
        assert [(e.node_id, e.property, e.value) for e in outcome.sets] == [
            ("t1", "Note", "done at 2026-07-19 12:00:00"),
        ]
        assert outcome.logs == ("fired",)

    async def test_print_output_never_corrupts_the_protocol(
        self, runner: SubprocessScriptRunner
    ) -> None:
        outcome = await runner.run(
            "print('this is not JSON')\nset('t1', 'Note', 'ok')",
            payload(),
        )
        assert outcome.sets[0].value == "ok"


class TestContainment:
    async def test_an_infinite_loop_is_killed_by_the_wall_clock(self) -> None:
        runner = SubprocessScriptRunner(timeout_seconds=0.5)
        with pytest.raises(GraphContextError, match="time limit"):
            await runner.run("while True: pass", payload())

    async def test_a_sleeping_script_is_killed_too(self) -> None:
        # Sleeping burns no CPU: this specifically proves the WALL kill.
        runner = SubprocessScriptRunner(timeout_seconds=0.5)
        with pytest.raises(GraphContextError, match="time limit"):
            await runner.run(
                "import time\ntime.sleep(30)", payload()
            )

    async def test_a_memory_bomb_dies_under_the_address_space_limit(
        self, runner: SubprocessScriptRunner
    ) -> None:
        with pytest.raises(GraphContextError, match="script failed"):
            await runner.run(
                "x = []\n"
                "while True:\n"
                "    x.append('m' * (16 * 1024 * 1024))",
                payload(),
            )

    async def test_a_raw_stdout_bomb_hits_the_parent_output_cap(self) -> None:
        runner = SubprocessScriptRunner(
            timeout_seconds=10.0, max_output_bytes=64 * 1024
        )
        with pytest.raises(GraphContextError, match="too much output"):
            await runner.run(
                # os.write(1, ...) bypasses the in-child stdout swap.
                "import os\n"
                "for _ in range(1000):\n"
                "    os.write(1, b'x' * 65536)",
                payload(),
            )

    async def test_the_child_sees_a_scrubbed_environment(
        self, runner: SubprocessScriptRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANYTYPE_API_KEY", "super-secret")
        outcome = await runner.run(
            "import os\n"
            "log('leaked' if os.environ.get('ANYTYPE_API_KEY') else 'clean')\n"
            "log(len([k for k in os.environ if k.startswith('ANTHROPIC')]))",
            payload(),
        )
        assert outcome.logs == ("clean", "0")
        assert os.environ["ANYTYPE_API_KEY"] == "super-secret"  # parent intact


class TestFailureSurface:
    async def test_a_traceback_names_the_authors_line(
        self, runner: SubprocessScriptRunner
    ) -> None:
        with pytest.raises(GraphContextError) as err:
            await runner.run("x = 1\ny = x / 0", payload())
        message = str(err.value)
        assert "script failed" in message
        assert "ZeroDivisionError" in message
        assert "<rule script>" in message and "line 2" in message

    async def test_a_syntax_error_surfaces_legibly(
        self, runner: SubprocessScriptRunner
    ) -> None:
        with pytest.raises(GraphContextError, match="SyntaxError"):
            await runner.run("def broken(:", payload())

    async def test_the_in_child_write_cap_surfaces(
        self, runner: SubprocessScriptRunner
    ) -> None:
        script = "\n".join(f"set('t1', 'p{i}', {i})" for i in range(21))
        with pytest.raises(GraphContextError, match="at most 20"):
            await runner.run(script, payload())
