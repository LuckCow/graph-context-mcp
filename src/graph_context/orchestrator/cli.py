"""The orchestrator's CLI: the first, thinnest transport adapter (WP6).

A keyboard loop over ``Orchestrator.handle_message`` -- exactly the shape
the WP8 chat transports take (see ``discord_bot``). All runtime assembly
lives in the shared ``bootstrap`` module; this file is only the loop and
the terminal rendering.

Run:  GC_BACKEND=memory PYTHONPATH=src python -m graph_context.orchestrator.cli
"""

from __future__ import annotations

import asyncio
import logging

from graph_context import composition
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.pipeline import ReplyEvent


def _print_event(event: ReplyEvent) -> None:
    prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
    print(f"{prefix}{event.text}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runtime = await bootstrap.build_orchestrator()
    print(
        f"graph-context orchestrator (profile={runtime.profile.name}). "
        f"{runtime.help_line} /quit exits."
    )
    try:
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() in {"/quit", "/exit"}:
                break
            if not line.strip():
                continue
            for event in await runtime.orchestrator.handle_message(
                "cli", "local", line
            ):
                _print_event(event)
    finally:
        await composition.run_teardown(runtime.teardown)


if __name__ == "__main__":
    asyncio.run(main())
