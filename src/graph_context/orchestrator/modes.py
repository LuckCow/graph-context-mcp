"""Activity modes: specs are data; the binding is still the boundary (ADR 015).

A :class:`~graph_context.interface.profiles.ModeSpec` picks one of two tool
bindings -- ``mutating`` gets the full surface, otherwise the read surface
(retrieval + ``context``: context curation is part of every activity) -- and
the enforcement mechanism is unchanged from ADR 007: a spec that isn't
mutating simply never has the mutation tools in its table. Unavailable, not
refused.

Since ADR 035 the space's own ``Activity Mode`` objects are the ONLY live
source of specs: ``in_space`` payloads read through the ModeStore port,
where the object name slugifies to the mode name and the page body is the
goal. Anytype is the human editing surface; there is no profile or TOML
overlay left to shadow an edit made there (seed TOMLs mint starter
objects into a mode-less space at composition time -- see
``interface/mode_config.py`` -- and are never consulted again).

Which mode NEW chats start in is in-space config too (ADR 034): the
space's ``Space Context`` singleton links an Activity Mode object via
``gc_default_mode``, read through the SpaceContextStore port; no link
falls back to the alphabetically first mode, with a logged hint.

Bad specs fail LOUDLY at load time (specs are prompts; a broken one should
stop startup, not surface mid-turn); a RUNTIME reload (the ``/mode``
refresh) catches the same errors and degrades instead -- see the pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from graph_context.domain.model_choice import model_id
from graph_context.errors import GraphContextError
from graph_context.interface import tools
from graph_context.interface.mode_config import slugify, spec_from_mapping
from graph_context.interface.profiles import DomainProfile, ModeSpec
from graph_context.interface.services import Services
from graph_context.orchestrator.drivers import DecideOptions

logger = logging.getLogger(__name__)

ToolFn = Callable[..., Awaitable[str]]

_FULL_SURFACE: dict[str, ToolFn] = {
    "context": tools.context_tool,
    "create_node": tools.create_node_tool,
    "update_node": tools.update_node_tool,
    "get_node": tools.get_node_tool,
    "explore": tools.explore_tool,
    "find_path": tools.find_path_tool,
    "find_node": tools.find_node_tool,
    "query": tools.query_tool,
    # ADR 027: like `context`, session bookkeeping rather than graph
    # authorship -- deliberately NOT a mutation tool, so read-only modes
    # can still take "remind me" requests (the node it mints is infra).
    "schedule": tools.schedule_tool,
    # ADR 040: same posture -- automation config is space bookkeeping
    # (the node it mints is infra), so read-only modes can still take
    # "whenever X changes, do Y" requests.
    "automation": tools.automation_tool,
    # WP23 (ADR 032): delivery, not graph authorship -- queues a file for
    # the transport to attach to the reply; read-only modes can send too.
    "send_file": tools.send_file_tool,
    # WP33 (ADR 041): schema proposals are drafted in conversation and
    # gated on the USER's confirmation, so binding them everywhere does
    # not let a read-only mode change anything unilaterally -- the human
    # authorizes every apply. Same posture as schedule/automation.
    "schema": tools.schema_tool,
}

MUTATION_TOOLS: frozenset[str] = frozenset({"create_node", "update_node"})

_READ_SURFACE: dict[str, ToolFn] = {
    name: fn for name, fn in _FULL_SURFACE.items() if name not in MUTATION_TOOLS
}


def binding_for(spec: ModeSpec) -> Mapping[str, ToolFn]:
    """The spec's tool table -- the boundary itself (ADR 007)."""
    return _FULL_SURFACE if spec.mutating else _READ_SURFACE


def full_surface() -> Mapping[str, ToolFn]:
    """Every tool any spec can bind -- drivers derive schemas from the
    wrappers' signatures (one source of truth, never a maintained table)."""
    return dict(_FULL_SURFACE)


def tool_docs(spec: ModeSpec, profile: DomainProfile) -> Mapping[str, str]:
    """The LLM-facing docs for a spec's binding -- what the driver may call.

    Docstrings are prompts (WP2); the profile supplies the words, the spec
    supplies the subset.
    """
    return {name: profile.tool_docs[name] for name in binding_for(spec)}


async def invoke(
    spec: ModeSpec, name: str, services: Services, arguments: Mapping[str, Any]
) -> str | None:
    """Run one bound tool; ``None`` when the spec's binding lacks it.

    ``None`` is the defensive runtime face of the binding boundary -- a
    driver that hallucinates an unbound tool gets an actionable message
    from the pipeline, but the enforcement is the table above.
    """
    fn = binding_for(spec).get(name)
    if fn is None:
        return None
    return await fn(services, **arguments)


@dataclass(frozen=True, slots=True)
class ModeRegistry:
    """The deployment's loaded activity modes."""

    specs: Mapping[str, ModeSpec]
    default: str

    def get(self, name: str) -> ModeSpec | None:
        return self.specs.get(name)

    def names(self) -> list[str]:
        return sorted(self.specs)


