"""Back-compat shim: the turn-log viewer grew into ``inspect_server``.

Old report.md footers and docs say ``python -m
graph_context.orchestrator.turn_log_server --log ...``; keep that
spelling working by re-exporting the real module's surface.
"""

from __future__ import annotations

from graph_context.orchestrator.inspect_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Handler,
    _read_new,
    create_server,
    main,
    viewer_settings,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Handler",
    "_read_new",
    "create_server",
    "main",
    "viewer_settings",
]

if __name__ == "__main__":
    main()
