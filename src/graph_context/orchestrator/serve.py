"""The everything server: every configured transport in one process.

Launches, in one asyncio loop:
  - the Anytype chat bot (always -- GC_SPACES_FILE is required),
  - the Discord bot (only when its token file has content AND at least
    one channel is bound; the devcontainer composes DISCORD_BOT_TOKEN_FILE
    and GC_CHANNELS_FILE unconditionally, so an EMPTY secret file or a
    zero-table channels file is the sanctioned "no Discord" state),
  - the inspection server (a daemon thread; the eval dashboard + the live
    turn-log viewer; skipped when GC_TURN_LOG or GC_LOG_VIEWER_PORT is
    off; GC_EVAL_ROOT points it at the eval artifacts).

Failure semantics are the TaskGroup's: one transport's unhandled crash
takes the whole process down loudly -- a half-alive server silently
missing a transport is the worse failure mode; restarts belong to the
supervisor. Each bot keeps its own ``finally`` teardown, so Ctrl-C here
behaves exactly like Ctrl-C on the standalone entries, which remain
launchable on their own (this module only composes their ``run()``s).

Run:  python -m graph_context.orchestrator.serve
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from graph_context.orchestrator import anytype_chat_bot, discord_bot, inspect_server
from graph_context.orchestrator.turn_log import turn_log_path

logger = logging.getLogger(__name__)


def _start_viewer() -> ThreadingHTTPServer | None:
    """The inspection server in a daemon thread, or None when switched off.

    ``serve_forever`` blocks and cannot be cancelled from the loop, so it
    lives in a plain daemon thread (NOT asyncio.to_thread) and ``run()``
    stops it via ``shutdown()`` in its ``finally``.
    """
    log = turn_log_path()
    if log is None:
        logger.info("inspection server: not starting (GC_TURN_LOG is off)")
        return None
    settings = inspect_server.viewer_settings()
    if settings is None:
        logger.info("inspection server: not starting (GC_LOG_VIEWER_PORT is off)")
        return None
    host, port = settings
    server = inspect_server.create_server(
        host, port, Path(log), inspect_server.eval_root_setting()
    )
    threading.Thread(
        target=server.serve_forever, daemon=True, name="inspection-server"
    ).start()
    logger.info("inspection server: http://%s:%d/ (tailing %s)", host, port, log)
    return server


async def run() -> None:
    viewer = _start_viewer()
    try:
        # The viewer is already answering while the bots bootstrap.
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(anytype_chat_bot.run())
            if discord_bot.is_configured():
                task_group.create_task(discord_bot.run())
            else:
                logger.info(
                    "discord: not starting the bot (empty token file or no "
                    "channels bound)"
                )
    finally:
        if viewer is not None:
            viewer.shutdown()
            viewer.server_close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run()


if __name__ == "__main__":
    # Ctrl-C is the expected way down; every teardown already ran.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
