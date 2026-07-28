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
    def test_all_pages_ship_next_to_the_module(self) -> None:
        # A missing file would only surface at first launch without this pin.
        from graph_context.orchestrator import inspect_server

        parent = Path(inspect_server.__file__).parent
        assert (parent / "turn_log_viewer.html").exists()
        assert (parent / "inspect.html").exists()
        assert (parent / "prose.html").exists()


# -- prose routes (WP43) ------------------------------------------------

PROSE_P1 = "The city fell quiet before the siege began, every gate barred."
PROSE_P2 = "Mira counted the engines twice; one was missing from the yard."
PROSE_TOKEN = "sekrit-token"


def _prose_hash(text: str) -> str:
    from graph_context.domain import revisions

    return revisions.block_hash(revisions.normalize_block(text))


@pytest.fixture
def prose_world():
    """A background loop hosting one registered space with a real
    historian -- the bridge's owning-loop side of the thread contract."""
    import asyncio

    from graph_context.application.node_historian import NodeHistorian
    from graph_context.domain import revisions
    from graph_context.domain.models import NodeDraft
    from graph_context.infrastructure.memory.fake_repository import (
        InMemoryGraphRepository,
    )
    from graph_context.orchestrator.prose_bridge import (
        ProseBridge,
        register_space,
    )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    bridge = ProseBridge()

    async def build() -> str:
        repo = InMemoryGraphRepository()
        await repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter"},
        ))
        node = await repo.create_node(NodeDraft(
            type="Chapter", name="Chapter One", summary="ch",
            body=f"{PROSE_P1}\n\n{PROSE_P2}",
        ))
        historian = NodeHistorian(repo, now=lambda: "T1")
        await historian.record_bot_revision(node.id, author_detail="m")
        register_space(
            bridge, space_id="sp1", label="Ashfall",
            historian=historian, repository=repo,
            route_lock=asyncio.Lock(),
        )
        return node.id

    node_id = asyncio.run_coroutine_threadsafe(build(), loop).result(10)
    try:
        yield bridge, node_id
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


