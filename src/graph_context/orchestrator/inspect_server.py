"""The inspection server: eval-run review UI + the live turn-log viewer.

    python -m graph_context.orchestrator.inspect_server
    python -m graph_context.orchestrator.inspect_server --log logs/turns.jsonl --port 8765

Grew out of the turn-log viewer (the old module path still works as a
shim): one process now serves both the behavioral-eval dashboard and the
live turn diary. Routes:

  GET /                        -> inspect.html (dashboard / run / case pages)
  GET /logs                    -> turn_log_viewer.html tailing the LIVE log
  GET /turns.jsonl             -> the raw live log (curl / download)
  GET /events                  -> SSE: replay the live log, then tail it
  GET /runs/<id>/log           -> turn_log_viewer.html over one eval run
  GET /runs/<id>/events        -> SSE over that run's turns.jsonl
  GET /runs/<id>/turns.jsonl   -> that run's raw transcript
  GET /api/summary             -> all cases + runs (eval_index.summary)
  GET /api/runs/<id>           -> one run's results.json, normalized
  GET /api/cases/<id>          -> one case's definition + result history
  GET /prose                   -> prose.html (WP43: the review page)
  GET /api/prose/spaces        -> registered spaces + their tracked nodes
  GET /api/prose/node?space&node    -> blocks + blame + review state
  GET /api/prose/diff?space&node&seq -> one revision's word-level spans
  POST /api/prose/mark         -> set one block's status/intent (WP42)

The prose routes speak to live space runtimes through a ``ProseBridge``
(``prose_bridge.py``): the bridge is handed over EMPTY before the bots
bootstrap and spaces register as they come up, so the server still
starts first and the page degrades to an empty state everywhere else
(standalone launches, memory backend, viewer-only runs).

``POST /api/prose/mark`` is this server's FIRST and ONLY non-GET route.
Read-only-by-construction was a load-bearing safety property, so writes
are doubly gated: same-origin enforcement (``Sec-Fetch-Site`` when the
browser sends it, ``Origin``-vs-``Host`` otherwise) refuses drive-by
requests from other pages, and a shared bearer token (``GC_PROSE_TOKEN``;
unset = writes disabled, the page is read-only) authenticates the
human. GETs stay tokenless.

The viewer HTML reaches its stream via a RELATIVE ``events`` URL, which
is what lets the same file serve both the live log (``/logs`` ->
``/events``) and any run replay (``/runs/<id>/log`` ->
``/runs/<id>/events``) without a line of routing JS.

Stdlib only -- no dependencies. ``/events`` is a long-lived connection, so
we use ``ThreadingHTTPServer`` (one thread per client) to keep the other
routes responsive while streams are open. When a tailed log's byte size
shrinks (the diary's byte-budget trim rewrites the file, dropping the
oldest entries) the stream emits a ``reset`` event and replays from the
top, so the viewer stays consistent instead of desyncing on the offset.

The consolidated server (``serve``) hosts this in a daemon thread via
``create_server``; standalone launches go through ``main()``'s argparse,
whose defaults read the same GC_LOG_VIEWER_HOST / GC_LOG_VIEWER_PORT /
GC_EVAL_ROOT env knobs so both paths bind identically inside the
devcontainer.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from graph_context.errors import GraphContextError, StaleSectionMark
from graph_context.orchestrator import eval_index
from graph_context.orchestrator.prose_bridge import ProseBridge
from graph_context.orchestrator.turn_log import (
    DEFAULT_TURN_LOG,
    OFF_VALUES,
    turn_log_path,
)

POLL_SECONDS = 0.5
HEARTBEAT_TICKS = 20  # send an SSE comment after this many idle polls (~10s)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_EVAL_ROOT = "evals"
MAX_MARK_BODY_BYTES = 65_536  # a mark payload is tiny; anything big is abuse


def viewer_settings() -> tuple[str, int] | None:
    """GC_LOG_VIEWER_HOST / GC_LOG_VIEWER_PORT resolution -> (host, port).

    None when the port is an off-value (the server is switched off).
    The host defaults to loopback; the devcontainer composes ``0.0.0.0``
    because Docker's published-port DNAT arrives on eth0, not loopback
    (the host side stays pinned to 127.0.0.1 by the compose mapping).
    A non-integer port fails loudly, like every other config knob.
    """
    raw = os.environ.get("GC_LOG_VIEWER_PORT", str(DEFAULT_PORT)).strip()
    if raw.lower() in OFF_VALUES:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise GraphContextError(
            f"GC_LOG_VIEWER_PORT must be an integer or off, got {raw!r}"
        ) from None
    host = os.environ.get("GC_LOG_VIEWER_HOST", "").strip() or DEFAULT_HOST
    return host, port


def prose_token_setting() -> str:
    """GC_PROSE_TOKEN resolution -> the shared write token, or ``""``.

    Empty/off means the prose page is read-only (marks return 403) --
    the operator consciously enables the server's only write surface.
    Follows the off-value convention of every other knob."""
    raw = os.environ.get("GC_PROSE_TOKEN", "").strip()
    if raw.lower() in OFF_VALUES:
        return ""
    return raw


def eval_root_setting() -> Path | None:
    """GC_EVAL_ROOT resolution -> the eval artifacts directory, or None.

    Defaults to the repo-conventional ``evals`` (relative to the process
    cwd, like the turn log's default); off-values disable the eval pages
    while the log viewer keeps working. A missing directory is NOT an
    error -- the dashboard renders its empty state, so the server boots
    the same everywhere.
    """
    raw = os.environ.get("GC_EVAL_ROOT", DEFAULT_EVAL_ROOT).strip()
    if raw.lower() in OFF_VALUES:
        return None
    return Path(raw)


def _read_new(path: Path, offset: int) -> tuple[int, list[str], bool]:
    """Return (new_offset, complete_new_lines, reset).

    Only whole lines are consumed; a trailing partial line leaves the
    offset short so it is picked up once the writer finishes it. ``reset``
    is True when the file shrank (a trim/clear), meaning the caller should
    tell the client to reload before the replayed lines.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return offset, [], False
    reset = False
    if size < offset:  # the file was trimmed/rotated out from under us
        offset, reset = 0, True
    if size <= offset:
        return offset, [], reset
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(size - offset)
    end = chunk.rfind(b"\n")
    if end == -1:
        return offset, [], reset  # only a partial line appended so far
    complete = chunk[: end + 1]
    lines = complete.decode("utf-8", "replace").splitlines()
    return offset + len(complete), lines, reset


class Handler(BaseHTTPRequestHandler):
    # Configured on the CLASS by create_server() -- one server per process.
    log_path: Path = Path(DEFAULT_TURN_LOG)
    html_dir: Path = Path(__file__).resolve().parent
    eval_root: Path | None = None
    prose: ProseBridge | None = None
    prose_token: str = ""

    def do_GET(self) -> None:  # http.server naming
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._serve_file(self.html_dir / "inspect.html",
                             "text/html; charset=utf-8")
        elif route == "/logs":
            self._serve_file(self.html_dir / "turn_log_viewer.html",
                             "text/html; charset=utf-8")
        elif route == "/prose":
            self._serve_file(self.html_dir / "prose.html",
                             "text/html; charset=utf-8")
        elif route == "/api/prose/spaces":
            self._serve_prose_spaces()
        elif route == "/api/prose/node":
            self._serve_prose_call(
                "node_view", ("node",), lambda q: (q["node"],)
            )
        elif route == "/api/prose/diff":
            self._serve_prose_call(
                "revision_diff", ("node", "seq"),
                lambda q: (q["node"], int(q["seq"])),
            )
        elif route == "/turns.jsonl":
            self._serve_file(self.log_path,
                             "application/x-ndjson; charset=utf-8")
        elif route == "/events":
            self._serve_events(self.log_path)
        elif route == "/api/summary":
            self._serve_summary()
        elif route.startswith("/api/runs/"):
            self._serve_api_detail(route.removeprefix("/api/runs/"),
                                   eval_index.run_detail)
        elif route.startswith("/api/cases/"):
            self._serve_api_detail(route.removeprefix("/api/cases/"),
                                   eval_index.case_detail)
        elif route.startswith("/runs/"):
            self._serve_run_route(route.removeprefix("/runs/"))
        else:
            self.send_error(404, "not found")

    # -- eval routes ---------------------------------------------------------

    def _serve_summary(self) -> None:
        if self.eval_root is None:
            self._serve_json(
                {"eval_root": None, "cases": [], "runs": [], "warnings": []}
            )
            return
        self._serve_json(eval_index.summary(self.eval_root))

    def _serve_api_detail(self, name: str, lookup: Any) -> None:
        payload = None if self.eval_root is None else lookup(self.eval_root, name)
        if payload is None:
            self.send_error(404, "not found")
            return
        self._serve_json(payload)

    def _serve_run_route(self, rest: str) -> None:
        """``/runs/<id>/(log|events|turns.jsonl)`` -- the per-run viewer.

        The id passes through ``eval_index.safe_child`` (single plain path
        segment, resolved containment) so a crafted URL cannot escape the
        runs directory.
        """
        run_id, _, tail = rest.partition("/")
        log = (
            None if self.eval_root is None
            else eval_index.run_log_path(self.eval_root, run_id)
        )
        if log is None:
            self.send_error(404, "not found")
        elif tail == "log":
            self._serve_file(self.html_dir / "turn_log_viewer.html",
                             "text/html; charset=utf-8")
        elif tail == "events":
            self._serve_events(log)
        elif tail == "turns.jsonl":
            self._serve_file(log, "application/x-ndjson; charset=utf-8")
        else:
            self.send_error(404, "not found")

    # -- prose routes (WP43) ---------------------------------------------

    def _serve_prose_spaces(self) -> None:
        """Registered spaces + their tracked-node lists; the page's
        entry point. Boots-anywhere: no bridge / no spaces / a space
        whose loop is busy all degrade to renderable JSON, never a 500."""
        spaces = []
        if self.prose is not None:
            for space_id, label in self.prose.spaces():
                row: dict[str, Any] = {"space_id": space_id, "label": label}
                try:
                    row["nodes"] = self.prose.call(space_id, "list_nodes")
                except (GraphContextError, TimeoutError) as err:
                    row["nodes"] = []
                    row["error"] = str(err)
                spaces.append(row)
        self._serve_json({
            "spaces": spaces,
            "writes_enabled": bool(self.prose_token),
        })

    def _serve_prose_call(
        self,
        name: str,
        required: tuple[str, ...],
        args_of: Any,
    ) -> None:
        """One bridge read (`node_view`/`revision_diff`) off the query
        string: ?space=<id> plus ``required`` params -> the callable's
        JSON. Unknown space/node/seq -> 404; a busy loop -> 504."""
        query = {
            key: values[0]
            for key, values in parse_qs(urlparse(self.path).query).items()
        }
        if "space" not in query or any(k not in query for k in required):
            self.send_error(
                400, f"missing query params (need space, {', '.join(required)})"
            )
            return
        if self.prose is None:
            self.send_error(404, "no live spaces")
            return
        try:
            payload = self.prose.call(query["space"], name, *args_of(query))
        except (KeyError, ValueError):
            self.send_error(404, "unknown space (or bad seq)")
            return
        except GraphContextError as err:
            self.send_error(404, str(err))
            return
        except TimeoutError:
            self.send_error(504, "space loop busy; retry")
            return
        self._serve_json(payload)

    def do_POST(self) -> None:  # http.server naming
        """The server's ONLY write: set one block's status/intent mark.

        Gate order matters: origin first (drive-by pages get nothing,
        not even a token prompt), then the token (401 tells the page to
        prompt; 403 'writes disabled' tells it to stay read-only)."""
        route = urlparse(self.path).path
        if route != "/api/prose/mark":
            self.send_error(404, "not found")
            return
        if not self._same_origin():
            self.send_error(403, "cross-origin writes are refused")
            return
        if not self.prose_token:
            self.send_error(
                403, "writes disabled: set GC_PROSE_TOKEN to enable marks"
            )
            return
        if not self._bearer_ok():
            self.send_error(401, "bad or missing bearer token")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if not 0 < length <= MAX_MARK_BODY_BYTES:
            self.send_error(400, "bad request body size")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "body must be JSON")
            return
        fields = ("space", "node", "hash", "kind", "value")
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(k), str) or not payload[k]
            for k in fields
        ):
            self.send_error(
                400, f"body needs string fields: {', '.join(fields)}"
            )
            return
        if self.prose is None:
            self.send_error(404, "no live spaces")
            return
        try:
            result = self.prose.call(
                payload["space"], "set_mark", payload["node"],
                payload["hash"], payload["kind"], payload["value"],
            )
        except KeyError:
            self.send_error(404, "unknown space")
            return
        except StaleSectionMark as err:
            self.send_error(409, str(err))
            return
        except GraphContextError as err:
            self.send_error(400, str(err))
            return
        except TimeoutError:
            self.send_error(504, "space loop busy; retry")
            return
        self._serve_json(result)

    def _same_origin(self) -> bool:
        """Refuse cross-site browser requests. ``Sec-Fetch-Site`` is
        authoritative when present (modern browsers always send it);
        ``Origin`` vs ``Host`` covers the rest. A bare client (curl,
        tests) sends neither header and passes -- the TOKEN is the
        authentication; this check only stops drive-by pages in the
        operator's own browser."""
        site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin", "").strip()
        if origin:
            host = self.headers.get("Host", "").strip()
            if urlparse(origin).netloc != host:
                return False
        return True

    def _bearer_ok(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {self.prose_token}")

    # -- plumbing ------------------------------------------------------------

    def _serve_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_events(self, log: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # defeat proxy buffering
        self.end_headers()
        offset, idle = 0, 0
        try:
            while True:
                offset, lines, reset = _read_new(log, offset)
                if reset:
                    self._send(b"event: reset\ndata: 1\n\n")
                for line in lines:
                    text = line.rstrip("\r")
                    if text:
                        self._send(b"data: " + text.encode("utf-8") + b"\n\n")
                if lines or reset:
                    idle = 0
                else:
                    idle += 1
                    if idle >= HEARTBEAT_TICKS:  # keep-alive + disconnect probe
                        self._send(b": ping\n\n")
                        idle = 0
                time.sleep(POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass  # client went away -- end the stream quietly

    def _send(self, frame: bytes) -> None:
        self.wfile.write(frame)
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # typeshed signature
        pass  # silence per-request stderr noise (heartbeats would spam it)


def create_server(
    host: str,
    port: int,
    log: Path,
    eval_root: Path | None = None,
    *,
    prose: ProseBridge | None = None,
    prose_token: str = "",
) -> ThreadingHTTPServer:
    """A ready-to-serve inspection server bound to ``host:port``.

    Tails ``log`` on the live routes; ``eval_root`` (usually the repo's
    ``evals/`` directory) feeds the dashboard, None disables the eval
    pages. ``prose`` is the (initially empty) space registry the chat
    bot fills as runtimes come up (WP43); None renders the prose page's
    empty state. ``prose_token`` gates the mark write route -- empty
    keeps the server read-only. Config rides on the Handler CLASS, so a
    process hosts at most one server -- fine for both entry paths
    (serve and standalone).
    """
    for page in ("inspect.html", "turn_log_viewer.html", "prose.html"):
        if not (Handler.html_dir / page).exists():
            raise GraphContextError(f"viewer HTML missing: {Handler.html_dir / page}")
    Handler.log_path = log
    Handler.eval_root = eval_root
    Handler.prose = prose
    Handler.prose_token = prose_token
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    # Standalone launch is an explicit ask for the server, so env off-values
    # degrade to the defaults here (same posture as the --log fallback below)
    # instead of refusing to start.
    host, port = viewer_settings() or (DEFAULT_HOST, DEFAULT_PORT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    # An off GC_TURN_LOG means "no diary"; the standalone server still
    # serves the default path rather than erroring on a bogus filename.
    parser.add_argument("--log", default=turn_log_path() or DEFAULT_TURN_LOG,
                        help="path to the turns.jsonl to serve")
    parser.add_argument("--eval-root",
                        default=str(eval_root_setting() or DEFAULT_EVAL_ROOT),
                        help="eval artifacts directory (cases/ + runs/)")
    args = parser.parse_args()

    server = create_server(
        args.host, args.port, Path(args.log), Path(args.eval_root),
        prose_token=prose_token_setting(),  # no bridge standalone: the
        # prose page renders its empty state; marks 404 on "no spaces"
    )
    print(f"inspection server: http://{args.host}:{args.port}/  "
          f"(live log {Handler.log_path}, evals {Handler.eval_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
