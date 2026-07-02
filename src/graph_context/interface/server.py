"""Composition root: the MCP server (WP2).

The ONLY module that imports the MCP SDK and the only one allowed to
import infrastructure. Wiring: config -> client -> bootstrap -> repository
-> hydrate -> session (restored via SessionStore) -> services -> tools.

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
* `get_node` exposes include_prose (NodeReader grew the reverse-reference
  lookup + on-demand body excerpts).
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

from graph_context.domain.session import SessionState
from graph_context.interface import profiles, tools
from graph_context.interface.tools import Services, build_services

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


async def _build_services() -> tuple[Services, list[Any]]:
    backend = os.environ.get("GC_BACKEND", "anytype")
    session = SessionState(project=os.environ.get("GC_PROJECT_NAME"))
    # WP3 privacy/size knob: GC_STORE_LLM_INPUT=0 stops record_prose from
    # persisting assembled prompts (llm_input) into the space.
    store_llm_input = os.environ.get("GC_STORE_LLM_INPUT", "1").lower() not in {
        "0", "false", "no",
    }
    teardown: list[Any] = []

    logger.info("profile=%s (%s)", _PROFILE.name, _PROFILE.description)
    if backend == "memory":
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )

        logger.info("backend=memory (development mode; nothing persists)")
        return build_services(
            InMemoryGraphRepository(role_overrides=_PROFILE.role_overrides),
            session,
            store_llm_input=store_llm_input,
        ), teardown

    from graph_context.application.session_persister import SessionPersister
    from graph_context.infrastructure.anytype.client import AnytypeClient
    from graph_context.infrastructure.anytype.config import AnytypeConfig
    from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
    from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
    from graph_context.infrastructure.anytype.session_repository import (
        AnytypeSessionStore,
    )

    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    teardown.append(client.aclose)
    await ensure_schema(client)
    # GC_FIELD_DENYLIST (ADR 012): comma-separated property keys to hide
    # from field reflection, on top of the built-in system-noise denylist.
    field_denylist = frozenset(
        key.strip()
        for key in os.environ.get("GC_FIELD_DENYLIST", "").split(",")
        if key.strip()
    )
    repository = AnytypeGraphRepository(
        client,
        role_overrides=_PROFILE.role_overrides,
        field_denylist=field_denylist,
    )
    await repository.hydrate()
    logger.info(
        "hydrated space %s: %d nodes / %d edges",
        config.space_id,
        repository.graph.node_count(),
        repository.graph.edge_count(),
    )
    # Restore the working session from the SessionContext meta-node, and
    # arrange a debounced flush (note_mutation in tools) + a final flush on
    # shutdown. A corrupt/missing snapshot degrades to the fresh session.
    store = AnytypeSessionStore(client)
    session = await SessionPersister.load_or_fresh(store, session)
    if not session.project:
        # Derived cosmetic default: the space's own name. Never blocks startup;
        # GC_PROJECT_NAME and a persisted set_project both take precedence.
        try:
            session.project = (await client.get_space()).get("name") or None
        except Exception:  # noqa: BLE001
            logger.warning("could not read space name for the project label")
    persister = SessionPersister(store, session)
    teardown.append(persister.flush)  # flush on shutdown (LIFO: before aclose)
    return build_services(
        repository, session, persister, store_llm_input=store_llm_input
    ), teardown


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


def _services(ctx: Context[Any, Any, Any]) -> Services:
    app: AppContext = ctx.request_context.lifespan_context
    return app.services


# ---------------------------------------------------------------------------
# Tool registrations. Docstrings are LLM-facing prompts -- keep them precise.
# ---------------------------------------------------------------------------


@mcp.tool(description=_PROFILE.tool_docs["context"])
async def context(
    ctx: Context[Any, Any, Any], action: str = "get", node_id: str = "", project: str = ""
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.context_tool(_services(ctx), action=action, node_id=node_id, project=project)


@mcp.tool(description=_PROFILE.tool_docs["create_node"])
async def create_node(
    ctx: Context[Any, Any, Any],
    type: str,
    name: str,
    summary: str,
    description: str = "",
    story_time: float | None = None,
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
    story_time: float | None = None,
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
    include_prose: int = 0,
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.get_node_tool(
        _services(ctx), node_id=node_id, edge_types=edge_types,
        include_prose=include_prose,
    )


@mcp.tool(description=_PROFILE.tool_docs["explore"])
async def explore(
    ctx: Context[Any, Any, Any],
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
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.explore_tool(
        _services(ctx), start=start, depth=depth, include_types=include_types,
        exclude_types=exclude_types, edge_types=edge_types, as_of=as_of,
        include_future=include_future, limit=limit, detail=detail,
        only_stale=only_stale,
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


@mcp.tool(description=_PROFILE.tool_docs["record_prose"])
async def record_prose(
    ctx: Context[Any, Any, Any],
    text: str,
    summary: str,
    references: list[str],
    title: str = "",
    llm_input: str = "",
    llm_output: str = "",
    model: str = "",
) -> str:
    """LLM-facing description supplied by the active profile (profiles.py)."""
    return await tools.record_prose_tool(
        _services(ctx), text=text, summary=summary, references=references,
        title=title, llm_input=llm_input, llm_output=llm_output, model=model,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()