@pytest.fixture
def prose_server(tmp_path, prose_world):
    """A live server with the prose bridge attached and writes enabled."""
    bridge, node_id = prose_world
    log = tmp_path / "turns.jsonl"
    log.write_text("")
    server = create_server(
        "127.0.0.1", 0, log, None, prose=bridge, prose_token=PROSE_TOKEN,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, node_id
    finally:
        server.shutdown()
        server.server_close()


def _post_mark(
    base: str,
    payload: dict,
    *,
    token: str | None = PROSE_TOKEN,
    origin: str | None = None,
    fetch_site: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    request = urllib.request.Request(
        f"{base}/api/prose/mark", data=json.dumps(payload).encode(),
        method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {}


def _mark_payload(node_id: str, **overrides) -> dict:
    payload = {
        "space": "sp1", "node": node_id, "hash": _prose_hash(PROSE_P1),
        "kind": "intent", "value": "locked",
    }
    payload.update(overrides)
    return payload


class TestProseReads:
    def test_the_page_serves_with_a_viewport_meta(self, prose_server) -> None:
        base, _ = prose_server
        status, body = _get(f"{base}/prose")
        assert status == 200
        assert b'name="viewport"' in body  # phone-readable, WP43

    def test_spaces_lists_tracked_nodes_and_write_state(
        self, prose_server
    ) -> None:
        base, node_id = prose_server
        payload = _get_json(f"{base}/api/prose/spaces")
        assert payload["writes_enabled"] is True
        (space,) = payload["spaces"]
        assert (space["space_id"], space["label"]) == ("sp1", "Ashfall")
        (row,) = space["nodes"]
        assert row["id"] == node_id and row["name"] == "Chapter One"

    def test_node_view_and_diff_round_trip(self, prose_server) -> None:
        base, node_id = prose_server
        view = _get_json(f"{base}/api/prose/node?space=sp1&node={node_id}")
        assert [b["hash"] for b in view["blocks"]] == [
            _prose_hash(PROSE_P1), _prose_hash(PROSE_P2),
        ]
        diff = _get_json(
            f"{base}/api/prose/diff?space=sp1&node={node_id}&seq=1"
        )
        assert len(diff["pairs"]) == 2  # the opening keyframe: two adds

    def test_unknown_space_node_and_params_are_404_400(
        self, prose_server
    ) -> None:
        base, node_id = prose_server
        with pytest.raises(urllib.error.HTTPError) as e404:
            _get(f"{base}/api/prose/node?space=nope&node={node_id}")
        assert e404.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e404b:
            _get(f"{base}/api/prose/node?space=sp1&node=ghost")
        assert e404b.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e400:
            _get(f"{base}/api/prose/node?space=sp1")
        assert e400.value.code == 400

    def test_no_bridge_renders_the_empty_state(self, server) -> None:
        base, _ = server  # the plain fixture: create_server without prose
        payload = _get_json(f"{base}/api/prose/spaces")
        assert payload == {"spaces": [], "writes_enabled": False}


class TestProseWrites:
    def test_a_mark_lands_and_echoes_the_folded_state(
        self, prose_server
    ) -> None:
        base, node_id = prose_server
        status, result = _post_mark(base, _mark_payload(node_id))
        assert status == 200
        assert result["intent"] == "locked"

    def test_missing_or_wrong_token_is_401(self, prose_server) -> None:
        base, node_id = prose_server
        assert _post_mark(base, _mark_payload(node_id), token=None)[0] == 401
        assert _post_mark(base, _mark_payload(node_id), token="wrong")[0] == 401

    def test_cross_origin_is_403_even_with_the_right_token(
        self, prose_server
    ) -> None:
        base, node_id = prose_server
        status, _ = _post_mark(
            base, _mark_payload(node_id), origin="http://evil.example",
        )
        assert status == 403
        status, _ = _post_mark(
            base, _mark_payload(node_id), fetch_site="cross-site",
        )
        assert status == 403

    def test_same_origin_headers_pass(self, prose_server) -> None:
        base, node_id = prose_server
        status, _ = _post_mark(
            base, _mark_payload(node_id, value="needs_change"),
            origin=base, fetch_site="same-origin",
        )
        assert status == 200

    def test_a_stale_hash_is_409(self, prose_server) -> None:
        base, node_id = prose_server
        status, _ = _post_mark(
            base, _mark_payload(node_id, hash="feedbeefcafe0123"),
        )
        assert status == 409

    def test_bad_values_and_bodies_are_400(self, prose_server) -> None:
        base, node_id = prose_server
        assert _post_mark(
            base, _mark_payload(node_id, value="golden"),
        )[0] == 400
        assert _post_mark(base, {"space": "sp1"})[0] == 400

    def test_writes_disabled_without_a_token_is_403(
        self, tmp_path, prose_world
    ) -> None:
        bridge, node_id = prose_world
        log = tmp_path / "t.jsonl"
        log.write_text("")
        server = create_server(
            "127.0.0.1", 0, log, None, prose=bridge, prose_token="",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            status, _ = _post_mark(base, _mark_payload(node_id), token="x")
            assert status == 403
            payload = _get_json(f"{base}/api/prose/spaces")
            assert payload["writes_enabled"] is False
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_post_routes_stay_404(self, prose_server) -> None:
        base, _ = prose_server
        request = urllib.request.Request(
            f"{base}/turns.jsonl", data=b"{}", method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request, timeout=5)
        assert err.value.code == 404


class TestProseTokenSetting:
    def test_default_is_read_only(self, monkeypatch) -> None:
        from graph_context.orchestrator.inspect_server import prose_token_setting

        monkeypatch.delenv("GC_PROSE_TOKEN", raising=False)
        assert prose_token_setting() == ""

    def test_off_values_disable_and_tokens_pass_through(
        self, monkeypatch
    ) -> None:
        from graph_context.orchestrator.inspect_server import prose_token_setting

        monkeypatch.setenv("GC_PROSE_TOKEN", "off")
        assert prose_token_setting() == ""
        monkeypatch.setenv("GC_PROSE_TOKEN", "  hunter2  ")
        assert prose_token_setting() == "hunter2"
