"""The orchestrator's CLI: composition root + first transport adapter (WP6).

A thin loop over ``Orchestrator.handle_message`` -- exactly the shape the
WP8 chat transports will take. This is the only orchestrator module allowed
to import infrastructure (via the shared builder; import-linter-enforced).

The driver is selected by ``GC_DRIVER``: ``claude`` (default) is the real
model -- :class:`~graph_context.orchestrator.claude_driver.ClaudeAgentDriver`
over the Claude Code CLI, billing the user's subscription
(``GC_DRIVER_MODEL`` / ``GC_DRIVER_EFFORT`` tune it; unset = the account's
CLI defaults). ``manual`` keeps the keyboard stand-in: ``/tool <name>
{json}`` issues one tool call through the ACTIVE MODE's binding -- which
makes the mode boundary tangible at the keyboard: switch with ``/mode
authoring`` and ``/tool create_node ...`` comes back "not available",
because the binding lacks it.

Run:  GC_BACKEND=memory PYTHONPATH=src python -m graph_context.orchestrator.cli
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence

from graph_context import composition
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.orchestrator import modes
from graph_context.orchestrator.drivers import (
    LLMDriver,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.pipeline import Orchestrator, ReplyEvent

logger = logging.getLogger(__name__)

_HELP = (
    "you are the model (GC_DRIVER=manual): /tool <name> {json-args} runs a "
    "tool through the active mode's binding; /mode [name] inspects/switches "
    "mode; /quit exits."
)


class ManualDriver:
    """A keyboard-powered stand-in for the LLM driver.

    One user message -> at most one tool call -> the tool's output becomes
    the reply. Deliberately minimal: the pipeline, bindings, and mode
    boundary are the thing being exercised, not this driver.
    """

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
    ) -> LLMTurn:
        last = transcript[-1]
        if last.kind == "tool":
            return LLMTurn(reply=last.text)
        text = last.text.strip()
        if text.startswith("/tool"):
            rest = text.removeprefix("/tool").strip()
            name, _, raw_args = rest.partition(" ")
            if not name:
                return LLMTurn(reply=f"usage: /tool <name> {{json}}. {_HELP}")
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                return LLMTurn(reply=f"arguments must be JSON, got: {raw_args!r}")
            return LLMTurn(tool_calls=(ToolCall(name, arguments),))
        return LLMTurn(reply=_HELP)


def _print_event(event: ReplyEvent) -> None:
    prefix = {"reply": "", "notice": "[notice] ", "error": "[error] "}[event.kind]
    print(f"{prefix}{event.text}")


def _build_driver() -> tuple[LLMDriver, str, str]:
    """GC_DRIVER resolution -> (driver, attribution model name, help line).

    Unknown values and a missing claude-agent-sdk fail loudly at startup,
    like every other config error (specs, GC_EMBEDDER).
    """
    choice = os.environ.get("GC_DRIVER", "claude").strip().lower()
    if choice == "manual":
        return ManualDriver(), "manual", _HELP
    if choice == "claude":
        try:
            from graph_context.orchestrator.claude_driver import ClaudeAgentDriver
        except ImportError as err:
            raise GraphContextError(
                "GC_DRIVER=claude needs claude-agent-sdk (a container rebuild "
                "installs the [orchestrator] extra); GC_DRIVER=manual runs "
                "without it"
            ) from err
        model = os.environ.get("GC_DRIVER_MODEL", "").strip() or None
        effort = os.environ.get("GC_DRIVER_EFFORT", "").strip().lower() or None
        allowed_efforts = ("low", "medium", "high", "xhigh", "max")
        if effort is not None and effort not in allowed_efforts:
            raise GraphContextError(
                f"unknown GC_DRIVER_EFFORT {effort!r}; allowed: "
                f"{', '.join(allowed_efforts)}"
            )
        driver = ClaudeAgentDriver(model=model, effort=effort)  # type: ignore[arg-type]
        help_line = (
            "talking to the model on your Claude subscription; /mode [name] "
            "inspects/switches mode; /quit exits."
        )
        return driver, model or "claude-code-default", help_line
    raise GraphContextError(
        f"unknown GC_DRIVER {choice!r}; allowed: claude (default), manual"
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    profile = profiles.get_profile(os.environ.get("GC_PROFILE"))
    # WP7 provenance subsystem: on by default; GC_PROVENANCE=0 disables.
    # GC_STORE_LLM_INPUT=0 withholds prompt text from intent nodes.
    provenance_on = os.environ.get("GC_PROVENANCE", "1").lower() not in {
        "0", "false", "no",
    }
    store_prompt = os.environ.get("GC_STORE_LLM_INPUT", "1").lower() not in {
        "0", "false", "no",
    }
    journal = MutationJournal() if provenance_on else None
    services, teardown = await composition.build_runtime(profile, journal=journal)
    recorder = (
        IntentRecorder(services.repository, store_prompt=store_prompt)
        if provenance_on else None
    )
    # ADR 015: profile defaults + optional GC_MODES_FILE (TOML) overlay;
    # bad specs fail loudly here, before the loop starts.
    registry = modes.load_registry(profile, os.environ.get("GC_MODES_FILE"))
    driver, model_name, help_line = _build_driver()
    orchestrator = Orchestrator(
        services=services, driver=driver, profile=profile,
        registry=registry, provenance=recorder, model_name=model_name,
    )
    print(f"graph-context orchestrator (profile={profile.name}). {help_line}")
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
            for event in await orchestrator.handle_message("cli", "local", line):
                _print_event(event)
    finally:
        await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
