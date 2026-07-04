"""WP12 acceptance demo: Record Procedure is a configuration, not a fork.

The assistant profile drives the SAME pipeline fiction uses -- only the
data changed: mode specs with their own goal prompts, a capture policy
whose artifact is a native `procedure` node, and a real-date timeline.
The scripted "model" organizes a task, then records a procedure; it never
calls a capture tool, yet the harness leaves behind a first-class
procedure node with references and the full intent chain.

Run:  PYTHONPATH=src python scripts/demo_wp12_assistant.py
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

PROCEDURE = (
    "Deploy notes for Staging server:\n"
    "1. Announce the deploy in the team channel.\n"
    "2. `git pull` on the Staging server, then `make build` (about 4 min).\n"
    "3. Run the smoke suite; if step 3 fails, roll back with `make revert`\n"
    "   BEFORE restarting anything -- restart order matters.\n"
    "4. Restart workers first, web second.\n"
)


async def main() -> None:
    os.environ["GC_BACKEND"] = "memory"
    profile = get_profile("assistant")
    journal = MutationJournal()
    services, teardown = await composition.build_runtime(profile, journal=journal)
    registry = load_registry(profile)
    orchestrator = Orchestrator(
        services=services,
        profile=profile,
        registry=registry,
        provenance=IntentRecorder(services.repository),
        model_name="scripted-demo",
        driver=ScriptedDriver([
            # Turn 1 (organizing -- the assistant default): set up the world.
            LLMTurn(tool_calls=(
                ToolCall("create_node", {
                    "type": "Technology", "name": "Staging server",
                    "summary": "The staging deploy target.", "icon": "🖥️",
                }),
                ToolCall("create_node", {
                    "type": "Task", "name": "Ship v2 to staging",
                    "summary": "Deploy and verify the v2 build.", "icon": "☑️",
                    "links": [{"edge_type": "runs_on", "other": "Staging server"}],
                    "create_missing_relations": True,
                }),
            )),
            LLMTurn(reply="Task and server are in the graph."),
            # Turn 2 (record_procedure): pure notation -- no tool calls.
            LLMTurn(reply=PROCEDURE),
        ]),
    )

    async def turn(text: str) -> None:
        print(f"\n>>> {text}")
        for event in await orchestrator.handle_message("demo", "you", text):
            prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
            print(f"{prefix}{event.text[:240]}")

    await turn("Track my staging deploy work.")
    await turn("/mode record_procedure")
    print(f"\n[goal prompt the driver receives]\n{registry.get('record_procedure').goal}")
    await turn("I'm deploying v2 to staging, take this down as I go...")

    graph = services.repository.graph
    procedures = [n for n in graph.nodes() if n.type_key == "procedure"]
    intents = [n for n in graph.nodes() if n.role is Role.INTENT]
    assert procedures, "the capture policy should have produced a procedure node"
    proc = procedures[0]
    refs = [graph.node(e.target).name for e in graph.edges(proc.id)
            if e.type == "references"]
    print(f"\nharness left behind: procedure {proc.name!r} "
          f"(first-class: role={proc.role}) referencing {refs}; "
          f"{len(intents)} intent record(s).")

    print("\n### get_node on the captured procedure")
    out = await tools.get_node_tool(services, node_id=proc.id)
    print(out.split("\n", 1)[1])
    await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
