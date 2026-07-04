"""Tool implementations: the v1 MCP surface, SDK-free.

``server.py`` registers thin FastMCP wrappers around these functions;
keeping the implementations here (plain async functions over a
:class:`Services` bundle) means they are testable in-process without an
MCP client, and the SDK never leaks below the composition root.

Three invariants every tool maintains -- enforced by ``guarded``, the one
wrapper everything goes through:

1. **Context echo.** Every response, success or error, begins with the
   session header. A tool that forgets the header is unrepresentable.
2. **Errors are prompts.** Any :class:`GraphContextError` is returned as
   ``ERROR: <message>`` -- its message is written for an LLM trying to
   self-correct, so parse failures must list the allowed values (see the
   ``_parse_*`` helpers). Unexpected exceptions are logged server-side and
   returned as a generic message: never leak stack traces into a story.
3. **Policy stays here.** e.g. `explore` excludes Prose/SessionContext by
   default (WP2 decision) -- the domain traversal remains policy-free.

Notes:
* `context` actions `set_project` / `resync`: resync is wired; project
  switching is a stub by design -- one server process = one space in v1
  (the repository is bound to a space id at construction). The stub's
  message explains that to the LLM. Revisit only with multi-space config.
* Writes call `_note_mutation(services)`, which drives the debounced
  SessionPersister wired in server.py's lifespan (a no-op when absent, as
  in the memory backend and most tests).
* Capture is the ORCHESTRATOR's job (WP7 auto-capture); the record_prose
  tool was removed 2026-07-04 -- the project is pre-deployment, so no
  vestigial surface is kept. CaptureRecorder is the service the harness
  calls, with the artifact type set by the active mode's CapturePolicy
  (ADR 015).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.application.explorer import Explorer
from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.application.node_reader import NodeReader
from graph_context.application.node_writer import NodeWriter
from graph_context.application.ranker import Ranker
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.application.session_persister import SessionPersister
from graph_context.domain import schema
from graph_context.domain.models import Edge, LinkSpec, NodeDraft, NodeId
from graph_context.domain.overview import build_overview
from graph_context.domain.schema import Role
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery
from graph_context.errors import GraphContextError, NodeNotFound
from graph_context.interface import presenters
from graph_context.interface.presenters import Detail
from graph_context.ports.graph_repository import GraphRepository

logger = logging.getLogger(__name__)

# WP2 decision: bookkeeping node *roles* never surface in traversal unless
# explicitly included. Tool-layer policy, not domain. (Intent joins via
# INFRA_ROLES-driven reader suppression; here the explore default.)
DEFAULT_EXPLORE_EXCLUDE_ROLES = frozenset(
    {Role.CAPTURE, Role.SESSION_CONTEXT, Role.INTENT}
)


@dataclass(slots=True)
class Services:
    """Everything a tool call needs, built once in the composition root."""

    repository: GraphRepository
    session: SessionState
    writer: NodeWriter
    reader: NodeReader
    explorer: Explorer
    capture: CaptureRecorder
    persister: SessionPersister | None = None  # wired in server lifespan
    # WP7: the orchestrator passes a real MutationJournal and drains it per
    # turn; the MCP server keeps the NullJournal (no turn boundary).
    journal: MutationJournal = field(default_factory=NullJournal)
    # WP11 (ADR 014): None when GC_EMBEDDER=off -- the semantic layer
    # degrades away and tools fall back to name search alone.
    projector: SemanticProjector | None = None
    ranker: Ranker | None = None


def build_services(
    repository: GraphRepository,
    session: SessionState,
    persister: SessionPersister | None = None,
    *,
    journal: MutationJournal | None = None,
    projector: SemanticProjector | None = None,
    ranker: Ranker | None = None,
) -> Services:
    journal = journal or NullJournal()
    return Services(
        repository=repository,
        session=session,
        writer=NodeWriter(repository, session, journal),
        reader=NodeReader(repository, session),
        explorer=Explorer(repository, session),
        capture=CaptureRecorder(repository, journal=journal),
        persister=persister,
        journal=journal,
        projector=projector,
        ranker=ranker,
    )


# -- the one wrapper ------------------------------------------------------


def guarded(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """Header on every response; GraphContextError -> actionable ERROR line.

    Also the single seam for structured per-call logging (WP2 deliverable):
    one INFO line per tool with name, ok/error outcome, and duration.
    Deliberately logs *no* payload -- prose text and summaries are a user's
    creative work and must never appear above DEBUG.
    """

    @wraps(fn)
    async def wrapper(services: Services, *args: Any, **kwargs: Any) -> str:
        start = time.perf_counter()
        outcome = "ok"
        try:
            body = await fn(services, *args, **kwargs)
        except GraphContextError as known:
            outcome = "error"
            body = f"ERROR: {known}"
        except Exception:  # noqa: BLE001 -- never leak a traceback into a story
            outcome = "error"
            logger.exception("unexpected error in tool %s", fn.__name__)
            body = "ERROR: internal error; details were logged server-side."
        finally:
            logger.info(
                "tool=%s outcome=%s duration_ms=%.1f",
                fn.__name__, outcome, (time.perf_counter() - start) * 1000,
            )
        header = presenters.render_context_header(
            services.session, services.repository.graph
        )
        return f"{header}\n{body}"

    return wrapper


async def _note_mutation(services: Services) -> None:
    if services.persister is not None:
        await services.persister.note_mutation()


# -- parsing helpers: error messages are written FOR the LLM ---------------


async def _resolve(services: Services, identifier: str) -> NodeId:
    """Translate a user-supplied id-or-name into a real node id.

    Resolution is a tool-layer concern (the same boundary that does all
    ``_parse_*`` normalization), so the application and domain layers keep
    receiving canonical ids. Raises NodeNotFound/AmbiguousNodeName, both
    actionable, when the string does not resolve to exactly one node.

    ADR 016: on a miss, the Ranker (when wired) appends "closest by
    meaning" candidates WITH evidence to the error -- a suggestion
    surface, never silent resolution: exact resolves, fuzzy suggests,
    and mutation targets are never guessed (ADR 014 non-feature).
    """
    try:
        return services.repository.graph.resolve(identifier).id
    except NodeNotFound:
        if services.ranker is None:
            raise
        hits = await services.ranker.rank(identifier, limit=3)
        if not hits:
            raise
        raise NodeNotFound(
            identifier, suggestions=presenters.render_ranked_hits(hits)
        ) from None


def _parse_node_type(value: str) -> str:
    """Normalize a requested node type. The vocabulary is OPEN: validation
    (does this type exist in the space?) is the repository's job, which
    raises an actionable ``UnknownNodeType`` listing the known types."""
    normalized = value.strip()
    if not normalized:
        raise GraphContextError("node 'type' must be a non-empty string")
    return normalized


def _parse_edge_type(value: str) -> str:
    """Normalize a relation label. OPEN vocabulary: an unknown label is
    surfaced for approval by the repository, not rejected here."""
    normalized = value.strip()
    if not normalized:
        raise GraphContextError("each link needs a non-empty 'edge_type' label")
    return normalized


def _parse_detail(value: str) -> Detail:
    try:
        return Detail(value)
    except ValueError:
        raise GraphContextError(
            f"unknown detail level {value!r}; allowed: names, summaries, full"
        ) from None


async def _parse_links(
    raw: Sequence[dict[str, Any]] | None, services: Services
) -> list[LinkSpec]:
    links: list[LinkSpec] = []
    for item in raw or []:
        if "edge_type" not in item or "other" not in item:
            raise GraphContextError(
                "each link needs 'edge_type' and 'other' (target node id or "
                "name); optional 'outgoing' (default true; false means the "
                "edge points FROM 'other' TO this node)"
            )
        links.append(
            LinkSpec(
                edge_type=_parse_edge_type(str(item["edge_type"])),
                other=await _resolve(services, str(item["other"])),
                outgoing=bool(item.get("outgoing", True)),
            )
        )
    return links


def _node_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_node_type(v) for v in values)


def _edge_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_edge_type(v) for v in values)


# -- tools ------------------------------------------------------------------


@guarded
async def context_tool(
    services: Services,
    action: str = "get",
    node_id: str = "",
    project: str = "",
) -> str:
    graph = services.repository.graph
    if action == "get":
        # Count only story nodes -- the managed SessionContext node and Prose
        # passages are bookkeeping and would otherwise inflate an empty world.
        story = [n for n in graph.nodes() if n.role not in schema.INFRA_ROLES]
        stale = sum(1 for n in story if n.summary_stale)
        return (
            f"graph: {len(story)} nodes, {graph.edge_count()} edges, "
            f"{stale} stale summaries. "
            "Call context action='overview' for entry-point node ids."
        )
    if action in {"overview", "map"}:
        # Derived cold-start map: per-type counts + highest-degree hub nodes,
        # each with an id to start exploring from. Empty graph -> guidance,
        # not an error (a fresh session should get something actionable).
        return presenters.render_overview(build_overview(graph))
    if action == "resync":
        changed = await services.repository.resync()
        if services.projector is not None and changed:
            # Keep the embedding cache in step with out-of-band edits.
            await services.projector.refresh(changed)
        if not changed:
            return "resync: no out-of-band changes."
        names = sorted(
            graph.node(i).name for i in changed if graph.has_node(i)
        )
        removed = len(changed) - len(names)
        suffix = f" ({removed} removed)" if removed else ""
        return (
            f"resync: {len(changed)} node(s) changed outside this "
            f"session{suffix}: {', '.join(names)}"
        )
    if action == "set_project":
        # v1: one server process = one space; the label is cosmetic.
        services.session.project = project or services.session.project
        return (
            "project label updated. Note: this server is bound to one "
            "Anytype space; switching spaces means restarting the server "
            "with a different ANYTYPE_SPACE_ID."
        )
    if action in {"focus", "pin", "unpin", "remove", "clear"}:
        if action == "clear":
            services.session.focus.clear()
            return "focus cleared (pinned entries kept)."
        if not node_id:
            raise GraphContextError(f"action {action!r} requires node_id")
        node_id = await _resolve(services, node_id)  # accept a name first
        getattr(services.session.focus, "push" if action == "focus" else action)(node_id)
        return f"focus {action}: {graph.node(node_id).name}"
    raise GraphContextError(
        f"unknown action {action!r}; allowed: get, overview, resync, "
        "set_project, focus, pin, unpin, remove, clear"
    )


@guarded
async def create_node_tool(
    services: Services,
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
    draft = NodeDraft(
        type=_parse_node_type(type),
        name=name,
        summary=summary,
        # Tool-surface "description" = the node's body (ADR 010).
        body=description,
        story_time=story_time,
        fields=fields or {},
        icon=icon.strip(),
    )
    node = await services.writer.create_node(
        draft,
        await _parse_links(links, services),
        create_missing_relations=create_missing_relations,
    )
    await _note_mutation(services)
    view = await services.reader.get_node(node.id)
    return f"created:\n{presenters.render_node_view(view)}"


@guarded
async def update_node_tool(
    services: Services,
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
    node_id = await _resolve(services, node_id)
    removals = [
        Edge(
            source=await _resolve(services, str(i["source"])),
            type=_parse_edge_type(str(i["edge_type"])),
            target=await _resolve(services, str(i["target"])),
            property_key=str(i.get("property_key", "")),
        )
        for i in remove_links or []
    ]
    node = await services.writer.update_node(
        node_id,
        name=name,
        summary=summary,
        description=description,
        story_time=story_time,
        fields=fields,
        add_links=await _parse_links(add_links, services),
        remove_links=removals,
        create_missing_relations=create_missing_relations,
    )
    await _note_mutation(services)
    stale_note = (
        "\nNOTE: summary flagged stale (no fresh summary in this update); "
        "supply `summary` to clear it."
        if node.summary_stale
        else ""
    )
    view = await services.reader.get_node(node.id)
    return f"updated:\n{presenters.render_node_view(view)}{stale_note}"


@guarded
async def get_node_tool(
    services: Services,
    node_id: str,
    edge_types: list[str] | None = None,
    include_provenance: int = 0,
) -> str:
    view = await services.reader.get_node(
        await _resolve(services, node_id),
        edge_type_filter=_edge_type_set(edge_types),
        include_provenance=include_provenance,
        excerpt_chars=presenters.EXCERPT_CHARS,
    )
    return presenters.render_node_view(view)


@guarded
async def explore_tool(
    services: Services,
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
    detail_level = _parse_detail(detail)  # fail fast, before any traversal
    excludes = _node_type_set(exclude_types) or frozenset()
    includes = _node_type_set(include_types)
    exclude_roles: frozenset[Role] = frozenset()
    if includes is None:
        # WP2 default: bookkeeping roles stay invisible unless included.
        exclude_roles = DEFAULT_EXPLORE_EXCLUDE_ROLES
    # Empty start still defaults to the focus-stack top in the Explorer.
    if start:
        start = await _resolve(services, start)
    result = await services.explorer.explore(
        ExploreQuery(
            start=start,
            depth=depth,
            include_node_types=includes,
            exclude_node_types=excludes,
            edge_types=_edge_type_set(edge_types),
            as_of=as_of,
            include_future=include_future,
            limit=limit,
            exclude_roles=exclude_roles,
        )
    )
    if only_stale:
        # WP3 stale-summary workflow: tool-layer narrowing, no new tool.
        from dataclasses import replace

        result = replace(
            result,
            hits=tuple(h for h in result.hits if h.node.summary_stale or h.depth == 0),
        )
    bodies = None
    if detail_level is Detail.FULL:
        # detail='full' = summaries + full bodies, fetched on demand
        # (ADR 010) -- after narrowing, so only rendered hits cost a GET.
        bodies = await services.explorer.bodies_for(
            [hit.node.id for hit in result.hits]
        )
    return presenters.render_explore_result(result, detail_level, bodies)


@guarded
async def find_path_tool(
    services: Services,
    target: str,
    start: str = "",
    edge_types: list[str] | None = None,
    max_length: int = 4,
) -> str:
    path = await services.explorer.find_path(
        await _resolve(services, start) if start else None,
        await _resolve(services, target),
        edge_types=_edge_type_set(edge_types),
        max_length=max_length,
    )
    return presenters.render_path(path)


@guarded
async def find_node_tool(
    services: Services,
    name: str,
    type: str = "",
    limit: int = 10,
) -> str:
    matches = services.repository.graph.find_by_name(
        name, node_type=type or None, limit=limit
    )
    if matches or services.ranker is None:
        return presenters.render_node_matches(matches)
    # Tier 3 (ADRs 014/016): no name matched -- treat the input as a
    # DESCRIPTION. Hits are labelled so the LLM knows it holds fuzzy
    # matches, and each carries its evidence.
    hits = await services.ranker.rank(name, limit=limit)
    if type:
        wanted = type.strip().lower()
        hits = [
            h for h in hits
            if wanted in {h.node.type.lower(), h.node.type_key.lower()}
        ]
    if not hits:
        return presenters.render_node_matches([])  # honest empty + guidance
    return (
        f"find_node: no name match; {len(hits)} semantic match(es) for "
        f"{name!r}:\n{presenters.render_ranked_hits(hits)}"
    )
