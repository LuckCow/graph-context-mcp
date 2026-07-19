"""The sandbox CHILD program (WP32, ADR 040).

Runs as ``python -I -S bootstrap.py`` in a scrubbed environment: reads
one JSON payload from stdin (the rule's script plus a graph snapshot),
executes the script against a tiny read/queue API, and prints the
queued effects as JSON on stdout. The parent (``runner.py``) owns the
wall-clock kill; this program's first act is to lower its OWN resource
limits -- hard limits cannot be raised back by an unprivileged process,
so the executed script cannot undo them.

MUST stay stdlib-only and self-contained: under ``-I -S`` the
``graph_context`` package is not importable. ``execute`` is also a
plain importable function so unit tests exercise the script API
in-process, without a subprocess.

The script author's API (globals available to the script):

- ``now`` -- engine-injected local timestamp ``"YYYY-MM-DD HH:MM:SS"``
  (deterministic: scripts should never read the clock themselves).
- ``rule_name`` -- the firing rule's name.
- ``trigger`` -- the triggering object as a dict:
  ``{"id", "type", "name", "summary", "fields"}``.
- ``before`` / ``after`` -- the watched property's value before/after
  the transition (``""`` means empty/unticked).
- ``objects(type=None)`` -- every exported object, optionally filtered
  by type name (case-insensitive).
- ``find(name, type=None)`` -- the object whose name matches
  case-insensitively: an exact match wins, else a UNIQUE substring
  match, else ``None``.
- ``field(obj_or_id, property)`` -- a property value, case-insensitive
  key match; ``""`` when absent (absence IS the empty/false value).
- ``neighbors(obj_or_id, edge_type=None)`` -- linked objects as
  ``{"edge": label, "direction": "out"|"in", "node": object}`` dicts.
- ``set(obj_or_id, property, value)`` -- queue one property write.
  ``value`` may be str, bool (-> "true"/"false"), int, or float;
  anything else raises TypeError. Queuing past the cap raises
  RuntimeError. The last write to the same (object, property) wins.
- ``log(msg)`` -- record a line for the bot's log (``print`` output is
  discarded -- stdout belongs to the effects protocol).
"""

from __future__ import annotations

import io
import json
import sys
import traceback
from typing import Any

