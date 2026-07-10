"""Tool implementations: the v1 MCP surface, SDK-free.

``server.py`` registers thin FastMCP wrappers around these functions;
keeping the implementations here (plain async functions over a
:class:`Services` bundle) means they are testable in-process without an
MCP client, and the SDK never leaks below the composition root.

Two invariants every tool maintains -- enforced by ``guarded``, the one
wrapper everything goes through:

1. **Errors are prompts.** Any :class:`GraphContextError` is returned as
   ``ERROR: <message>`` -- its message is written for an LLM trying to
   self-correct, so parse failures must list the allowed values (see the
   ``_parse_*`` helpers). Unexpected exceptions are logged server-side and
   returned as a generic message: never leak stack traces into a story.
2. **Policy stays here.** e.g. `explore` excludes Prose/SessionContext by
   default (WP2 decision) -- the domain traversal remains policy-free.

(The per-response ``[project | focus | recent]`` context header was
removed 2026-07-06 as token waste. WP15 replaced the focus stack with
the LLM-curated working set + scratchpad, echoed once per orchestrator
turn instead of on every response; recent history still feeds traversal
defaults.)

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
from graph_context.application.querier import Querier
from graph_context.application.ranker import Ranker
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.application.session_persister import SessionPersister
from graph_context.domain import schema
from graph_context.domain.models import Edge, LinkSpec, NodeDraft, NodeId
from graph_context.domain.overview import build_overview
from graph_context.domain.query import (
    NodeQuery,
    Op,
    Predicate,
    SortKey,
    normalize_value,
)
from graph_context.domain.schema import Role
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery, node_identifiers
from graph_context.errors import GraphContextError, NodeNotFound, UnknownNodeType
from graph_context.interface import presenters
from graph_context.interface.presenters import Detail
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.view_catalog import ViewCatalog

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
    querier: Querier
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
    views: ViewCatalog | None = None,
) -> Services:
    journal = journal or NullJournal()
    return Services(
        repository=repository,
        session=session,
        writer=NodeWriter(repository, session, journal),
        reader=NodeReader(repository, session),
        explorer=Explorer(repository, session),
        querier=Querier(repository, views),
        capture=CaptureRecorder(repository, journal=journal),
        persister=persister,
        journal=journal,
        projector=projector,
        ranker=ranker,
    )


def derive_services(
    base: Services, session: SessionState, persister: SessionPersister | None
) -> Services:
    """A per-session view of one runtime (WP8): rebind the three
    session-bound services, share everything expensive by reference.

    Repository (and its GraphIndex), querier, capture, journal, projector,
    and ranker stay THE runtime's instances -- sessions are views over one
    space, not runtimes of their own. Cheap: three thin wrappers, no I/O.
    """
    return Services(
        repository=base.repository,
        session=session,
        writer=NodeWriter(base.repository, session, base.journal),
        reader=NodeReader(base.repository, session),
        explorer=Explorer(base.repository, session),
        querier=base.querier,
        capture=base.capture,
        persister=persister,
        journal=base.journal,
        projector=base.projector,
        ranker=base.ranker,
    )


# -- the one wrapper ------------------------------------------------------


def guarded(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """GraphContextError -> actionable ERROR line; nothing else escapes.

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
        except Exception:  # never leak a traceback into a story
            outcome = "error"
            logger.exception("unexpected error in tool %s", fn.__name__)
            body = "ERROR: internal error; details were logged server-side."
        finally:
            logger.info(
                "tool=%s outcome=%s duration_ms=%.1f",
                fn.__name__, outcome, (time.perf_counter() - start) * 1000,
            )
        return body

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


_OPS_LISTING = ", ".join(op.value for op in Op)


def _parse_predicates(raw: Sequence[dict[str, Any]] | None) -> tuple[Predicate, ...]:
    predicates = []
    for item in raw or []:
        field_name = str(item.get("field", "")).strip()
        if not field_name or "op" not in item:
            raise GraphContextError(
                "each where item needs 'field' and 'op' (plus 'value' unless "
                f"op is exists/missing); ops: {_OPS_LISTING}"
            )
        try:
            op = Op(str(item["op"]).strip().casefold())
        except ValueError:
            raise GraphContextError(
                f"unknown op {item['op']!r}; allowed: {_OPS_LISTING}"
            ) from None
        predicates.append(
            Predicate(
                field=field_name,
                op=op,
                value=normalize_value(item.get("value", "")),
            )
        )
    return tuple(predicates)


def _parse_order_by(raw: Sequence[str] | None) -> tuple[SortKey, ...]:
    keys = []
    for item in raw or []:
        parts = str(item).split()
        directions = {"asc": False, "desc": True}
        if len(parts) == 1:
            keys.append(SortKey(field=parts[0]))
        elif len(parts) == 2 and parts[1].casefold() in directions:
            keys.append(
                SortKey(field=parts[0], descending=directions[parts[1].casefold()])
            )
        else:
            raise GraphContextError(
                f"bad order_by entry {item!r}; each entry is 'field', "
                "'field asc', or 'field desc'"
            )
    return tuple(keys)


