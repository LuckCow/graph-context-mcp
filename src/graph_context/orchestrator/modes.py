"""Modes and their tool bindings (ADR 007).

The mode boundary is the HARNESS owning the binding: in authoring mode the
mutation tools are not in the table at all -- the model cannot call what it
was never handed, so "please don't mutate" is an enforcement rather than a
convention. World-modeling binds the full surface.

Authoring keeps the read/retrieval tools plus ``context`` (focus management
is explicitly part of the authoring workflow: pin the scene's cast, walk
the neighborhood, write).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

from graph_context.interface import tools
from graph_context.interface.profiles import DomainProfile
from graph_context.interface.tools import Services

ToolFn = Callable[..., Awaitable[str]]


class Mode(StrEnum):
    WORLD_MODELING = "world_modeling"
    AUTHORING = "authoring"


_FULL_SURFACE: dict[str, ToolFn] = {
    "context": tools.context_tool,
    "create_node": tools.create_node_tool,
    "update_node": tools.update_node_tool,
    "get_node": tools.get_node_tool,
    "explore": tools.explore_tool,
    "find_path": tools.find_path_tool,
    "find_node": tools.find_node_tool,
    "record_prose": tools.record_prose_tool,
}

MUTATION_TOOLS: frozenset[str] = frozenset(
    {"create_node", "update_node", "record_prose"}
)

TOOL_BINDINGS: Mapping[Mode, Mapping[str, ToolFn]] = {
    Mode.WORLD_MODELING: _FULL_SURFACE,
    Mode.AUTHORING: {
        name: fn for name, fn in _FULL_SURFACE.items()
        if name not in MUTATION_TOOLS
    },
}


def tool_docs(mode: Mode, profile: DomainProfile) -> Mapping[str, str]:
    """The LLM-facing docs for a mode's binding -- what the driver may call.

    Docstrings are prompts (WP2); the profile supplies the words, the mode
    supplies the subset.
    """
    return {
        name: profile.tool_docs[name] for name in TOOL_BINDINGS[mode]
    }


async def invoke(
    mode: Mode, name: str, services: Services, arguments: Mapping[str, Any]
) -> str | None:
    """Run one bound tool; ``None`` when the mode's binding lacks it.

    ``None`` is the defensive runtime face of the binding boundary -- a
    driver that hallucinates an unbound tool gets an actionable message
    from the pipeline, but the enforcement is the table above.
    """
    fn = TOOL_BINDINGS[mode].get(name)
    if fn is None:
        return None
    return await fn(services, **arguments)
