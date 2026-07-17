"""Seeded registries for orchestrator tests (ADR 035).

Profiles no longer carry mode specs; the packaged starter corpora are
the canonical world_modeling/authoring/organizing definitions. These
helpers build a registry the way a freshly seeded space loads one --
seed payloads through the in-space seam, the marked default linked via
a fabricated Space Context payload -- so tests exercise the production
resolution instead of a shortcut.
"""

from __future__ import annotations

from typing import Any

from graph_context.interface.mode_config import (
    default_seed,
    load_seed_modes,
    seed_payloads,
)
from graph_context.orchestrator.modes import ModeRegistry, load_registry


def seeded_registry(
    profile_name: str = "fiction", *extra: dict[str, Any]
) -> ModeRegistry:
    """The profile's starter corpus (+ extra in-space payloads), loaded
    exactly as a freshly seeded space would be."""
    seeds = load_seed_modes(None, profile_name)
    return load_registry(
        in_space=[*seed_payloads(seeds), *extra],
        space_context=[{
            "name": "Space Context",
            "origin": "Space Context (tests)",
            "default_mode_ids": [f"seed:{default_seed(seeds).name}"],
        }],
    )


def fiction_registry(*extra: dict[str, Any]) -> ModeRegistry:
    return seeded_registry("fiction", *extra)
