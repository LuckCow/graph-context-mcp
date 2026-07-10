"""The turn-log viewer: tailing logic, HTTP routes, and env resolution.

``_read_new`` is the tail's whole brain (offsets, partial lines, the
shrink->reset contract), so it is pinned directly. Route tests bind a
real server on an ephemeral port -- stdlib only, no sockets faked.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator.turn_log import DEFAULT_TURN_LOG, turn_log_path
from graph_context.orchestrator.turn_log_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    _read_new,
    create_server,
    viewer_settings,
)


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


@pytest.fixture
def viewer(tmp_path):
    """A live viewer on an ephemeral loopback port, tailing a real file."""
    log = tmp_path / "turns.jsonl"
    log.write_text('{"event":"user","text":"hi"}\n')
    server = create_server("127.0.0.1", 0, log)
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


class TestRoutes:
    def test_root_serves_the_viewer_html(self, viewer) -> None:
        base, _ = viewer
        status, body = _get(f"{base}/")
        assert status == 200
        assert b"<" in body  # the packaged single-file UI, not an empty 200

    def test_turns_jsonl_serves_the_raw_log_bytes(self, viewer) -> None:
        base, log = viewer
        status, body = _get(f"{base}/turns.jsonl")
        assert status == 200
        assert body == log.read_bytes()

    def test_an_unknown_route_is_404(self, viewer) -> None:
        base, _ = viewer
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(f"{base}/nope")
        assert err.value.code == 404

    def test_a_missing_viewer_html_fails_loudly(self, tmp_path, monkeypatch) -> None:
        from graph_context.orchestrator import turn_log_server

        monkeypatch.setattr(
            turn_log_server.Handler, "html_path", tmp_path / "gone.html"
        )
        with pytest.raises(GraphContextError, match="viewer HTML missing"):
            create_server("127.0.0.1", 0, tmp_path / "turns.jsonl")


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


class TestViewerServer:
    def test_the_packaged_html_ships_next_to_the_module(self) -> None:
        # The UI moved into the package (from scripts/); a missing file
        # would only surface at first launch without this pin.
        from graph_context.orchestrator import turn_log_server

        html = Path(turn_log_server.__file__).parent / "turn_log_viewer.html"
        assert html.exists()
