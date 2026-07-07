"""Activity modes: specs are data; the binding is still the boundary (ADR 015).

A :class:`~graph_context.interface.profiles.ModeSpec` picks one of two tool
bindings -- ``mutating`` gets the full surface, otherwise the read surface
(retrieval + ``context``: focus management is part of every activity) -- and
the enforcement mechanism is unchanged from ADR 007: a spec that isn't
mutating simply never has the mutation tools in its table. Unavailable, not
refused.

Specs come from the active profile's defaults, overlaid (in precedence
order) by a ``GC_MODES_FILE`` TOML file (deployment configuration)::

    [modes.record_procedure]
    goal = "Notate each step the user takes so it can be repeated later..."
    # mutating defaults to false

    [modes.record_procedure.capture]
    artifact_type = "procedure"
    min_chars = 120

and by the space's own ``Activity Mode`` objects (ADR 015 amendment) --
``in_space`` payloads read through the ModeStore port, where the object
name slugifies to the mode name and the page body is the goal. In-space
wins: Anytype is the human editing surface, and an edit made there must
never be shadowed by a file.

Bad specs fail LOUDLY at load time (specs are prompts; a broken one should
stop startup, not surface mid-turn); a RUNTIME reload (the ``/mode``
refresh) catches the same errors and degrades instead -- see the pipeline.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_context.errors import GraphContextError
from graph_context.interface import tools
from graph_context.interface.profiles import CapturePolicy, DomainProfile, ModeSpec
from graph_context.interface.tools import Services

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
    profile: DomainProfile,
    modes_file: str | None = None,
    in_space: Sequence[Mapping[str, Any]] = (),
) -> ModeRegistry:
    """Profile defaults < GC_MODES_FILE TOML < in-space mode objects.

    Later sources override same-named specs and may add new ones;
    ``in_space`` payloads come from the ModeStore port. Every problem
    raises :class:`GraphContextError` naming the config source, mode, and
    field -- load-time is the only acceptable place for a spec to fail.
    """
    specs = {spec.name: spec for spec in profile.mode_specs}
    if modes_file:
        for spec in _parse_modes_file(Path(modes_file)):
            specs[spec.name] = spec
    for spec in _parse_in_space(in_space):
        specs[spec.name] = spec
    if not specs:
        raise GraphContextError(
            f"profile {profile.name!r} defines no activity modes and no "
            "GC_MODES_FILE was given"
        )
    default = profile.default_mode if profile.default_mode in specs else next(iter(specs))
    return ModeRegistry(specs=specs, default=default)


_SPEC_KEYS = {"goal", "mutating", "capture"}
_CAPTURE_KEYS = {"artifact_type", "references_label", "min_chars"}


def _spec_from_mapping(
    name: str, body: Mapping[str, Any], origin: str
) -> ModeSpec:
    """One validated ModeSpec from config data; errors name ``origin``.

    The single validation seam for every config source (TOML file,
    in-space objects) -- the sources differ only in how ``name``, the
    field mapping, and the ``origin`` label are derived.
    """
    unknown = set(body) - _SPEC_KEYS
    if unknown:
        raise GraphContextError(
            f"{origin} has unknown keys {sorted(unknown)}; "
            f"allowed: {sorted(_SPEC_KEYS)}"
        )
    capture = None
    if body.get("capture") is not None:
        raw = body["capture"]
        if not isinstance(raw, Mapping):
            raise GraphContextError(f"{origin}: capture must be a table")
        unknown = set(raw) - _CAPTURE_KEYS
        if unknown:
            raise GraphContextError(
                f"{origin}: capture has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_CAPTURE_KEYS)}"
            )
        kwargs = dict(raw)
        if "min_chars" in kwargs:
            kwargs["min_chars"] = _positive_int(
                kwargs["min_chars"], f"{origin}: min_chars"
            )
        capture = CapturePolicy(**kwargs)
    try:
        return ModeSpec(
            name=name,
            goal=str(body.get("goal", "")),
            mutating=bool(body.get("mutating", False)),
            capture=capture,
        )
    except ValueError as err:
        raise GraphContextError(f"{origin}: {err}") from None


def _positive_int(value: Any, origin: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or value != int(value)
        or value <= 0
    ):
        raise GraphContextError(
            f"{origin} must be a positive whole number, got {value!r}"
        )
    return int(value)


def _slugify(name: str) -> str:
    """An object's display name -> the ``/mode`` name.

    ``"Faithful Scribe"`` -> ``faithful_scribe``; empty when nothing
    alphanumeric survives.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _parse_modes_file(path: Path) -> list[ModeSpec]:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise GraphContextError(f"GC_MODES_FILE not found: {path}") from None
    except tomllib.TOMLDecodeError as err:
        raise GraphContextError(f"GC_MODES_FILE is not valid TOML: {err}") from None
    modes = data.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise GraphContextError(
            "GC_MODES_FILE must define at least one [modes.<name>] table"
        )
    specs: list[ModeSpec] = []
    for name, body in modes.items():
        origin = f"GC_MODES_FILE [modes.{name}]"
        if not isinstance(body, dict):
            raise GraphContextError(f"{origin} must be a table")
        specs.append(_spec_from_mapping(name, body, origin))
    return specs


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
        name = _slugify(str(payload.get("name") or ""))
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
            for key in ("goal", "mutating", "capture")
            if key in payload
        }
        specs.append(_spec_from_mapping(name, body, origin))
    return specs
