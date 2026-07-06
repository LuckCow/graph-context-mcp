"""Shared composition wiring for orchestrator transports (WP6/WP8).

Every transport -- CLI, Discord, and whatever WP8 adds next -- builds the
same runtime (profile, provenance, services, mode registry, driver) and
differs only in its message loop, so that wiring lives here once. Like
the transport ``__main__`` modules it serves, this is part of the
orchestrator's composition root: it may import infrastructure via the
shared builder.

Driver selection (``GC_DRIVER``): ``claude`` (default) is the real model
-- :class:`~graph_context.orchestrator.claude_driver.ClaudeAgentDriver`
over the Claude Code CLI, billing the user's subscription
(``GC_DRIVER_MODEL`` / ``GC_DRIVER_EFFORT`` tune it; unset = the
account's CLI defaults). ``manual`` is the keyboard stand-in
(:class:`ManualDriver`): ``/tool <name> {json}`` issues one tool call
through the ACTIVE MODE's binding, which makes the mode boundary
tangible from any transport -- switch with ``/mode authoring`` and
``/tool create_node ...`` comes back "not available", because the
binding lacks it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graph_context import composition
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.composition import TeardownHook
from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.interface.profiles import DomainProfile
from graph_context.orchestrator import modes
from graph_context.orchestrator.drivers import (
    LLMDriver,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.pipeline import Orchestrator

logger = logging.getLogger(__name__)

MANUAL_HELP = (
    "you are the model (GC_DRIVER=manual): /tool <name> {json-args} runs a "
    "tool through the active mode's binding; /mode [name] inspects/switches "
    "mode."
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
                return LLMTurn(reply=f"usage: /tool <name> {{json}}. {MANUAL_HELP}")
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                return LLMTurn(reply=f"arguments must be JSON, got: {raw_args!r}")
            return LLMTurn(tool_calls=(ToolCall(name, arguments),))
        return LLMTurn(reply=MANUAL_HELP)


def build_driver() -> tuple[LLMDriver, str, str]:
    """GC_DRIVER resolution -> (driver, attribution model name, help line).

    Unknown values and a missing claude-agent-sdk fail loudly at startup,
    like every other config error (specs, GC_EMBEDDER).
    """
    choice = os.environ.get("GC_DRIVER", "claude").strip().lower()
    if choice == "manual":
        return ManualDriver(), "manual", MANUAL_HELP
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
            "inspects/switches mode."
        )
        return driver, model or "claude-code-default", help_line
    raise GraphContextError(
        f"unknown GC_DRIVER {choice!r}; allowed: claude (default), manual"
    )


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything a transport loop needs, plus the shutdown hooks."""

    orchestrator: Orchestrator
    profile: DomainProfile
    help_line: str
    teardown: list[TeardownHook]


async def build_orchestrator() -> Runtime:
    """Env-driven assembly shared by every transport ``main()``.

    Honors ``GC_PROFILE``, ``GC_PROVENANCE`` (on by default; ``0``
    disables), ``GC_STORE_LLM_INPUT`` (``0`` withholds prompt text from
    intent nodes), ``GC_MODES_FILE``, and ``GC_DRIVER``. Bad specs and
    driver config fail loudly here, before any loop starts.
    """
    profile = profiles.get_profile(os.environ.get("GC_PROFILE"))
    provenance_on = os.environ.get("GC_PROVENANCE", "1").lower() not in {
        "0", "false", "no",
    }
    store_prompt = os.environ.get("GC_STORE_LLM_INPUT", "1").lower() not in {
        "0", "false", "no",
    }
    journal = MutationJournal() if provenance_on else None
    built = await composition.build_runtime(profile, journal=journal)
    services = built.services
    recorder = (
        IntentRecorder(services.repository, store_prompt=store_prompt)
        if provenance_on else None
    )
    # ADR 015: profile defaults, overlaid by the optional GC_MODES_FILE
    # TOML and by the space's own Activity Mode objects (in-space wins).
    # The same closure re-reads all three sources on every /mode command,
    # so an edit made in Anytype applies without a restart.
    modes_file = os.environ.get("GC_MODES_FILE")

    async def reload_registry() -> modes.ModeRegistry:
        return modes.load_registry(
            profile, modes_file, in_space=await built.mode_store.load()
        )

    registry = await reload_registry()  # startup: bad specs fail loudly here
    driver, model_name, help_line = build_driver()
    orchestrator = Orchestrator(
        services=services, driver=driver, profile=profile,
        registry=registry, provenance=recorder, model_name=model_name,
        reload_registry=reload_registry,
    )
    return Runtime(
        orchestrator=orchestrator, profile=profile,
        help_line=help_line, teardown=built.teardown,
    )
