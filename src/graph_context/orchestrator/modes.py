"""Activity modes: specs are data; the binding is still the boundary (ADR 015).

A :class:`~graph_context.interface.profiles.ModeSpec` picks one of two tool
bindings -- ``mutating`` gets the full surface, otherwise the read surface
(retrieval + ``context``: focus management is part of every activity) -- and
the enforcement mechanism is unchanged from ADR 007: a spec that isn't
mutating simply never has the mutation tools in its table. Unavailable, not
refused.

Specs come from the active profile's defaults, optionally extended or
overridden by a ``GC_MODES_FILE`` TOML file (deployment configuration)::

    [modes.record_procedure]
    goal = "Notate each step the user takes so it can be repeated later..."
    # mutating defaults to false

    [modes.record_procedure.capture]
    artifact_type = "procedure"
    min_chars = 120

Bad specs fail LOUDLY at load time (specs are prompts; a broken one should
stop startup, not surface mid-turn). In-space mode objects are the stated
direction (ADR 015) -- this loader is the seam they will feed.
"""

from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable, Mapping
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
    profile: DomainProfile, modes_file: str | None = None
) -> ModeRegistry:
    """Profile defaults, overlaid by the optional GC_MODES_FILE TOML.

    File entries override same-named profile specs and may add new ones.
    Every problem raises :class:`GraphContextError` naming the mode and
    field -- load-time is the only acceptable place for a spec to fail.
    """
    specs = {spec.name: spec for spec in profile.mode_specs}
    if modes_file:
        for spec in _parse_modes_file(Path(modes_file)):
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
        if not isinstance(body, dict):
            raise GraphContextError(f"[modes.{name}] must be a table")
        unknown = set(body) - _SPEC_KEYS
        if unknown:
            raise GraphContextError(
                f"[modes.{name}] has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_SPEC_KEYS)}"
            )
        capture = None
        if "capture" in body:
            raw = body["capture"]
            if not isinstance(raw, dict):
                raise GraphContextError(f"[modes.{name}.capture] must be a table")
            unknown = set(raw) - _CAPTURE_KEYS
            if unknown:
                raise GraphContextError(
                    f"[modes.{name}.capture] has unknown keys {sorted(unknown)}; "
                    f"allowed: {sorted(_CAPTURE_KEYS)}"
                )
            capture = CapturePolicy(**raw)
        try:
            specs.append(ModeSpec(
                name=name,
                goal=str(body.get("goal", "")),
                mutating=bool(body.get("mutating", False)),
                capture=capture,
            ))
        except ValueError as err:
            raise GraphContextError(f"GC_MODES_FILE [modes.{name}]: {err}") from None
    return specs
