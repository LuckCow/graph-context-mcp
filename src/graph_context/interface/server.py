"""Composition root: the MCP server (WP2).

The ONLY module that imports the MCP SDK. The build itself (config ->
client -> bootstrap -> repository -> hydrate -> session -> services) lives
in graph_context/composition.py, shared with the orchestrator's composition
root (ADR 007) -- one wiring, two roots.

Backends (env `GC_BACKEND`):
* `anytype` (default) -- requires ANYTYPE_SPACE_ID, a key (ANYTYPE_API_KEY or
  ANYTYPE_API_KEY_FILE), and a running local Anytype (ANYTYPE_API_BASE_URL).
* `memory`  -- in-memory repository; for development and demos without
  Anytype. State evaporates on exit.

The tool wrappers below are deliberately thin and deliberately verbose in
their docstrings: **the docstrings are prompts** (WORK_PACKAGES WP2,
"tool docstrings are prompts"). The LLM chooses tools and fills parameters
by reading them, so every wrapper states defaults, bounds, when to prefer
it over its neighbors, and a worked example. Editing these strings IS
prompt engineering -- expect to iterate on them from dogfooding
transcripts, not from code review alone.

Run:  PYTHONPATH=src python -m graph_context.interface.server
(stdio transport; see README for a Claude Desktop config snippet).

Done since the WP2/WP3 scaffold (integrated against the live-server WP1):
* AnytypeSessionStore is wired into the lifespan (load_or_fresh on startup,
  debounced flush via tools' note_mutation, final flush on teardown).
* Structured per-call logging with durations lives at the tools.guarded
  seam (one INFO line per call: tool, outcome, duration; no payloads).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from graph_context import composition
from graph_context.interface import profiles, tools
from graph_context.interface.tools import Services

logger = logging.getLogger(__name__)

# The active domain profile (WP5): GC_PROFILE picks the docstring set (the
# tool descriptions registered below ARE the profile's prompts) and the
# type-key -> Role additions passed to the repository in _build_services.
# Read at import time because @mcp.tool registration happens at import time.
_PROFILE = profiles.get_profile(os.environ.get("GC_PROFILE"))


@dataclass(slots=True)
class AppContext:
    services: Services
    teardown: list[Any]


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
    # The build itself is shared with the orchestrator's composition root
    # (ADR 007): one wiring, two roots -- see graph_context/composition.py.
    # The runtime's mode store is unused here: activity modes are an
    # orchestrator concept; the MCP surface binds every tool (ADR 007).
    built = await composition.build_runtime(_PROFILE)
    try:
        yield AppContext(services=built.services, teardown=built.teardown)
    finally:
        await composition.run_teardown(built.teardown)


mcp = FastMCP("graph-context", lifespan=lifespan)


def _services(ctx: Context[Any, Any, Any]) -> Services:
    app: AppContext = ctx.request_context.lifespan_context
    return app.services


# ---------------------------------------------------------------------------
# Tool registrations. Docstrings are LLM-facing prompts -- keep them precise.
# ---------------------------------------------------------------------------


@mcp.tool(description=_PROFILE.tool_docs["context"])
async def context(
    ctx: Context[Any, Any, Any],
    action: str = "get",
    node_id: str = "",
    project: str = "",
    text: str = "",
    detail: str = "",
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.context_tool(
        _services(ctx), action=action, node_id=node_id, project=project,
        text=text, detail=detail,
    )


@mcp.tool(description=_PROFILE.tool_docs["create_node"])
async def create_node(
    ctx: Context[Any, Any, Any],
    type: str,
    name: str,
    summary: str,
    description: str = "",
    story_time: float | str | None = None,
    fields: dict[str, str] | None = None,
    links: list[dict[str, Any]] | None = None,
    icon: str = "",
    create_missing_relations: bool = False,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.create_node_tool(
        _services(ctx), type=type, name=name, summary=summary,
        description=description, story_time=story_time, fields=fields, links=links,
        icon=icon,
        create_missing_relations=create_missing_relations,
    )


@mcp.tool(description=_PROFILE.tool_docs["update_node"])
async def update_node(
    ctx: Context[Any, Any, Any],
    node_id: str,
    name: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    story_time: float | str | None = None,
    fields: dict[str, str] | None = None,
    add_links: list[dict[str, Any]] | None = None,
    remove_links: list[dict[str, Any]] | None = None,
    create_missing_relations: bool = False,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.update_node_tool(
        _services(ctx), node_id=node_id, name=name, summary=summary,
        description=description, story_time=story_time, fields=fields,
        add_links=add_links, remove_links=remove_links,
        create_missing_relations=create_missing_relations,
    )


@mcp.tool(description=_PROFILE.tool_docs["get_node"])
async def get_node(
    ctx: Context[Any, Any, Any],
    node_id: str,
    edge_types: list[str] | None = None,
    include_provenance: int = 0,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.get_node_tool(
        _services(ctx), node_id=node_id, edge_types=edge_types,
        include_provenance=include_provenance,
    )


@mcp.tool(description=_PROFILE.tool_docs["explore"])
async def explore(
    ctx: Context[Any, Any, Any],
    start: str = "",
    depth: int = 1,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    as_of: float | str | None = None,
    include_future: bool = False,
    limit: int = 25,
    detail: str = "summaries",
    only_stale: bool = False,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.explore_tool(
        _services(ctx), start=start, depth=depth, include_types=include_types,
        exclude_types=exclude_types, edge_types=edge_types, as_of=as_of,
        include_future=include_future, limit=limit, detail=detail,
        only_stale=only_stale,
    )


@mcp.tool(description=_PROFILE.tool_docs["query"])
async def query(
    ctx: Context[Any, Any, Any],
    type: str = "",
    linked_to: str = "",
    edge_types: list[str] | None = None,
    where: list[dict[str, Any]] | None = None,
    order_by: list[str] | None = None,
    view: str = "",
    limit: int = 25,
    detail: str = "summaries",
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.query_tool(
        _services(ctx), type=type, linked_to=linked_to, edge_types=edge_types,
        where=where, order_by=order_by, view=view, limit=limit, detail=detail,
    )


@mcp.tool(description=_PROFILE.tool_docs["find_path"])
async def find_path(
    ctx: Context[Any, Any, Any],
    target: str,
    start: str = "",
    edge_types: list[str] | None = None,
    max_length: int = 4,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.find_path_tool(
        _services(ctx), target=target, start=start, edge_types=edge_types,
        max_length=max_length,
    )


@mcp.tool(description=_PROFILE.tool_docs["find_node"])
async def find_node(
    ctx: Context[Any, Any, Any],
    name: str,
    type: str = "",
    limit: int = 10,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.find_node_tool(
        _services(ctx), name=name, type=type, limit=limit,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()