def load_registry(
    in_space: Sequence[Mapping[str, Any]],
    space_context: Sequence[Mapping[str, Any]] = (),
) -> ModeRegistry:
    """The space's Activity Mode objects -> the live registry (ADR 035).

    ``in_space`` payloads come from the ModeStore port -- the ONLY spec
    source since ADR 035. Every problem raises
    :class:`GraphContextError` naming the object and field; load-time is
    the only acceptable place for a spec to fail.

    ``space_context`` payloads come from the SpaceContextStore port (ADR
    034): the space's Space Context object links the Activity Mode
    object NEW chats start in. No link falls back to the alphabetically
    first mode (deterministic; a logged hint says how to choose) -- a
    BROKEN link fails loudly like any other in-space config error.
    """
    specs = {spec.name: spec for spec in _parse_in_space(in_space)}
    if not specs:
        raise GraphContextError(
            "the space has no Activity Mode objects to load; create one in "
            "Anytype (or unarchive one) -- a restart reseeds the starter "
            "modes into a space that has none (ADR 035)"
        )
    if set(specs) == {"example_mode"}:
        # The pre-ADR-035 signature: the mint-time explainer is blocking
        # the heal in a space that used to ride the retired profile modes.
        logger.warning(
            "the only loaded mode is the Example Mode template; archive it "
            "and restart to seed the starter modes (ADR 035 migration)"
        )
    default = _default_from_space_context(space_context, in_space)
    if default is None:
        default = sorted(specs)[0]
        logger.info(
            "no default-mode link on the Space Context; defaulting to %r "
            "-- link an Activity Mode object there to choose", default,
        )
    return ModeRegistry(specs=specs, default=default)


def _default_from_space_context(
    payloads: Sequence[Mapping[str, Any]],
    in_space: Sequence[Mapping[str, Any]],
) -> str | None:
    """The space-declared default mode name, or ``None`` (ADR 034).

    ``payloads`` are SpaceContextStore payloads (normally zero or one);
    the default is the mode whose OBJECT the singleton's
    ``gc_default_mode`` relation links. Resolving by object id means the
    default can only ever be an in-space Activity Mode -- which is the
    point: the space's settings point at the space's own config objects,
    never at names that exist only in code or a TOML file.
    """
    if not payloads:
        return None
    if len(payloads) > 1:
        listed = ", ".join(
            str(p.get("origin") or "(unknown object)") for p in payloads
        )
        raise GraphContextError(
            f"the space has {len(payloads)} Space Context objects ({listed}); "
            "keep exactly one -- archive the rest"
        )
    payload = payloads[0]
    origin = f"Space Context {payload.get('origin') or '(unknown object)'}"
    ids = [str(i) for i in payload.get("default_mode_ids") or []]
    if not ids:
        return None
    if len(ids) > 1:
        raise GraphContextError(
            f"{origin}: the default-mode field links {len(ids)} objects; "
            "link exactly one Activity Mode"
        )
    modes_by_id = {str(p.get("id") or ""): p for p in in_space}
    target = modes_by_id.get(ids[0])
    if target is None:
        raise GraphContextError(
            f"{origin}: the default-mode field links object {ids[0]}, which "
            "is not a loadable Activity Mode object (archived, deleted, or "
            "not an Activity Mode?) -- link one of the space's Activity "
            "Mode objects"
        )
    # The name reduced to a usable slug and parsed into a spec, or
    # _parse_in_space would have raised before we got here.
    return slugify(str(target.get("name") or ""))


def _parse_in_space(payloads: Sequence[Mapping[str, Any]]) -> list[ModeSpec]:
    """ModeStore payloads (the space's Activity Mode objects) -> specs.

    The object name slugifies to the mode name and the page body is the
    goal; errors name the Anytype object so the human knows what to fix
    or archive.
    """
    specs: list[ModeSpec] = []
    origins: dict[str, str] = {}
    for payload in payloads:
        label = str(payload.get("origin") or "(unknown object)")
        origin = f"Activity Mode {label}"
        name = slugify(str(payload.get("name") or ""))
        if not name:
            raise GraphContextError(
                f"{origin}: the object name {payload.get('name')!r} does not "
                "reduce to a usable mode name -- use letters and digits"
            )
        if name in origins:
            raise GraphContextError(
                f"{origin} and Activity Mode {origins[name]} both resolve to "
                f"mode name {name!r}; rename or archive one"
            )
        origins[name] = label
        if not str(payload.get("goal") or "").strip():
            raise GraphContextError(
                f"{origin}: the goal is empty -- write the mode's "
                "instructions in the object's page body"
            )
        body = {
            key: payload[key]
            for key in (
                "goal", "mutating", "capture", "activity_detail",
                "web_search", "model",
                "thinking", "max_tokens", "web_search_max_uses",
                "web_search_allowed_domains", "web_search_blocked_domains",
            )
            if key in payload
        }
        specs.append(spec_from_mapping(name, body, origin))
    return specs


def decide_options(spec: ModeSpec) -> DecideOptions:
    """The spec's driver options for one decision (ADR 037).

    The model choice resolves to its provider id here -- drivers take
    ids, never canonical names; everything else passes through with
    "empty/zero = not set" intact.
    """
    return DecideOptions(
        web_search=spec.web_search,
        model=model_id(spec.model),
        thinking=spec.thinking,
        max_tokens=spec.max_tokens,
        web_search_max_uses=spec.web_search_max_uses,
        web_search_allowed_domains=spec.web_search_allowed_domains,
        web_search_blocked_domains=spec.web_search_blocked_domains,
    )
