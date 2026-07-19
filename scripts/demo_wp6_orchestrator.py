"""WP6 acceptance demo: switch modes; authoring mode cannot mutate.

Drives the orchestrator pipeline with a scripted fake LLM against the
in-memory backend, through the SHARED service builder (the same wiring the
MCP server uses). Watch for the [error] turn: in authoring mode the
mutation attempt never runs -- the binding lacks the tool.

Run:  PYTHONPATH=src python scripts/demo_wp6_orchestrator.py
"""

import asyncio
import os

from graph_context import composition
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver, ToolCall
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator


async def main() -> None:
    os.environ["GC_BACKEND"] = "memory"
    profile = get_profile("fiction")
    built = await composition.build_runtime(profile)
    services, teardown = built.services, built.teardown
    orchestrator = Orchestrator(
        services=services,
        profile=profile,
        # ADR 035: the memory runtime's stores come pre-seeded with the
        # profile's starter modes -- load the registry the way the bot does.
        registry=load_registry(
            in_space=await built.mode_store.load(),
            space_context=await built.space_context_store.load(),
        ),
        driver=ScriptedDriver([
            # Turn 1 (world_modeling): create, then reply.
            LLMTurn(tool_calls=(ToolCall("create_node", {
                "type": "Character", "name": "Mira",
                "summary": "Exiled siege engineer.", "icon": "⚙️",
            }),)),
            LLMTurn(reply="Mira exists now."),
            # Turn 2 (authoring): the model TRIES to mutate, then reads.
            LLMTurn(tool_calls=(ToolCall("update_node", {
                "node_id": "Mira", "summary": "Rewritten!",
            }),)),
            LLMTurn(tool_calls=(ToolCall("get_node", {"node_id": "Mira"}),)),
            LLMTurn(reply="Couldn't touch her record; here is what I read."),
        ]),
    )

    async def turn(text: str) -> None:
        print(f"\n>>> {text}")
        for event in await orchestrator.handle_message("demo", "you", text):
            prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
            print(f"{prefix}{event.text}")

    await turn("Add Mira to the world.")
    await turn("/mode authoring")
    await turn("Punch up Mira's summary, then remind me who she is.")
    summary = services.repository.graph.resolve("Mira").summary
    print(f"\nstore truth: Mira's summary is still {summary!r} (unmutated)")
    await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