_DEFAULT_MAX_SETS = 20
_MAX_LOGS = 50
_LOG_CHARS = 200
_PRINT_CAP = 64 * 1024  # script print() output beyond this is dropped
_SCRIPT_FILENAME = "<rule script>"


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Run ``payload["script"]`` against the script API; return the
    outcome ``{"sets": [{"id","property","value"}...], "logs": [...]}``.

    Pure with respect to the process: no rlimits, no stdio games --
    ``__main__`` adds those. Raises whatever the script raises (the
    caller formats the traceback for the author).
    """
    nodes: list[dict[str, Any]] = payload.get("nodes", [])
    edges: list[dict[str, Any]] = payload.get("edges", [])
    max_sets = int(payload.get("caps", {}).get("max_sets", _DEFAULT_MAX_SETS))
    by_id: dict[str, dict[str, Any]] = {node["id"]: node for node in nodes}

    sets: dict[tuple[str, str], str] = {}  # (id, property) -> value
    logs: list[str] = []

    def _ident(obj_or_id: Any) -> str:
        if isinstance(obj_or_id, dict):
            return str(obj_or_id.get("id", ""))
        return str(obj_or_id)

    def objects(type: str | None = None) -> list[dict[str, Any]]:
        if type is None:
            return list(nodes)
        wanted = type.strip().lower()
        return [n for n in nodes if str(n.get("type", "")).strip().lower() == wanted]

    def find(name: str, type: str | None = None) -> dict[str, Any] | None:
        wanted = name.strip().lower()
        pool = objects(type)
        exact = [n for n in pool if str(n.get("name", "")).strip().lower() == wanted]
        if exact:
            return exact[0]
        partial = [n for n in pool if wanted in str(n.get("name", "")).lower()]
        return partial[0] if len(partial) == 1 else None

    def field(obj_or_id: Any, property: str) -> str:
        node = by_id.get(_ident(obj_or_id))
        if node is None:
            return ""
        fields: dict[str, Any] = node.get("fields", {})
        value = fields.get(property)
        if value is not None:
            return str(value)
        wanted = property.strip().lower()
        for key, stored in fields.items():
            if key.strip().lower() == wanted:
                return str(stored)
        return ""

    def neighbors(
        obj_or_id: Any, edge_type: str | None = None
    ) -> list[dict[str, Any]]:
        node_id = _ident(obj_or_id)
        wanted = edge_type.strip().lower() if edge_type else None
        found: list[dict[str, Any]] = []
        for edge in edges:
            label = str(edge.get("type", ""))
            if wanted is not None and label.strip().lower() != wanted:
                continue
            if edge.get("source") == node_id:
                other, direction = edge.get("target"), "out"
            elif edge.get("target") == node_id:
                other, direction = edge.get("source"), "in"
            else:
                continue
            node = by_id.get(str(other))
            if node is not None:
                found.append(
                    {"edge": label, "direction": direction, "node": node}
                )
        return found

    def set(obj_or_id: Any, property: str, value: Any) -> None:  # noqa: A001
        if isinstance(value, bool):
            wire = "true" if value else "false"
        elif isinstance(value, (int, float)):
            wire = str(value)
        elif isinstance(value, str):
            wire = value
        else:
            raise TypeError(
                f"set() takes a str, bool, int, or float value; got "
                f"{type(value).__name__}"
            )
        key = (_ident(obj_or_id), str(property))
        if key not in sets and len(sets) >= max_sets:
            raise RuntimeError(
                f"a script may queue at most {max_sets} writes per fire"
            )
        sets[key] = wire

    def log(msg: Any) -> None:
        if len(logs) < _MAX_LOGS:
            logs.append(str(msg)[:_LOG_CHARS])

    trigger = by_id.get(str(payload.get("trigger", "")), {})
    script_globals: dict[str, Any] = {
        "__name__": "__rule_script__",
        "now": payload.get("now", ""),
        "rule_name": payload.get("rule", {}).get("name", ""),
        "trigger": trigger,
        "before": payload.get("before", ""),
        "after": payload.get("after", ""),
        "objects": objects,
        "find": find,
        "field": field,
        "neighbors": neighbors,
        "set": set,
        "log": log,
    }
    code = compile(payload.get("script", ""), _SCRIPT_FILENAME, "exec")
    exec(code, script_globals)  # noqa: S102 -- the sandbox's entire point
    return {
        "sets": [
            {"id": node_id, "property": prop, "value": value}
            for (node_id, prop), value in sets.items()
        ],
        "logs": logs,
    }


def _apply_rlimits() -> None:
    """Lower this process's OWN limits before touching any input.

    Soft AND hard: an unprivileged process can never raise a hard limit
    back, so the script cannot undo them. Each set degrades silently on
    platforms that refuse (the parent's wall-clock kill is the
    universal backstop). NPROC is per-UID and checked at fork -- this
    process already exists, so 1 effectively blocks fork()/subprocess
    from inside the sandbox (kernel/container dependent, hence belt).
    """
    import contextlib
    import resource

    limits = [
        (resource.RLIMIT_CPU, 5),  # seconds; SIGKILL at the hard limit
        (resource.RLIMIT_AS, 256 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, 1024 * 1024),  # pipes are exempt
        (resource.RLIMIT_NOFILE, 16),
        (resource.RLIMIT_NPROC, 1),
    ]
    for kind, value in limits:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(kind, (value, value))


class _CappedSink(io.StringIO):
    """Absorbs script ``print()`` output, discarding past the cap --
    stdout belongs to the effects protocol, and a print bomb must not
    grow memory until the address-space limit fires."""

    def write(self, s: str) -> int:
        if self.tell() < _PRINT_CAP:
            super().write(s[: _PRINT_CAP - self.tell()])
        return len(s)


def _author_traceback(limit_file: str = _SCRIPT_FILENAME) -> str:
    """The traceback with bootstrap frames stripped, so the author sees
    THEIR line numbers first; falls back to the full text."""
    text = traceback.format_exc()
    marker = f'File "{limit_file}"'
    index = text.find(marker)
    if index == -1:
        return text
    return "Traceback (most recent call last):\n  " + text[index:]


def main() -> int:
    _apply_rlimits()
    payload = json.load(sys.stdin)
    real_stdout = sys.stdout
    sys.stdout = _CappedSink()
    try:
        result = execute(payload)
    except Exception:  # noqa: BLE001 -- the protocol boundary: report, exit 1
        print(_author_traceback(), file=sys.stderr)
        return 1
    finally:
        sys.stdout = real_stdout
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
