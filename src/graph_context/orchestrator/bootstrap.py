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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from graph_context import composition
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.composition import TeardownHook
from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.interface.profiles import DomainProfile
from graph_context.orchestrator import modes
from graph_context.orchestrator.channels import ChannelRoute, load_channel_bindings
from graph_context.orchestrator.discord_transport import parse_channel_allowlist
from graph_context.orchestrator.drivers import (
    LLMDriver,
    LLMTurn,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.pipeline import Orchestrator
from graph_context.orchestrator.spaces import SpaceBinding, load_space_bindings
from graph_context.orchestrator.turn_log import TurnLog

logger = logging.getLogger(__name__)

DEFAULT_TURN_LOG = "logs/turns.jsonl"  # relative to the process cwd

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
            "inspects/switches mode; /clear resets conversation memory."
        )
        return driver, model or "claude-code-default", help_line
    raise GraphContextError(
        f"unknown GC_DRIVER {choice!r}; allowed: claude (default), manual"
    )


def build_turn_log() -> TurnLog | None:
    """``GC_TURN_LOG`` resolution -> the turn diary, or None (disabled).

    The value is the JSONL path (default ``logs/turns.jsonl``); ``0`` /
    ``false`` / ``no`` / ``off`` switches the diary off entirely.
    ``GC_TURN_LOG_MAX_BYTES`` caps the file -- past it the oldest
    entries are dropped -- and, like every other config knob, a value
    that isn't a positive integer fails loudly at startup.
    """
    raw_path = os.environ.get("GC_TURN_LOG", DEFAULT_TURN_LOG).strip()
    if raw_path.lower() in {"", "0", "false", "no", "off"}:
        return None
    raw_max = os.environ.get("GC_TURN_LOG_MAX_BYTES", "").strip()
    if not raw_max:
        return TurnLog(raw_path)
    try:
        max_bytes = int(raw_max)
    except ValueError:
        max_bytes = 0
    if max_bytes <= 0:
        raise GraphContextError(
            f"GC_TURN_LOG_MAX_BYTES must be a positive integer, got {raw_max!r}"
        )
    return TurnLog(raw_path, max_bytes=max_bytes)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything a transport loop needs, plus the shutdown hooks."""

    orchestrator: Orchestrator
    profile: DomainProfile
    help_line: str
    teardown: list[TeardownHook]


@dataclass(frozen=True, slots=True)
class ChannelRuntimes:
    """The Discord bot's routing table: every served channel, wired.

    In legacy allowlist mode all channels share one route (one runtime,
    one lock -- today's serialization preserved); with a channels file
    each binding gets its own (ADR 017).
    """

    routes: Mapping[int, ChannelRoute]
    descriptions: Mapping[int, str]  # channel id -> "space=..., profile=..."
    help_line: str
    teardown: list[TeardownHook]


@dataclass(frozen=True, slots=True)
class SpaceRuntimes:
    """The Anytype chat bot's routing table: every served chat, wired.

    Keys are CHAT ids (the inbound message's address); ``spaces`` maps
    each chat back to its space id for stream targets and deep links.
    """

    routes: Mapping[str, ChannelRoute]
    spaces: Mapping[str, str]  # chat id -> space id
    descriptions: Mapping[str, str]  # chat id -> "space=..., profile=..."
    help_line: str
    teardown: list[TeardownHook]


async def _assemble_runtime(
    profile: DomainProfile,
    driver: LLMDriver,
    model_name: str,
    help_line: str,
    turn_log: TurnLog | None,
    *,
    space_id: str | None = None,
    project: str | None = None,
    modes_file: str | None = None,
) -> Runtime:
    """One fully wired runtime: services, provenance, mode registry.

    Everything space-bound multiplies per call -- the journal included
    (a shared journal would attribute one channel's mutations to another
    channel's intent node); the driver and turn log are shared, both
    per-turn stateless. ``modes_file`` overrides ``GC_MODES_FILE`` for
    this runtime (per-channel modes, ADR 017).
    """
    provenance_on = os.environ.get("GC_PROVENANCE", "1").lower() not in {
        "0", "false", "no",
    }
    store_prompt = os.environ.get("GC_STORE_LLM_INPUT", "1").lower() not in {
        "0", "false", "no",
    }
    journal = MutationJournal() if provenance_on else None
    built = await composition.build_runtime(
        profile, journal=journal, space_id=space_id, project=project
    )
    services = built.services
    recorder = (
        IntentRecorder(services.repository, store_prompt=store_prompt)
        if provenance_on else None
    )
    # ADR 015: profile defaults, overlaid by the modes file TOML and by
    # the space's own Activity Mode objects (in-space wins). The same
    # closure re-reads all three sources on every /mode command, so an
    # edit made in Anytype applies without a restart.
    modes_file = modes_file or os.environ.get("GC_MODES_FILE")

    async def reload_registry() -> modes.ModeRegistry:
        return modes.load_registry(
            profile, modes_file, in_space=await built.mode_store.load()
        )

    registry = await reload_registry()  # startup: bad specs fail loudly here
    orchestrator = Orchestrator(
        services=services, driver=driver, profile=profile,
        registry=registry, provenance=recorder, model_name=model_name,
        reload_registry=reload_registry, turn_log=turn_log,
    )
    return Runtime(
        orchestrator=orchestrator, profile=profile,
        help_line=help_line, teardown=built.teardown,
    )


async def build_orchestrator() -> Runtime:
    """Env-driven assembly shared by every transport ``main()``.

    Honors ``GC_PROFILE``, ``GC_PROVENANCE`` (on by default; ``0``
    disables), ``GC_STORE_LLM_INPUT`` (``0`` withholds prompt text from
    intent nodes), ``GC_MODES_FILE``, ``GC_DRIVER``, and ``GC_TURN_LOG``
    / ``GC_TURN_LOG_MAX_BYTES`` (the full-fidelity turn diary; see
    :func:`build_turn_log`). Bad specs and driver config fail loudly
    here, before any loop starts.
    """
    profile = profiles.get_profile(os.environ.get("GC_PROFILE"))
    driver, model_name, help_line = build_driver()
    return await _assemble_runtime(
        profile, driver, model_name, help_line, build_turn_log()
    )


async def build_channel_runtimes() -> ChannelRuntimes:
    """The Discord composition: channel bindings -> per-space runtimes.

    ``GC_CHANNELS_FILE`` set -> one runtime per binding, assembled
    SEQUENTIALLY (concurrent ``ensure_schema`` bursts would just trip the
    live server's ~1 write/s throttle) and failing the whole bot if any
    space fails -- a half-alive bot silently ignoring a channel is the
    worse failure mode. Unset -> exactly today's behavior: the
    ``GC_DISCORD_CHANNELS`` allowlist over one env-configured runtime,
    every channel sharing one route (and so one turn lock). Setting both
    is ambiguous and fails loudly.
    """
    channels_file = os.environ.get("GC_CHANNELS_FILE", "").strip()
    legacy_raw = os.environ.get("GC_DISCORD_CHANNELS")
    if channels_file and legacy_raw:
        raise GraphContextError(
            "both GC_CHANNELS_FILE and GC_DISCORD_CHANNELS are set; the "
            "channels file replaces the allowlist -- unset one"
        )
    driver, model_name, help_line = build_driver()
    turn_log = build_turn_log()

    if not channels_file:
        allowed = parse_channel_allowlist(legacy_raw)
        runtime = await _assemble_runtime(
            profiles.get_profile(os.environ.get("GC_PROFILE")),
            driver, model_name, help_line, turn_log,
        )
        shared = ChannelRoute(orchestrator=runtime.orchestrator)
        space = os.environ.get("ANYTYPE_SPACE_ID", "(env)")
        return ChannelRuntimes(
            routes={cid: shared for cid in allowed},
            descriptions={
                cid: f"space={space}, profile={runtime.profile.name}"
                for cid in allowed
            },
            help_line=help_line,
            teardown=runtime.teardown,
        )

    bindings = load_channel_bindings(channels_file, os.environ.get("GC_PROFILE"))
    routes: dict[int, ChannelRoute] = {}
    descriptions: dict[int, str] = {}
    teardown: list[TeardownHook] = []
    for binding in bindings:
        try:
            runtime = await _assemble_runtime(
                binding.profile, driver, model_name, help_line, turn_log,
                space_id=binding.space_id, project=binding.project,
                modes_file=binding.modes_file,
            )
        except GraphContextError as err:
            await composition.run_teardown(teardown)  # close what already built
            raise GraphContextError(
                f"channel {binding.channel_id} (space {binding.space_id}) "
                f"failed to start: {err}"
            ) from err
        routes[binding.channel_id] = ChannelRoute(orchestrator=runtime.orchestrator)
        descriptions[binding.channel_id] = (
            f"space={binding.space_id}, profile={binding.profile.name}"
        )
        teardown.extend(runtime.teardown)
        logger.info(
            "channel %d -> space %s (profile=%s)",
            binding.channel_id, binding.space_id, binding.profile.name,
        )
    return ChannelRuntimes(
        routes=routes, descriptions=descriptions,
        help_line=help_line, teardown=teardown,
    )


async def build_space_runtimes(
    resolve_chat_id: Callable[[SpaceBinding], Awaitable[str]],
) -> SpaceRuntimes:
    """The Anytype chat composition: space bindings -> per-space runtimes.

    Same posture as :func:`build_channel_runtimes`: SEQUENTIAL assembly
    (concurrent ``ensure_schema`` bursts would trip a throttled server),
    failing the whole bot if any space fails. ``resolve_chat_id`` is
    injected by the composition root -- it owns the chat client -- so this
    module stays free of infrastructure. ``GC_SPACES_FILE`` unset fails
    loudly: a chat bot serving nowhere is a misconfiguration, not a
    default.
    """
    spaces_file = os.environ.get("GC_SPACES_FILE", "").strip()
    if not spaces_file:
        raise GraphContextError(
            "GC_SPACES_FILE is not set; the Anytype chat transport needs "
            '[spaces."<space-id>"] bindings (see spaces.toml at the repo root)'
        )
    bindings = load_space_bindings(spaces_file, os.environ.get("GC_PROFILE"))
    driver, model_name, help_line = build_driver()
    turn_log = build_turn_log()
    routes: dict[str, ChannelRoute] = {}
    spaces: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    teardown: list[TeardownHook] = []
    for binding in bindings:
        try:
            chat_id = await resolve_chat_id(binding)
            runtime = await _assemble_runtime(
                binding.profile, driver, model_name, help_line, turn_log,
                space_id=binding.space_id, project=binding.project,
                modes_file=binding.modes_file,
            )
        except GraphContextError as err:
            await composition.run_teardown(teardown)  # close what already built
            raise GraphContextError(
                f"space {binding.space_id} failed to start: {err}"
            ) from err
        routes[chat_id] = ChannelRoute(orchestrator=runtime.orchestrator)
        spaces[chat_id] = binding.space_id
        descriptions[chat_id] = (
            f"space={binding.space_id}, profile={binding.profile.name}"
        )
        teardown.extend(runtime.teardown)
        logger.info(
            "chat %s -> space %s (profile=%s)",
            chat_id, binding.space_id, binding.profile.name,
        )
    return SpaceRuntimes(
        routes=routes, spaces=spaces, descriptions=descriptions,
        help_line=help_line, teardown=teardown,
    )
