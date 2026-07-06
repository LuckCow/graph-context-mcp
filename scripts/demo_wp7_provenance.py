"""WP7 acceptance demo: provenance is automatic; the model volunteers nothing.

A scripted "model" builds a node, then writes a scene in authoring mode.
It never calls record_prose and never reports what it touched -- yet the
harness leaves behind: one intent node per mutating turn (prompt, tool
trace, attribution, `intent` edges to every touched node), an
auto-captured Prose artifact with `references` to the mentioned cast, and
a get_node(include_provenance=1) audit trail.

Run:  PYTHONPATH=src python scripts/demo_wp7_provenance.py
"""

import asyncio
import os

from graph_context import composition
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.domain.schema import Role
from graph_context.interface import tools
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver, ToolCall
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator

SCENE = (
    "Mira counted the cracks in the vault line the way other people count "
    "sins. The Undercroft answered her footfalls twice, the second echo "
    "always late, always lower, as if the stone kept its own ledger. She "
    "pressed a palm to the coldest block and listened for the siege that "
    "was still living in it."
)


async def main() -> None:
    os.environ["GC_BACKEND"] = "memory"
    profile = get_profile("fiction")
    journal = MutationJournal()
    built = await composition.build_runtime(profile, journal=journal)
    services, teardown = built.services, built.teardown
    orchestrator = Orchestrator(
        services=services,
        profile=profile,
        registry=load_registry(profile),
        provenance=IntentRecorder(services.repository),
        model_name="scripted-demo",
        driver=ScriptedDriver([
            # Turn 1 (world_modeling): build the scene's pieces.
            LLMTurn(tool_calls=(
                ToolCall("create_node", {
                    "type": "Character", "name": "Mira",
                    "summary": "Exiled siege engineer.", "icon": "⚙️",
                }),
                ToolCall("create_node", {
                    "type": "Location", "name": "The Undercroft",
                    "summary": "Vaults beneath Brakk.", "icon": "🕳️",
                }),
            )),
            LLMTurn(reply="Mira and the Undercroft exist."),
            # Turn 2 (authoring): just prose -- no tool calls, no record_prose.
            LLMTurn(reply=SCENE),
        ]),
    )

    async def turn(text: str) -> None:
        print(f"\n>>> {text}")
        for event in await orchestrator.handle_message("demo", "you", text):
            prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
            print(f"{prefix}{event.text[:200]}")

    await turn("Set up Mira and the Undercroft.")
    await turn("/mode authoring")
    await turn("Write Mira alone in the Undercroft.")

    graph = services.repository.graph
    intents = [n for n in graph.nodes() if n.role is Role.INTENT]
    prose = [n for n in graph.nodes() if n.role is Role.CAPTURE]
    print(f"\nharness left behind: {len(intents)} intent node(s), "
          f"{len(prose)} captured artifact(s) -- the model called no capture tool.")
    for intent in intents:
        print(f"  {intent.name}")

    print("\n### get_node(Mira, include_provenance=1) — the audit view")
    out = await tools.get_node_tool(
        services, node_id="Mira", include_provenance=1
    )
    print(out.split("\n", 1)[1])
    await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
