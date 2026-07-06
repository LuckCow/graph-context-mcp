"""WP6 acceptance with the REAL driver: the model mutates in world_modeling
and cannot in authoring -- the binding boundary, not model manners.

LIVE: talks to the model on your Claude subscription (the devcontainer's
persisted ``claude login``, or ``CLAUDE_CODE_OAUTH_TOKEN``). In-memory
backend; nothing persists. ``GC_DRIVER_MODEL`` picks the model (unset =
your account's CLI default).

Run:  PYTHONPATH=src python scripts/demo_claude_driver.py
"""

import asyncio
import os

from graph_context import composition
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.claude_driver import ClaudeAgentDriver
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator


async def main() -> None:
    os.environ["GC_BACKEND"] = "memory"
    profile = get_profile("fiction")
    services, teardown = await composition.build_runtime(profile)
    orchestrator = Orchestrator(
        services=services,
        profile=profile,
        registry=load_registry(profile),
        driver=ClaudeAgentDriver(model=os.environ.get("GC_DRIVER_MODEL") or None),
        model_name=os.environ.get("GC_DRIVER_MODEL", "claude-code-default"),
    )

    async def turn(text: str) -> None:
        print(f"\n>>> {text}")
        for event in await orchestrator.handle_message("demo", "you", text):
            prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
            print(f"{prefix}{event.text}")

    await turn(
        "Create a Character named exactly 'Mira', summary "
        "'Exiled siege engineer of Brakk.' Then confirm briefly."
    )
    created = services.repository.graph.node_count()
    print(f"\nstore truth: {created} node(s) after the world_modeling turn")

    await turn("/mode authoring")
    await turn(
        "Add a Location named 'Castle Brakk' to the graph. "
        "If you cannot, say so briefly."
    )
    after = services.repository.graph.node_count()
    print(
        f"\nstore truth: {after} node(s) after the authoring turn -- "
        + ("UNCHANGED (the binding held)" if after == created else "MUTATED (bug!)")
    )
    await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
