"""Console logging setup shared by every entry point.

One knob: ``GC_LOG_LEVEL`` (default ``INFO``; any standard level name).
httpx logs every request at INFO -- useful when debugging the wire,
noise the rest of the time -- so unless the deployment asks for DEBUG
the HTTP client loggers are capped at WARNING. The INFO channel stays
for events worth a human's glance: turns, scheduled fires, prose saves,
resyncs.
"""

from __future__ import annotations

import logging
import os

#: Loggers whose per-request chatter belongs to DEBUG runs only.
_HTTP_LOGGERS = ("httpx", "httpcore")


def configure_logging() -> None:
    """``basicConfig`` at ``GC_LOG_LEVEL``, HTTP request chatter demoted.

    Idempotent like ``basicConfig`` itself; an unknown level name warns
    and falls back to INFO rather than refusing to start.
    """
    name = os.environ.get("GC_LOG_LEVEL", "").strip().upper() or "INFO"
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        level = logging.INFO
        logging.basicConfig(level=level)
        logging.getLogger(__name__).warning(
            "unknown GC_LOG_LEVEL %r; using INFO", name
        )
    else:
        logging.basicConfig(level=level)
    for http_logger in _HTTP_LOGGERS:
        logging.getLogger(http_logger).setLevel(
            logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        )
