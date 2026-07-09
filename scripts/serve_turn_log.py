"""Serve the turn-log viewer with live Server-Sent-Events tailing.

    python scripts/serve_turn_log.py            # then open the printed URL
    python scripts/serve_turn_log.py --log logs/turns.jsonl --port 8765

Routes:
  GET /            -> scripts/turn_log_viewer.html (the single-file UI)
  GET /turns.jsonl -> the raw current log (for curl / download)
  GET /events      -> a text/event-stream that first replays the whole log,
                      then pushes each newly appended line as it arrives.

Stdlib only -- no dependencies. ``/events`` is a long-lived connection, so
we use ``ThreadingHTTPServer`` (one thread per client) to keep ``/`` and
``/turns.jsonl`` responsive while a stream is open. When the log's byte
size shrinks (the diary's byte-budget trim rewrites the file, dropping the
oldest entries) the stream emits a ``reset`` event and replays from the
top, so the viewer stays consistent instead of desyncing on the offset.
"""

from __future__ import annotations

import argparse
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Mirrors bootstrap.DEFAULT_TURN_LOG; off-values mean "no log", so the
# viewer falls back to the default path rather than a bogus filename.
DEFAULT_LOG = "logs/turns.jsonl"
_OFF = {"0", "false", "no", "off", ""}

POLL_SECONDS = 0.5
HEARTBEAT_TICKS = 20  # send an SSE comment after this many idle polls (~10s)


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
    # Set on the class in main() before the server starts.
    log_path: Path = Path(DEFAULT_LOG)
    html_path: Path = Path(__file__).resolve().parent / "turn_log_viewer.html"

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
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

    def log_message(self, *args: object) -> None:
        pass  # silence per-request stderr noise (heartbeats would spam it)


def _default_log() -> str:
    value = os.environ.get("GC_TURN_LOG", DEFAULT_LOG)
    return DEFAULT_LOG if value.strip().lower() in _OFF else value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", default=_default_log(),
                        help="path to the turns.jsonl to serve")
    args = parser.parse_args()

    Handler.log_path = Path(args.log)
    if not Handler.html_path.exists():
        parser.error(f"viewer HTML missing: {Handler.html_path}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
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
