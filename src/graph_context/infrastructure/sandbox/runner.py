"""Parent side of the sandbox: spawn, feed, cap, and kill (WP32, ADR 040).

``SubprocessScriptRunner`` implements the ``ScriptRunner`` port by
running ``bootstrap.py`` under ``python -I -S`` in its own session with
a scrubbed environment. The child lowers its own resource limits
(CPU/memory/files -- see bootstrap); the parent owns the two things the
child cannot: the WALL-CLOCK kill (a sleeping script burns no CPU, so
RLIMIT_CPU alone never fires) and the OUTPUT cap (an ``os.write(1, …)``
bomb bypasses the child's in-process stdout swap).

Threat model (ADR 040): scripts are authored by the space owner; this
contains accidents -- runaway loops, memory bombs, output floods -- not
a hostile author. The env scrub keeps secrets (ANYTYPE_API_KEY & co)
out of reach either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graph_context.errors import GraphContextError
from graph_context.infrastructure.sandbox import bootstrap
from graph_context.ports.script_runner import ScriptEffect, ScriptOutcome

logger = logging.getLogger(__name__)

_BOOTSTRAP = str(Path(bootstrap.__file__))
_STDERR_TAIL = 64 * 1024  # keep the END: a traceback's last lines matter


class SubprocessScriptRunner:
    """The production :class:`ScriptRunner`: one child process per run."""

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        max_output_bytes: int = 1024 * 1024,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes

    async def run(
        self, script: str, payload: Mapping[str, Any]
    ) -> ScriptOutcome:
        wire = json.dumps({**dict(payload), "script": script})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", _BOOTSTRAP,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_scrubbed_env(),
            start_new_session=True,  # its own group: killpg reaps strays
            cwd="/",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                self._pump(process, wire.encode()), self._timeout
            )
        except TimeoutError:
            await _kill_and_reap(process)
            raise GraphContextError(
                f"the script exceeded its {self._timeout:g}s time limit "
                "and was stopped -- remove long loops or waits"
            ) from None
        except _OutputOverflow:
            await _kill_and_reap(process)
            raise GraphContextError(
                "the script produced too much output and was stopped"
            ) from None
        if process.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-_STDERR_TAIL:]
            raise GraphContextError(
                f"the script failed: {tail or 'no error output'}"
            )
        return _parse_outcome(stdout)

    async def _pump(
        self, process: asyncio.subprocess.Process, payload: bytes
    ) -> tuple[bytes, bytes]:
        """Feed stdin, drain stdout/stderr concurrently with caps."""
        assert process.stdin and process.stdout and process.stderr

        async def feed() -> None:
            assert process.stdin
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                # The child died before reading everything (rlimit kill,
                # crash-on-start): the exit code + stderr tell the story.
                pass

        async def drain_stdout() -> bytes:
            assert process.stdout
            chunks: list[bytes] = []
            total = 0
            while chunk := await process.stdout.read(64 * 1024):
                total += len(chunk)
                if total > self._max_output:
                    raise _OutputOverflow()
                chunks.append(chunk)
            return b"".join(chunks)

        async def drain_stderr() -> bytes:
            assert process.stderr
            kept = bytearray()
            while chunk := await process.stderr.read(64 * 1024):
                kept.extend(chunk)
                if len(kept) > _STDERR_TAIL:
                    del kept[: len(kept) - _STDERR_TAIL]
            return bytes(kept)

        _, stdout, stderr = await asyncio.gather(
            feed(), drain_stdout(), drain_stderr()
        )
        await process.wait()
        return stdout, stderr


class _OutputOverflow(Exception):
    """Internal: the child exceeded the stdout cap."""


def _scrubbed_env() -> dict[str, str]:
    # Nothing inherited: no ANYTYPE_API_KEY, no ANTHROPIC_*, no PYTHON*
    # hooks. PATH only so the interpreter's own machinery works.
    return {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole group, then drain its pipes to EOF.

    The drain is load-bearing, not tidiness: ``process.wait()`` blocks
    until every pipe transport disconnects, and a pipe whose reader
    stopped (output overflow) or was cancelled (wall timeout) still
    holds buffered data -- reading was paused by flow control, EOF is
    never seen, and wait() hangs forever on an already-dead child. The
    child is dead, so the drain is bounded by the pipe buffers. The 5s
    belt covers platform weirdness; a leaked zombie beats a hung tick.
    """
    with contextlib.suppress(ProcessLookupError):  # already gone is fine
        os.killpg(process.pid, signal.SIGKILL)  # new session: pgid == pid

    async def reap() -> None:
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                while await stream.read(64 * 1024):
                    pass
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        await process.wait()

    try:
        await asyncio.wait_for(reap(), 5.0)
    except TimeoutError:
        logger.warning("sandbox child %s did not reap cleanly", process.pid)


def _parse_outcome(stdout: bytes) -> ScriptOutcome:
    try:
        raw = json.loads(stdout.decode() or "{}")
        sets = tuple(
            ScriptEffect(
                node_id=str(entry["id"]),
                property=str(entry["property"]),
                value=str(entry["value"]),
            )
            for entry in raw.get("sets", [])
        )
        logs = tuple(str(line) for line in raw.get("logs", []))
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        raise GraphContextError(
            f"the script runner returned malformed output ({err}); "
            "this is a sandbox bug, not a script bug"
        ) from None
    return ScriptOutcome(sets=sets, logs=logs)
