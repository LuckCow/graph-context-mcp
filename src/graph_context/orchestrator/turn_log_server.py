"""Serve the turn-log viewer with live Server-Sent-Events tailing.

    python -m graph_context.orchestrator.turn_log_server
    python -m graph_context.orchestrator.turn_log_server --log logs/turns.jsonl --port 8765

Routes:
  GET /            -> turn_log_viewer.html (the single-file UI, packaged here)
  GET /turns.jsonl -> the raw current log (for curl / download)
  GET /events      -> a text/event-stream that first replays the whole log,
                      then pushes each newly appended line as it arrives.

Stdlib only -- no dependencies. ``/events`` is a long-lived connection, so
we use ``ThreadingHTTPServer`` (one thread per client) to keep ``/`` and
``/turns.jsonl`` responsive while a stream is open. When the log's byte
size shrinks (the diary's byte-budget trim rewrites the file, dropping the
oldest entries) the stream emits a ``reset`` event and replays from the
top, so the viewer stays consistent instead of desyncing on the offset.

The consolidated server (``serve``) hosts this in a daemon thread via
``create_server``; standalone launches go through ``main()``'s argparse,
whose defaults read the same GC_LOG_VIEWER_HOST / GC_LOG_VIEWER_PORT env
knobs so both paths bind identically inside the devcontainer.
"""

from __future__ import annotations

import argparse
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from graph_context.errors import GraphContextError
from graph_context.orchestrator.turn_log import (
    DEFAULT_TURN_LOG,
    OFF_VALUES,
    turn_log_path,
)

POLL_SECONDS = 0.5
HEARTBEAT_TICKS = 20  # send an SSE comment after this many idle polls (~10s)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def viewer_settings() -> tuple[str, int] | None:
    """GC_LOG_VIEWER_HOST / GC_LOG_VIEWER_PORT resolution -> (host, port).

    None when the port is an off-value (the viewer is switched off).
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
    # Configured on the CLASS by create_server() -- one viewer per process.
    log_path: Path = Path(DEFAULT_TURN_LOG)
    html_path: Path = Path(__file__).resolve().parent / "turn_log_viewer.html"

    def do_GET(self) -> None:  # http.server naming
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._serve_file(self.html_path, "text/html; charset=utf-8")
        elif route == "/turns.jsonl":
            self._serve_file(self.log_path, "application/x-ndjson; charset=utf-8")
        elif route == "/events":
            self._serve_events()
        else:
            self.send_error(404, "not found")

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

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # defeat proxy buffering
        self.end_headers()
        offset, idle = 0, 0
        try:
            while True:
                offset, lines, reset = _read_new(self.log_path, offset)
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


def create_server(host: str, port: int, log: Path) -> ThreadingHTTPServer:
    """A ready-to-serve viewer bound to ``host:port``, tailing ``log``.

    Config rides on the Handler CLASS, so a process hosts at most one
    viewer -- fine for both entry paths (serve and standalone).
    """
    if not Handler.html_path.exists():
        raise GraphContextError(f"viewer HTML missing: {Handler.html_path}")
    Handler.log_path = log
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    # Standalone launch is an explicit ask for the viewer, so env off-values
    # degrade to the defaults here (same posture as the --log fallback below)
    # instead of refusing to start.
    host, port = viewer_settings() or (DEFAULT_HOST, DEFAULT_PORT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    # An off GC_TURN_LOG means "no diary"; the standalone viewer still
    # serves the default path rather than erroring on a bogus filename.
    parser.add_argument("--log", default=turn_log_path() or DEFAULT_TURN_LOG,
                        help="path to the turns.jsonl to serve")
    args = parser.parse_args()

    server = create_server(args.host, args.port, Path(args.log))
    print(f"turn-log viewer: http://{args.host}:{args.port}/  "
          f"(tailing {Handler.log_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