def _validate_query_type(services: Services, requested: str) -> Role | None:
    """Typo-check a query's type filter and resolve its role.

    The vocabulary is open, so accept anything the space registry knows,
    any role name, or any identifier a node in the graph actually carries;
    reject the rest with the known-types listing (errors are prompts). A
    known type with zero instances proceeds and honestly matches nothing.
    """
    wanted = requested.casefold()
    role = services.repository.role_for(requested)
    if role is None:
        role = next((r for r in Role if r.value.casefold() == wanted), None)
    known = {t.casefold() for t in services.repository.known_node_types()}
    if wanted in known or role is not None:
        return role
    for node in services.repository.graph.nodes():
        if any(i.casefold() == wanted for i in node_identifiers(node)):
            return node.role
    raise UnknownNodeType(requested, tuple(services.repository.known_node_types()))


def _parse_field_declarations(
    raw: dict[str, str] | None,
) -> dict[str, str] | None:
    """Normalize a ``create_missing_fields`` map (key -> format); format
    well-formedness is the writer's rule (schema.validate_field_declarations)."""
    if raw is None:
        return None
    return {str(k).strip(): str(v).strip().lower() for k, v in raw.items()}


def _node_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_node_type(v) for v in values)


def _edge_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_edge_type(v) for v in values)


# -- tools ------------------------------------------------------------------


SCRATCHPAD_MAX_CHARS = 2000  # over-cap is an error that teaches condensing


def _parse_hold_detail(value: str) -> Detail:
    normalized = value.strip().casefold()
    levels = {
        "": Detail.SUMMARIES,  # default bucket
        "summary": Detail.SUMMARIES,
        "summaries": Detail.SUMMARIES,
        "full": Detail.FULL,
    }
    if normalized not in levels:
        raise GraphContextError(
            f"unknown hold detail {value!r}; allowed: summaries (default), full"
        )
    return levels[normalized]


def _render_session_echo(services: Services) -> list[str]:
    """The `get` action's session section (WP15): scratchpad + working set
    + recent trail, names resolved against the live graph (vanished nodes
    are skipped, never crash a response)."""
    session = services.session
    graph = services.repository.graph
    working_set = session.working_set

    def name_of(node_id: NodeId) -> str | None:
        if not graph.has_node(node_id):
            return None
        node = graph.node(node_id)
        return f"{node.name} ({node.type}, id={node.id})"

    lines = [f"scratchpad: {session.scratchpad or '(empty)'}"]
    held = [
        f"- {label} [{entry.detail.value}]"
        for entry in working_set.entries
        if (label := name_of(entry.node_id))
    ]
    if held:
        full_used = sum(
            1 for e in working_set.entries if e.detail is Detail.FULL
        )
        lines.append(
            f"working set ({full_used}/{working_set.full_slots} full, "
            f"{len(working_set) - full_used}/{working_set.summary_slots} "
            "summary slots):"
        )
        lines.extend(held)
    else:
        lines.append(
            "working set: empty -- keep a node in every turn's context "
            "with context action='hold'."
        )
    recent = [n for i in session.recent.items if (n := name_of(i))]
    if recent:
        lines.append(f"recent: {', '.join(r.split(' (')[0] for r in recent)}")
    return lines


