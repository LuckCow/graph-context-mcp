"""Composition root: the MCP server (WP2).

The ONLY module that imports the MCP SDK and the only one allowed to
import infrastructure. Wiring: config -> client -> bootstrap -> repository
-> hydrate -> session (restored via SessionStore) -> services -> tools.

Backends (env `GC_BACKEND`):
* `anytype` (default) -- requires ANYTYPE_API_KEY + ANYTYPE_SPACE_ID and a
  running local Anytype.
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

TODO(junior):
* Wire AnytypeSessionStore into the lifespan (load_or_fresh + flush on
  teardown) -- the persister plumbing exists; marked below.
* `get_node` include_prose parameter once NodeReader grows it (WP3).
* Structured per-call logging with durations (WP2 deliverable): a small
  middleware-style wrapper around tools.guarded is the right seam.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from graph_context.domain.session import SessionState
from graph_context.interface import tools
from graph_context.interface.tools import Services, build_services

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    services: Services
    teardown: list[Any]


async def _build_services() -> tuple[Services, list[Any]]:
    backend = os.environ.get("GC_BACKEND", "anytype")
    session = SessionState(project=os.environ.get("GC_PROJECT_NAME"))
    teardown: list[Any] = []

    if backend == "memory":
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )

        logger.info("backend=memory (development mode; nothing persists)")
        return build_services(InMemoryGraphRepository(), session), teardown

    from graph_context.infrastructure.anytype.client import AnytypeClient
    from graph_context.infrastructure.anytype.config import AnytypeConfig
    from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
    from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema

    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    teardown.append(client.aclose)
    await ensure_schema(client)
    repository = AnytypeGraphRepository(client)
    await repository.hydrate()
    logger.info(
        "hydrated space %s: %d nodes / %d edges",
        config.space_id,
        repository.graph.node_count(),
        repository.graph.edge_count(),
    )
    # TODO(junior): restore + persist the session via AnytypeSessionStore:
    #   store = AnytypeSessionStore(client)
    #   session = await SessionPersister.load_or_fresh(store, session)
    #   persister = SessionPersister(store, session)
    #   teardown.append(persister.flush)         # flush on shutdown
    #   return build_services(repository, session, persister), teardown
    return build_services(repository, session), teardown


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
    services, teardown = await _build_services()
    try:
        yield AppContext(services=services, teardown=teardown)
    finally:
        for hook in reversed(teardown):
            try:
                await hook()
            except Exception:  # noqa: BLE001
                logger.exception("teardown hook failed")


mcp = FastMCP("graph-context", lifespan=lifespan)


def _services(ctx: Context) -> Services:
    app: AppContext = ctx.request_context.lifespan_context
    return app.services


# ---------------------------------------------------------------------------
# Tool registrations. Docstrings are LLM-facing prompts -- keep them precise.
# ---------------------------------------------------------------------------


@mcp.tool()
async def context(
    ctx: Context, action: str = "get", node_id: str = "", project: str = ""
) -> str:
    """Inspect or adjust the working session: graph stats, focus stack, resync.

    Actions:
      get          -- graph statistics (node/edge counts, stale summaries).
      resync       -- pull in edits a human made directly in Anytype; reports
                      which nodes changed. Use before a long writing session.
      focus        -- push node_id onto the focus stack (queries default to
                      the top of this stack when no start is given).
      pin / unpin  -- protect / unprotect node_id from focus-stack eviction.
      remove       -- drop node_id from the focus stack.
      clear        -- empty the focus stack (pinned entries survive).
      set_project  -- relabel the project shown in the header (cosmetic; one
                      server is bound to one story world).

    Every tool response begins with `[project | focus | recent]` -- this tool
    is how you manage what appears there.
    """
    return await tools.context_tool(_services(ctx), action=action, node_id=node_id, project=project)


@mcp.tool()
async def create_node(
    ctx: Context,
    type: str,
    name: str,
    summary: str,
    description: str = "",
    story_time: float | None = None,
    fields: dict[str, str] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> str:
    """Create a story-world node and its initial links in ONE call.

    type: Character | Location | Event | Technology | Faction | Item.
    summary: REQUIRED one-liner; keep it current -- exploration shows it.
    story_time: REQUIRED for Event (number; position on the story timeline).
    links: list of {"edge_type", "other" (node id), "outgoing" (default true)}.
      outgoing=false means the edge points FROM `other` TO the new node --
      e.g. creating an Event that an existing Character took part in:
        {"edge_type": "participated_in", "other": "<character id>",
         "outgoing": false}
    Edge types: knows, located_at, member_of, participated_in, caused,
    possesses, parent_of, child_of, precedes.

    Prefer linking at creation over separate update_node calls.
    """
    return await tools.create_node_tool(
        _services(ctx), type=type, name=name, summary=summary,
        description=description, story_time=story_time, fields=fields, links=links,
    )


@mcp.tool()
async def update_node(
    ctx: Context,
    node_id: str,
    name: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    story_time: float | None = None,
    fields: dict[str, str] | None = None,
    add_links: list[dict[str, Any]] | None = None,
    remove_links: list[dict[str, Any]] | None = None,
) -> str:
    """Modify a node's fields and/or links. Only provided arguments change.

    IMPORTANT: any update WITHOUT a new `summary` flags the node's summary
    as stale (the one-liner may no longer reflect reality). Pass a fresh
    `summary` whenever the change is meaningful; clear backlog stale flags
    later via explore(only_stale=true).

    add_links: same shape as create_node's links.
    remove_links: list of {"source", "edge_type", "target"} exactly as shown
    by get_node.
    """
    return await tools.update_node_tool(
        _services(ctx), node_id=node_id, name=name, summary=summary,
        description=description, story_time=story_time, fields=fields,
        add_links=add_links, remove_links=remove_links,
    )


@mcp.tool()
async def get_node(
    ctx: Context, node_id: str, edge_types: list[str] | None = None
) -> str:
    """Read ONE node in depth: all fields plus every edge grouped by type,
    with neighbor names and ids. Use when you need the full picture of a
    single entity; use `explore` to see a neighborhood instead.

    edge_types: optional filter, e.g. ["participated_in", "knows"].
    """
    return await tools.get_node_tool(_services(ctx), node_id=node_id, edge_types=edge_types)


@mcp.tool()
async def explore(
    ctx: Context,
    start: str = "",
    depth: int = 1,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    as_of: float | None = None,
    include_future: bool = False,
    limit: int = 25,
    detail: str = "summaries",
    only_stale: bool = False,
) -> str:
    """Walk the graph outward from a node. THE general retrieval primitive.

    start: node id; empty = top of the focus stack. depth: 1-3 (default 1).
    detail: names | summaries (default) | full.
    as_of: story-time cutoff -- Events after it are hidden (a character's
    view of the world at that moment); include_future=true restores them
    (foreshadowing/direction). limit caps results (default 25; the response
    says when it truncated).

    SCENE ASSEMBLY is an explore configuration, not a separate tool:
      explore(start="<event id>", depth=2,
              include_types=["Character", "Location", "Item"],
              detail="summaries", as_of=<event time>)

    STALE-SUMMARY SWEEP (before a big writing session):
      explore(depth=3, limit=50, only_stale=true, detail="names")
      ...then update_node each with a fresh summary.

    Prose and SessionContext nodes are hidden unless explicitly named in
    include_types.
    """
    return await tools.explore_tool(
        _services(ctx), start=start, depth=depth, include_types=include_types,
        exclude_types=exclude_types, edge_types=edge_types, as_of=as_of,
        include_future=include_future, limit=limit, detail=detail,
        only_stale=only_stale,
    )


@mcp.tool()
async def find_path(
    ctx: Context,
    target: str,
    start: str = "",
    edge_types: list[str] | None = None,
    max_length: int = 4,
) -> str:
    """Find the shortest meaningful connection between two nodes -- "how is
    Mira related to the Fall of Brakk?" Surfaces non-obvious links for plot
    work. start: empty = focus-stack top. Edge direction is ignored for
    reachability but shown in the result. Restrict edge_types to make the
    path more meaningful (e.g. only social edges: ["knows", "member_of"]).
    """
    return await tools.find_path_tool(
        _services(ctx), target=target, start=start, edge_types=edge_types,
        max_length=max_length,
    )


@mcp.tool()
async def record_prose(
    ctx: Context,
    text: str,
    summary: str,
    references: list[str],
    title: str = "",
    llm_input: str = "",
    llm_output: str = "",
    model: str = "",
) -> str:
    """Save rendered prose INTO the graph so future scenes stay consistent.

    Call after writing any scene/passage the user keeps. references is
    REQUIRED and must list every node id whose content shaped the prose
    (the characters present, the location, the events depicted) -- this
    powers later consistency checks. summary: one-liner of what the passage
    covers. Optionally store the generation inputs (llm_input/llm_output/
    model) for provenance.
    """
    return await tools.record_prose_tool(
        _services(ctx), text=text, summary=summary, references=references,
        title=title, llm_input=llm_input, llm_output=llm_output, model=model,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()
