"""The inspection server: tailing logic, HTTP routes, and env resolution.

``_read_new`` is the tail's whole brain (offsets, partial lines, the
shrink->reset contract), so it is pinned directly. Route tests bind a
real server on an ephemeral port -- stdlib only, no sockets faked. The
eval routes run against a fixture eval root; the traversal probes use a
raw socket because urllib normalizes ``..`` away before sending.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator.inspect_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    _read_new,
    create_server,
    eval_root_setting,
    viewer_settings,
)
from graph_context.orchestrator.turn_log import DEFAULT_TURN_LOG, turn_log_path


class TestReadNew:
    def test_appended_whole_lines_advance_the_offset(self, tmp_path) -> None:
        log = tmp_path / "turns.jsonl"
        log.write_text('{"a":1}\n{"b":2}\n')
        offset, lines, reset = _read_new(log, 0)
        assert lines == ['{"a":1}', '{"b":2}']
        assert offset == log.stat().st_size
        assert reset is False

    def test_a_partial_trailing_line_is_left_for_the_next_poll(self, tmp_path) -> None:
        log = tmp_path / "turns.jsonl"
        log.write_text('{"a":1}\n{"partial"')
        offset, lines, _ = _read_new(log, 0)
        assert lines == ['{"a":1}']
        log.write_text('{"a":1}\n{"partial":true}\n')
        offset, lines, reset = _read_new(log, offset)
        assert lines == ['{"partial":true}']
        assert reset is False

    def test_a_shrunken_file_resets_and_replays_from_the_top(self, tmp_path) -> None:
        log = tmp_path / "turns.jsonl"
        log.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        offset, _, _ = _read_new(log, 0)
        log.write_text('{"c":3}\n')  # the diary's byte-budget trim
        offset, lines, reset = _read_new(log, offset)
        assert reset is True
        assert lines == ['{"c":3}']
        assert offset == log.stat().st_size

    def test_a_missing_file_yields_nothing_and_keeps_the_offset(self, tmp_path) -> None:
        offset, lines, reset = _read_new(tmp_path / "absent.jsonl", 42)
        assert (offset, lines, reset) == (42, [], False)


RESULTS = {
    "format": 2,
    "run": {"driver": "scripted", "model": "(scripted)", "label": "fix",
            "started": "2026-07-11T10:00:00+00:00",
            "finished": "2026-07-11T10:00:05+00:00", "ok": True},
    "cases": [{
        "id": "who_is_mira", "suite": "smoke", "must_fail": False,
        "skipped": False, "mode": "", "judge_rubric": "", "ok": True,
        "pass_rate": 1.0, "pass_any": True, "pass_all": True,
        "trials": [{
            "trial": 1, "passed": True, "session": "who_is_mira#t1",
            "system_prompt": "goal text", "bound_tools": ["get_node"],
            "harness_error": "", "decisions": 1, "executed_calls": 1,
            "latency_s": 0.1, "cost_usd": 0.0, "output_tokens": 0,
            "grades": [], "judge": None, "final_reply": "Mira exists.",
        }],
    }],
}


@pytest.fixture
def eval_root(tmp_path) -> Path:
    """A fixture eval root: one case file, one run with a transcript."""
    root = tmp_path / "evals"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "smoke.toml").write_text(
        '[suite]\nname = "smoke"\nprofile = "fiction"\nembedder = "off"\n'
        '[[case]]\nid = "who_is_mira"\ntrials = 1\n'
        '[[case.turn]]\nuser = "Who is Mira?"\n',
        encoding="utf-8",
    )
    run = root / "runs" / "20260711T100000Z-fix"
    run.mkdir(parents=True)
    (run / "results.json").write_text(json.dumps(RESULTS), encoding="utf-8")
    (run / "turns.jsonl").write_text(
        '{"event":"user","session":"who_is_mira#t1","text":"Who is Mira?"}\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture
def server(tmp_path, eval_root):
    """A live inspection server on an ephemeral loopback port."""
    log = tmp_path / "turns.jsonl"
    log.write_text('{"event":"user","text":"hi"}\n')
    server = create_server("127.0.0.1", 0, log, eval_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, log
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def _get_json(url: str) -> dict:
    data: dict = json.loads(_get(url)[1])
    return data


def _raw_get(base: str, path: str) -> int:
    """GET without urllib's path normalization (it collapses ``..``)."""
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        status_line = sock.makefile("rb").readline().decode()
    return int(status_line.split()[1])


class TestRoutes:
    def test_root_serves_the_dashboard_html(self, server) -> None:
        base, _ = server
        status, body = _get(f"{base}/")
        assert status == 200
        assert b"Inspection" in body

    def test_logs_serves_the_live_viewer_html(self, server) -> None:
        base, _ = server
        status, body = _get(f"{base}/logs")
        assert status == 200
        assert b"Turn log" in body

    def test_turns_jsonl_serves_the_raw_log_bytes(self, server) -> None:
        base, log = server
        status, body = _get(f"{base}/turns.jsonl")
        assert status == 200
        assert body == log.read_bytes()

    def test_an_unknown_route_is_404(self, server) -> None:
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(f"{base}/nope")
        assert err.value.code == 404

    def test_a_missing_page_fails_loudly_at_create(self, tmp_path, monkeypatch) -> None:
        from graph_context.orchestrator import inspect_server

        monkeypatch.setattr(inspect_server.Handler, "html_dir", tmp_path)
        with pytest.raises(GraphContextError, match="viewer HTML missing"):
            create_server("127.0.0.1", 0, tmp_path / "turns.jsonl")

    def test_the_old_module_path_still_works(self) -> None:
        # report.md footers in old runs name the pre-rename module.
        from graph_context.orchestrator import turn_log_server

        assert turn_log_server.create_server is create_server