@guarded
async def context_tool(
    services: Services,
    action: str = "get",
    node_id: str = "",
    project: str = "",
    text: str = "",
    detail: str = "",
) -> str:
    graph = services.repository.graph
    session = services.session
    if action == "get":
        # Count only story nodes -- the managed SessionContext node and Prose
        # passages are bookkeeping and would otherwise inflate an empty world.
        story = [n for n in graph.nodes() if n.role not in schema.INFRA_ROLES]
        stale = sum(1 for n in story if n.summary_stale)
        lines = [
            f"graph: {len(story)} nodes, {graph.edge_count()} edges, "
            f"{stale} stale summaries. "
            "Call context action='overview' for entry-point node ids.",
            *_render_session_echo(services),
        ]
        return "\n".join(lines)
    if action in {"overview", "map"}:
        # Derived cold-start map: per-type counts + highest-degree hub nodes,
        # each with an id to start exploring from, plus the space's property
        # catalog (ADR 023) so writes reuse existing properties as fields
        # keys. Empty graph -> guidance, not an error (a fresh session
        # should get something actionable).
        return presenters.render_overview(
            build_overview(graph), services.repository.field_catalog()
        )
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
    if action == "note":
        if len(text) > SCRATCHPAD_MAX_CHARS:
            raise GraphContextError(
                f"scratchpad is limited to {SCRATCHPAD_MAX_CHARS} characters "
                f"(got {len(text)}); condense it -- durable facts belong in "
                "the graph, not the scratchpad"
            )
        session.scratchpad = text.strip()
        # Flush immediately: the scratchpad is the model's cross-turn
        # memory; losing it to the mutation debounce defeats the feature.
        if services.persister is not None:
            await services.persister.flush()
        if not session.scratchpad:
            return "scratchpad cleared."
        return (
            f"scratchpad replaced ({len(session.scratchpad)} chars); it is "
            "echoed at the start of your next turn."
        )
    if action == "hold":
        if not node_id:
            raise GraphContextError(
                "action 'hold' requires node_id (a node id or name)"
            )
        level = _parse_hold_detail(detail)
        node_id = await _resolve(services, node_id)  # accept a name first
        outcome = session.working_set.hold(node_id, level)
        parts = [f"holding {graph.node(node_id).name} [{level.value}]"]
        parts.extend(
            f"demoted to summaries ({session.working_set.full_slots} full "
            f"slots): {graph.node(i).name}"
            for i in outcome.demoted if graph.has_node(i)
        )
        parts.extend(
            f"released ({session.working_set.summary_slots} summary slots): "
            f"{graph.node(i).name}"
            for i in outcome.evicted if graph.has_node(i)
        )
        return "; ".join(parts) + "."
    if action == "release":
        if not node_id:
            raise GraphContextError(
                "action 'release' requires node_id (a node id or name)"
            )
        try:
            resolved = await _resolve(services, node_id)
        except NodeNotFound:
            # The node may have been deleted out from under the hold; a
            # raw-id release must still work so the set can be tidied.
            if session.working_set.release(node_id):
                return f"released {node_id} (node no longer exists)."
            raise
        if session.working_set.release(resolved):
            return f"released {graph.node(resolved).name}."
        return f"{graph.node(resolved).name} was not held."
    if action == "clear":
        session.working_set.clear()
        return (
            "working set cleared. The scratchpad is kept; clear it with "
            "action='note', text=''."
        )
    raise GraphContextError(
        f"unknown action {action!r}; allowed: get, overview, resync, "
        "set_project, note, hold, release, clear"
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
    create_missing_fields: dict[str, str] | None = None,
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
        create_missing_fields=_parse_field_declarations(create_missing_fields),
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
    create_missing_fields: dict[str, str] | None = None,
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
        create_missing_fields=_parse_field_declarations(create_missing_fields),
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
    # Empty start still defaults to the session default in the Explorer.
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
async def query_tool(
    services: Services,
    type: str = "",
    linked_to: str = "",
    edge_types: list[str] | None = None,
    where: list[dict[str, Any]] | None = None,
    order_by: list[str] | None = None,
    view: str = "",
    limit: int = 25,
    detail: str = "summaries",
) -> str:
    detail_level = _parse_detail(detail)  # fail fast, before any scanning
    if view.strip():
        # WP13/ADR 018: a saved Set view IS a server-defined type+where+
        # order_by -- combining them is ambiguous, so it is an error.
        if type or linked_to or edge_types or where or order_by:
            raise GraphContextError(
                "view cannot be combined with type/linked_to/edge_types/"
                "where/order_by -- the view already defines those; drop "
                "them or drop view"
            )
        saved, view_result = await services.querier.run_view(
            view, limit=limit, exclude_roles=schema.INFRA_ROLES
        )
        view_bodies = None
        if detail_level is Detail.FULL:
            view_bodies = await services.explorer.bodies_for(
                [node.id for node in view_result.hits]
            )
        rendered = presenters.render_query_result(
            view_result, detail_level, saved.query.order_by, view_bodies
        )
        return f"view {saved.full_name!r}:\n{rendered}"
    predicates = _parse_predicates(where)
    sort_keys = _parse_order_by(order_by)
    node_type = type.strip() or None
    # Corpus scans reach everything, so hide ALL bookkeeping roles (not
    # just explore's default set -- mode config objects included) unless
    # the type filter explicitly names an infra type (same escape hatch
    # as explore's include_types).
    exclude_roles: frozenset[Role] = schema.INFRA_ROLES
    if node_type is not None:
        role = _validate_query_type(services, node_type)
        if role in schema.INFRA_ROLES:
            exclude_roles = frozenset()
    anchor = await _resolve(services, linked_to) if linked_to else None
    result = await services.querier.query(
        NodeQuery(
            node_type=node_type,
            linked_to=anchor,
            edge_types=_edge_type_set(edge_types),
            predicates=predicates,
            order_by=sort_keys,
            limit=limit,
            exclude_roles=exclude_roles,
        )
    )
    bodies = None
    if detail_level is Detail.FULL:
        bodies = await services.explorer.bodies_for(
            [node.id for node in result.hits]
        )
    return presenters.render_query_result(result, detail_level, sort_keys, bodies)


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
