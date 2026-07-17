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
from dataclasses import dataclass, field

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
    plain_transcript,
)
from graph_context.orchestrator.pipeline import Orchestrator
from graph_context.orchestrator.spaces import (
    SpaceBinding,
    load_space_bindings,
    served_chat_ids,
)
from graph_context.orchestrator.turn_log import OFF_VALUES, TurnLog, turn_log_path

logger = logging.getLogger(__name__)


def _knob_on(env: str) -> bool:
    """Default-on boolean knob: any repo-wide off spelling disables it."""
    return os.environ.get(env, "1").strip().lower() not in OFF_VALUES


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

    def system_prompt(self, goal: str) -> str:
        return goal  # no model behind this driver; the goal stands in

    def render_prompt(self, transcript: Sequence[TranscriptEvent]) -> str:
        return plain_transcript(transcript)  # ditto

    async def decide(
        self,
        transcript: Sequence[TranscriptEvent],
        tools: Mapping[str, str],
        goal: str = "",
        *,
        web_search: bool = False,
        model: str = "",
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


# The selection names are vendor-namespaced (WHO PAYS, not which SDK):
# ``anthropic_subscription`` is the Claude plan (claude-agent-sdk over the
# CLI), ``anthropic_api`` is Anthropic API credits (the anthropic SDK).
# The namespace leaves room for other vendors later (openai_api, ...). The
# original SDK-flavored names stay accepted so existing env files keep
# working.
_DRIVER_ALIASES = {
    "claude": "anthropic_subscription",
    "subscription": "anthropic_subscription",
    "anthropic": "anthropic_api",
    "api": "anthropic_api",
}


def build_driver() -> tuple[LLMDriver, str, str]:
    """GC_DRIVER resolution -> (driver, attribution model name, help line).

    ``anthropic_subscription`` (default) | ``anthropic_api`` | ``manual``.
    Unknown values and a missing SDK fail loudly at startup, like every
    other config error (specs, GC_EMBEDDER).
    """
    raw = os.environ.get("GC_DRIVER", "anthropic_subscription").strip().lower()
    choice = _DRIVER_ALIASES.get(raw, raw)
    if choice != raw:
        logger.info("GC_DRIVER=%s is a legacy alias for %s", raw, choice)
    if choice == "manual":
        return ManualDriver(), "manual", MANUAL_HELP
    model = os.environ.get("GC_DRIVER_MODEL", "").strip() or None
    effort = os.environ.get("GC_DRIVER_EFFORT", "").strip().lower() or None
    allowed_efforts = ("low", "medium", "high", "xhigh", "max")
    if effort is not None and effort not in allowed_efforts:
        raise GraphContextError(
            f"unknown GC_DRIVER_EFFORT {effort!r}; allowed: "
            f"{', '.join(allowed_efforts)}"
        )
    if choice == "anthropic_subscription":
        try:
            from graph_context.orchestrator.claude_driver import ClaudeAgentDriver
        except ImportError as err:
            raise GraphContextError(
                "GC_DRIVER=anthropic_subscription needs claude-agent-sdk (a "
                "container rebuild installs the [orchestrator] extra); "
                "GC_DRIVER=manual runs without it"
            ) from err
        driver = ClaudeAgentDriver(model=model, effort=effort)  # type: ignore[arg-type]
        help_line = (
            "talking to the model on your Claude subscription; /mode [name] "
            "inspects/switches mode; /clear resets conversation memory."
        )
        return driver, model or "claude-code-default", help_line
    if choice == "anthropic_api":
        try:
            from graph_context.orchestrator.anthropic_driver import (
                DEFAULT_MODEL,
                AnthropicDriver,
            )
        except ImportError as err:
            raise GraphContextError(
                "GC_DRIVER=anthropic_api needs the anthropic SDK (the "
                "[anthropic] extra; a container rebuild installs it)"
            ) from err
        # The billing switch must be a conscious choice: no key, no driver.
        # The SDK would otherwise silently fall back to an `ant auth login`
        # OAuth profile, which hides WHO is paying.
        if not (
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        ):
            raise GraphContextError(
                "GC_DRIVER=anthropic_api bills API credits, not the Claude "
                "subscription -- set ANTHROPIC_API_KEY to opt in, or use "
                "GC_DRIVER=anthropic_subscription for the plan-billed path"
            )
        logger.warning(
            "GC_DRIVER=anthropic_api: model calls bill API credits, "
            "not the Claude subscription"
        )
        anthropic_driver = AnthropicDriver(
            model=model or DEFAULT_MODEL, effort=effort
        )
        help_line = (
            "talking to the model over the Anthropic API (bills API "
            "credits); /mode [name] inspects/switches mode; /clear resets "
            "conversation memory."
        )
        return anthropic_driver, model or DEFAULT_MODEL, help_line
    raise GraphContextError(
        f"unknown GC_DRIVER {raw!r}; allowed: anthropic_subscription "
        "(default), anthropic_api, manual"
    )


def build_turn_log() -> TurnLog | None:
    """``GC_TURN_LOG`` resolution -> the turn diary, or None (disabled).

    The value is the JSONL path (default ``logs/turns.jsonl``); ``0`` /
    ``false`` / ``no`` / ``off`` switches the diary off entirely.
    ``GC_TURN_LOG_MAX_BYTES`` caps the file -- past it the oldest
    entries are dropped -- and, like every other config knob, a value
    that isn't a positive integer fails loudly at startup.
    """
    raw_path = turn_log_path()
    if raw_path is None:
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
    """Everything a transport loop needs, plus the shutdown hooks.

    ``session_labels`` is the runtime's shared mutable key->name map
    (WP8): fill it before a session's first turn so its Anytype node
    gets a legible title (e.g. the chat's display name).
    """

    orchestrator: Orchestrator
    profile: DomainProfile
    help_line: str
    teardown: list[TeardownHook]
    session_labels: dict[str, str]


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

    WP8: all chats of one space share ONE route (one runtime, one turn
    lock, one repository) and each gets its own keyed session. The maps
    are MUTABLE and shared with the turn handler (aliased, not copied), so
    the live-discovery watcher can register a new chat with
    :func:`register_chat` and the handler sees it immediately.
    ``space_routes``/``space_bindings``/``session_labels`` are what the
    watcher needs to serve a newly-created chat against the right runtime.
    """

    routes: dict[str, ChannelRoute]
    spaces: dict[str, str]  # chat id -> space id
    descriptions: dict[str, str]  # chat id -> "space=..., profile=..."
    help_line: str
    teardown: list[TeardownHook]
    space_routes: Mapping[str, ChannelRoute]  # space id -> its shared route
    space_bindings: Mapping[str, SpaceBinding]  # space id -> binding
    session_labels: Mapping[str, dict[str, str]]  # space id -> label sink
    # chat id -> its current name as last listed (WP21: the auto-titler's
    # untitled test reads it; the rescan watcher keeps it fresh).
    chat_names: dict[str, str] = field(default_factory=dict)


def register_chat(
    runtimes: SpaceRuntimes, space_id: str, chat_id: str, name: str
) -> None:
    """Wire one chat to its space's shared runtime (startup + discovery).

    The single place the routing maps grow: startup calls it per enumerated
    chat, the watcher per newly-discovered one. Because the maps are the
    same objects the handler holds, an addition here is live at once.
    """
    binding = runtimes.space_bindings[space_id]
    runtimes.routes[chat_id] = runtimes.space_routes[space_id]
    runtimes.spaces[chat_id] = space_id
    runtimes.chat_names[chat_id] = name.strip()  # "" = untitled (WP21)
    label = name.strip() or chat_id
    runtimes.descriptions[chat_id] = (
        f"{label} (space={space_id}, profile={binding.profile.name})"
    )
    runtimes.session_labels[space_id][f"anytype:{chat_id}"] = label


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
    default_mode: str | None = None,
) -> Runtime:
    """One fully wired runtime: services, provenance, mode registry.

    Everything space-bound multiplies per call -- the journal included
    (a shared journal would attribute one channel's mutations to another
    channel's intent node); the driver and turn log are shared, both
    per-turn stateless. ``modes_file`` overrides ``GC_MODES_FILE`` for
    this runtime (per-channel modes, ADR 017); ``default_mode`` (WP21)
    overrides the profile's default for sessions with no persisted mode
    -- re-applied on every /mode reload, so it survives registry
    refreshes.
    """
    provenance_on = _knob_on("GC_PROVENANCE")
    store_prompt = _knob_on("GC_STORE_LLM_INPUT")
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
            profile, modes_file, in_space=await built.mode_store.load(),
            default=default_mode,
        )

    registry = await reload_registry()  # startup: bad specs fail loudly here
    orchestrator = Orchestrator(
        services=services, driver=driver, profile=profile,
        registry=registry, provenance=recorder, model_name=model_name,
        reload_registry=reload_registry, turn_log=turn_log,
        services_for=built.services_for,  # WP8: per-session-key Services
    )
    return Runtime(
        orchestrator=orchestrator, profile=profile,
        help_line=help_line, teardown=built.teardown,
        session_labels=built.session_labels,
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
    list_chats: Callable[[SpaceBinding], Awaitable[Sequence[tuple[str, str]]]],
) -> SpaceRuntimes:
    """The Anytype chat composition: space bindings -> per-space runtimes.

    Same posture as :func:`build_channel_runtimes`: SEQUENTIAL assembly
    (concurrent ``ensure_schema`` bursts would trip a throttled server),
    failing the whole bot if any space fails. ``list_chats`` is injected by
    the composition root -- it owns the chat client -- returning ``(id,
    name)`` pairs, so this module stays free of infrastructure.

    WP8: one runtime and one shared route per SPACE; every served chat
    (``served_chat_ids``: all listed minus ``exclude_chats``, or a pinned
    ``chat_id``) is wired to it via :func:`register_chat`, each with its
    own keyed session. A space with zero served chats is a warning, not an
    error -- the live-discovery watcher will pick chats up as they appear.
    ``GC_SPACES_FILE`` unset still fails loudly: a chat bot serving nowhere
    is a misconfiguration, not a default.
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
    teardown: list[TeardownHook] = []
    space_routes: dict[str, ChannelRoute] = {}
    space_bindings: dict[str, SpaceBinding] = {}
    session_labels: dict[str, dict[str, str]] = {}
    runtimes = SpaceRuntimes(
        routes={}, spaces={}, descriptions={},
        help_line=help_line, teardown=teardown,
        space_routes=space_routes, space_bindings=space_bindings,
        session_labels=session_labels,
    )
    for binding in bindings:
        try:
            listed = list(await list_chats(binding))
            runtime = await _assemble_runtime(
                binding.profile, driver, model_name, help_line, turn_log,
                space_id=binding.space_id, project=binding.project,
                modes_file=binding.modes_file,
                default_mode=binding.default_mode,
            )
        except GraphContextError as err:
            await composition.run_teardown(teardown)  # close what already built
            raise GraphContextError(
                f"space {binding.space_id} failed to start: {err}"
            ) from err
        space_routes[binding.space_id] = ChannelRoute(
            orchestrator=runtime.orchestrator
        )
        space_bindings[binding.space_id] = binding
        session_labels[binding.space_id] = runtime.session_labels
        teardown.extend(runtime.teardown)
        served = served_chat_ids(binding, [cid for cid, _ in listed])
        names = dict(listed)
        for chat_id in served:
            register_chat(runtimes, binding.space_id, chat_id, names.get(chat_id, ""))
            logger.info(
                "chat %s -> space %s (profile=%s)",
                chat_id, binding.space_id, binding.profile.name,
            )
        if not served:
            logger.warning(
                "space %s has no served chat yet (profile=%s); the watcher "
                "will pick chats up as they are created",
                binding.space_id, binding.profile.name,
            )
    return runtimes
