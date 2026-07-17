"""Activity-mode config: the validation seam + the seed corpus (ADR 035).

Since ADR 035 the space's own Activity Mode objects are the ONLY live
source of mode specs; a modes TOML exists solely to SEED starter objects
into a space that has none (fresh space, or every mode archived). This
module is the layering-neutral home for everything both sides of that
split need:

* :func:`spec_from_mapping` / :func:`slugify` -- the single validation
  seam every config source funnels through (the orchestrator's in-space
  loader imports them back; nothing may import the orchestrator, so they
  cannot live there);
* the seed-TOML parser (:func:`parse_seed_modes` /
  :func:`load_seed_modes`) -- today's ``[modes.<name>]`` tables plus two
  seed-only keys, ``default`` (which minted mode the Space Context's
  default link points at) and ``icon`` (the minted object's emoji);
* :func:`seed_payloads` -- seeds rendered in the ModeStore-port payload
  shape, so ONE representation feeds the in-memory store, the Anytype
  seeder, and the eval runner alike.

Packaged starter sets live beside this module in ``mode_seeds/`` (one
TOML per profile name); a deployment's ``modes_file``/``GC_MODES_FILE``
overrides the packaged set as the seed source. Bad seed config fails
LOUDLY at parse time, like every other config error.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from graph_context.errors import GraphContextError
from graph_context.interface.profiles import CapturePolicy, ModeSpec

_SPEC_KEYS = {
    "goal", "mutating", "capture", "activity_detail", "web_search", "model",
}
_CAPTURE_KEYS = {"artifact_type", "references_label", "min_chars"}
_SEED_ONLY_KEYS = {"default", "icon"}


def slugify(name: str) -> str:
    """A display name -> the ``/mode`` name.

    ``"Faithful Scribe"`` -> ``faithful_scribe``; empty when nothing
    alphanumeric survives.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def spec_from_mapping(
    name: str, body: Mapping[str, Any], origin: str
) -> ModeSpec:
    """One validated ModeSpec from config data; errors name ``origin``.

    The single validation seam for every config source (seed TOML,
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
    # Humans type the level (TOML or the Anytype UI): normalize case and
    # padding; empty means "not set" and takes the default. The model
    # choice (ADR 033) follows the same rule.
    detail = str(body.get("activity_detail") or "").strip().lower()
    model = str(body.get("model") or "").strip().lower()
    try:
        return ModeSpec(
            name=name,
            goal=str(body.get("goal", "")),
            mutating=bool(body.get("mutating", False)),
            capture=capture,
            web_search=bool(body.get("web_search", False)),
            model=model,
            **({"activity_detail": detail} if detail else {}),
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


@dataclass(frozen=True, slots=True)
class ModeSeed:
    """One starter mode: a validated spec plus its mint-time dressing."""

    name: str          # the slug (the TOML table key)
    display_name: str  # the minted object's name; slugifies back to name
    spec: ModeSpec
    icon: str = ""
    default: bool = False


def parse_seed_modes(text: str, origin: str) -> tuple[ModeSeed, ...]:
    """Parse a seed TOML; every problem names ``origin`` and its spot.

    At most one mode may set ``default = true``; when none does, the
    FIRST table is the default (a seed corpus always has a definite
    starting mode, so the Space Context link can always be set).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise GraphContextError(f"{origin} is not valid TOML: {err}") from None
    modes = data.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise GraphContextError(
            f"{origin} must define at least one [modes.<name>] table"
        )
    seeds: list[ModeSeed] = []
    for name, body in modes.items():
        table = f"{origin} [modes.{name}]"
        if not isinstance(body, dict):
            raise GraphContextError(f"{table} must be a table")
        unknown = set(body) - _SPEC_KEYS - _SEED_ONLY_KEYS
        if unknown:
            raise GraphContextError(
                f"{table} has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_SPEC_KEYS | _SEED_ONLY_KEYS)}"
            )
        icon = body.get("icon")
        if icon is not None and not isinstance(icon, str):
            raise GraphContextError(f"{table}: icon must be a string")
        default = body.get("default")
        if default is not None and not isinstance(default, bool):
            raise GraphContextError(f"{table}: default must be a boolean")
        spec_body = {k: v for k, v in body.items() if k in _SPEC_KEYS}
        if "goal" in spec_body:
            # TOML formatting (trailing newline of a multi-line string)
            # is never part of the prompt.
            spec_body["goal"] = str(spec_body["goal"]).strip()
        spec = spec_from_mapping(name, spec_body, table)
        seeds.append(ModeSeed(
            name=spec.name,
            display_name=name.replace("_", " ").title(),
            spec=spec,
            icon=(icon or "").strip(),
            default=bool(default),
        ))
    marked = [seed.name for seed in seeds if seed.default]
    if len(marked) > 1:
        raise GraphContextError(
            f"{origin} marks {len(marked)} modes as default "
            f"({', '.join(marked)}); mark at most one"
        )
    return tuple(seeds)


def load_seed_modes(
    source: str | None, profile_name: str
) -> tuple[ModeSeed, ...]:
    """The deployment's seed corpus: ``source`` file, else the packaged set.

    ``source`` is the configured ``modes_file``/``GC_MODES_FILE`` path;
    ``None`` falls back to the packaged ``mode_seeds/<profile_name>.toml``.
    """
    if source:
        try:
            text = Path(source).read_text()
        except OSError as err:
            raise GraphContextError(
                f"cannot read the modes seed file at {source}: {err}"
            ) from None
        return parse_seed_modes(text, f"modes seed file {source}")
    packaged = (
        resources.files("graph_context.interface")
        / "mode_seeds" / f"{profile_name}.toml"
    )
    try:
        text = packaged.read_text()
    except (FileNotFoundError, OSError) as err:
        raise GraphContextError(
            f"no packaged mode seeds for profile {profile_name!r}: {err}"
        ) from None
    return parse_seed_modes(text, f"packaged mode seeds {profile_name}.toml")


def default_seed(seeds: Sequence[ModeSeed]) -> ModeSeed:
    """The seed the Space Context default link should point at."""
    for seed in seeds:
        if seed.default:
            return seed
    return seeds[0]


def seed_payloads(seeds: Sequence[ModeSeed]) -> list[dict[str, Any]]:
    """Seeds in the ModeStore-port payload shape (plus seed-only extras).

    ``id`` is synthetic (``seed:<slug>``) so a fabricated Space Context
    payload can link the default before any real object exists (the
    memory backend and the eval runner do exactly that); ``icon`` and
    ``default`` ride along for the Anytype seeder and are ignored by the
    in-space loader.
    """
    payloads: list[dict[str, Any]] = []
    for seed in seeds:
        spec = seed.spec
        payload: dict[str, Any] = {
            "id": f"seed:{seed.name}",
            "name": seed.display_name,
            "goal": spec.goal,
            "mutating": spec.mutating,
            "web_search": spec.web_search,
            "capture": None,
            "activity_detail": spec.activity_detail,
            "origin": f"seed [modes.{seed.name}]",
            "icon": seed.icon,
            "default": seed.default,
        }
        if spec.capture is not None:
            payload["capture"] = {
                "artifact_type": spec.capture.artifact_type,
                "references_label": spec.capture.references_label,
                "min_chars": spec.capture.min_chars,
            }
        if spec.model:
            payload["model"] = spec.model
        payloads.append(payload)
    return payloads