class TestEvalApi:
    def test_summary_lists_cases_and_runs(self, server) -> None:
        base, _ = server
        data = _get_json(f"{base}/api/summary")
        (case,) = data["cases"]
        assert case["id"] == "who_is_mira"
        assert case["defined"] is True
        assert case["latest"]["ok"] is True
        (run,) = data["runs"]
        assert run["id"] == "20260711T100000Z-fix"
        assert run["has_transcript"] is True
        assert data["warnings"] == []

    def test_run_detail_returns_the_results_verbatim(self, server) -> None:
        base, _ = server
        data = _get_json(f"{base}/api/runs/20260711T100000Z-fix")
        trial = data["results"]["cases"][0]["trials"][0]
        assert trial["session"] == "who_is_mira#t1"
        assert trial["system_prompt"] == "goal text"

    def test_case_detail_joins_definition_and_history(self, server) -> None:
        base, _ = server
        data = _get_json(f"{base}/api/cases/who_is_mira")
        assert data["turns"] == ["Who is Mira?"]
        (entry,) = data["history"]
        assert entry["id"] == "20260711T100000Z-fix"
        assert entry["outcome"]["ok"] is True

    def test_unknown_ids_are_404(self, server) -> None:
        base, _ = server
        for url in ("api/runs/absent", "api/cases/absent"):
            with pytest.raises(urllib.error.HTTPError) as err:
                _get(f"{base}/{url}")
            assert err.value.code == 404

    def test_run_transcript_routes(self, server, eval_root) -> None:
        base, _ = server
        run = "20260711T100000Z-fix"
        _, body = _get(f"{base}/runs/{run}/turns.jsonl")
        assert body == (eval_root / "runs" / run / "turns.jsonl").read_bytes()
        _, body = _get(f"{base}/runs/{run}/log")
        assert b"Turn log" in body  # the same viewer, relative SSE

    def test_traversal_probes_are_404(self, server) -> None:
        base, _ = server
        for path in (
            "/runs/../cases/turns.jsonl",
            "/runs/%2e%2e/turns.jsonl",
            "/runs/.hidden/turns.jsonl",
            "/api/runs/..",
        ):
            assert _raw_get(base, path) == 404, path

    def test_no_eval_root_degrades_to_the_empty_state(self, tmp_path) -> None:
        log = tmp_path / "turns.jsonl"
        log.write_text("")
        server = create_server("127.0.0.1", 0, log, eval_root=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            data = _get_json(f"{base}/api/summary")
            assert data == {"eval_root": None, "cases": [],
                            "runs": [], "warnings": []}
            with pytest.raises(urllib.error.HTTPError) as err:
                _get(f"{base}/runs/x/log")
            assert err.value.code == 404
        finally:
            server.shutdown()
            server.server_close()


class TestTurnLogPathResolution:
    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " OFF "])
    def test_off_values_mean_no_diary(self, monkeypatch, value) -> None:
        monkeypatch.setenv("GC_TURN_LOG", value)
        assert turn_log_path() is None

    def test_unset_falls_back_to_the_default_path(self, monkeypatch) -> None:
        monkeypatch.delenv("GC_TURN_LOG", raising=False)
        assert turn_log_path() == DEFAULT_TURN_LOG

    def test_an_explicit_path_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_TURN_LOG", "elsewhere/diary.jsonl")
        assert turn_log_path() == "elsewhere/diary.jsonl"


class TestViewerSettings:
    def test_defaults_to_loopback_8765(self, monkeypatch) -> None:
        monkeypatch.delenv("GC_LOG_VIEWER_HOST", raising=False)
        monkeypatch.delenv("GC_LOG_VIEWER_PORT", raising=False)
        assert viewer_settings() == (DEFAULT_HOST, DEFAULT_PORT)

    def test_the_composed_container_host_binds_all_interfaces(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_LOG_VIEWER_HOST", "0.0.0.0")
        monkeypatch.delenv("GC_LOG_VIEWER_PORT", raising=False)
        assert viewer_settings() == ("0.0.0.0", DEFAULT_PORT)

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_port_off_values_disable_the_viewer(self, monkeypatch, value) -> None:
        monkeypatch.setenv("GC_LOG_VIEWER_PORT", value)
        assert viewer_settings() is None

    def test_a_non_integer_port_fails_loudly(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_LOG_VIEWER_PORT", "please")
        with pytest.raises(GraphContextError, match="GC_LOG_VIEWER_PORT"):
            viewer_settings()


class TestEvalRootSetting:
    def test_defaults_to_the_repo_conventional_evals(self, monkeypatch) -> None:
        monkeypatch.delenv("GC_EVAL_ROOT", raising=False)
        assert eval_root_setting() == Path("evals")

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_off_values_disable_the_eval_pages(self, monkeypatch, value) -> None:
        monkeypatch.setenv("GC_EVAL_ROOT", value)
        assert eval_root_setting() is None

    def test_an_explicit_path_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_EVAL_ROOT", "elsewhere/evals")
        assert eval_root_setting() == Path("elsewhere/evals")


class TestPackagedHtml:
    def test_both_pages_ship_next_to_the_module(self) -> None:
        # A missing file would only surface at first launch without this pin.
        from graph_context.orchestrator import inspect_server

        parent = Path(inspect_server.__file__).parent
        assert (parent / "turn_log_viewer.html").exists()
        assert (parent / "inspect.html").exists()
