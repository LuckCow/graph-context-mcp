"""Presenters: turning domain results into the strings tools return.

Detail levels (``names`` / ``summaries`` / ``full``) are an interface
concern -- the traversal always returns full nodes; how much of each node
reaches the LLM's context window is decided at the edge. Keeping this out
of the domain means response-budget tuning never touches tested logic.

(The ``[project | focus | recent]`` context-header echo that used to open
every response was removed 2026-07-06 as token waste.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_context.application.node_reader import NodeView
from graph_context.application.ranker import RankedHit
from graph_context.domain.models import Detail as Detail  # re-export (WP15)
from graph_context.domain.models import Node
from graph_context.domain.overview import GraphOverview
from graph_context.domain.pathfinding import Path
from graph_context.domain.query import QueryResult, SortKey, field_value
from graph_context.domain.traversal import ExploreResult

# Detail moved to domain.models (WP15) -- the working set persists it; it
# stays importable from here for rendering call sites.

EXCERPT_CHARS = 300  # provenance excerpts; tune by dogfooding


def render_overview(overview: GraphOverview) -> str:
    """Render the cold-start entry-point map (``context action='overview'``).

    Ids are the last token before the colon on every hub line, so the LLM
    can copy one straight into ``explore`` / ``get_node`` / ``hold``.
    """
    if overview.total_story_nodes == 0:
        return "overview: no nodes yet -- use create_node to add the first one."
    types = ", ".join(f"{tc.type} {tc.count}" for tc in overview.type_counts)
    lines = [
        f"overview: {overview.total_story_nodes} nodes across "
        f"{len(overview.type_counts)} types (derived entry-point map).",
        f"types: {types}",
        "entry points (highest-degree nodes; pass an id to explore, "
        "get_node, or context action='hold'):",
    ]
    for hub in overview.hubs:
        node = hub.node
        stale = " [summary stale]" if node.summary_stale else ""
        lines.append(
            f"- {node.name} ({node.type}, id={node.id}){stale}: {node.summary}"
        )
    return "\n".join(lines)


def render_ranked_hits(hits: Sequence[RankedHit]) -> str:
    """Ranked matches with their evidence (ADR 016) -- one hit per line
    pair, id in the standard copy-paste position, evidence indented so the
    LLM can verify the reasoning before committing to an id."""
    lines = []
    for hit in hits:
        node = hit.node
        lines.append(
            f"- {node.name} ({node.type}, id={node.id}): {node.summary}"
        )
        if hit.evidence:
            lines.append(f"    why: {'; '.join(hit.evidence)}")
    return "\n".join(lines)


def render_node_matches(nodes: list[Node]) -> str:
    """Render ``find_node`` results as copy-paste-able entry-point lines.

    Same line shape as the overview's hub list -- id is the last token
    before the colon -- so a match drops straight into any tool. Zero
    matches is not an error: it returns guidance toward the overview.
    """
    if not nodes:
        return (
            "find_node: no match. Try a shorter or different name, drop the "
            "type filter, or call context action='overview' for entry points."
        )
    header = f"find_node: {len(nodes)} match(es)."
    lines = [header]
    for node in nodes:
        stale = " [summary stale]" if node.summary_stale else ""
        lines.append(
            f"- {node.name} ({node.type}, id={node.id}){stale}: {node.summary}"
        )
    return "\n".join(lines)


def render_explore_result(
    result: ExploreResult,
    detail: Detail,
    bodies: Mapping[str, str] | None = None,
) -> str:
    """``bodies`` (node id -> full text) accompanies ``detail='full'``: the
    tool layer fans out on-demand fetches (ADR 010) and passes them in, so
    the presenter stays I/O-free."""
    lines = [
        _render_hit_line(hit.node, hit.depth, detail, (bodies or {}).get(hit.node.id, ""))
        for hit in result.hits
    ]
    if result.truncated:
        lines.append(
            "... result limit reached; narrow filters or raise `limit` to see more."
        )
    return "\n".join(lines)


def _render_hit_line(
    node: Node, depth: int, detail: Detail, body: str = "", annotation: str = ""
) -> str:
    indent = "  " * depth
    base = f"{indent}- {node.name} ({node.type}, id={node.id}){annotation}"
    if detail is Detail.NAMES:
        return base
    stale = " [summary stale]" if node.summary_stale else ""
    if detail is Detail.SUMMARIES or not body:
        return f"{base}{stale}: {node.summary}"
    return f"{base}{stale}: {node.summary}\n{indent}    {body}"


def render_query_result(
    result: QueryResult,
    detail: Detail,
    order_by: Sequence[SortKey] = (),
    bodies: Mapping[str, str] | None = None,
) -> str:
    """Render ``query`` hits with their sort-key values echoed inline.

    The annotation (``[due_date=2026-07-10, priority=High]``) makes the
    ordering legible to the LLM -- without it a sorted list is just a
    list. ``N of M`` in the header is the tighten-or-raise signal.
    """
    if result.matched == 0:
        return (
            "query: 0 matches. Loosen `where`, drop `type`, or get_node a "
            "sample node to see the fields it actually carries."
        )
    lines = [f"query: {len(result.hits)} of {result.matched} match(es)."]
    for node in result.hits:
        lines.append(
            _render_hit_line(
                node,
                0,
                detail,
                (bodies or {}).get(node.id, ""),
                _order_annotation(node, order_by),
            )
        )
    if result.truncated:
        lines.append(
            f"... showing {len(result.hits)} of {result.matched}; tighten "
            "`where` or raise `limit` to see more."
        )
    return "\n".join(lines)


def _order_annotation(node: Node, order_by: Sequence[SortKey]) -> str:
    if not order_by:
        return ""
    parts = []
    for key in order_by:
        value = field_value(node, key.field)
        parts.append(f"{key.field}={'(none)' if value is None else value}")
    return " [" + ", ".join(parts) + "]"


def render_node_view(view: NodeView) -> str:
    """Deep single-node rendering for ``get_node``.

    Arrow direction is derived per edge: ``->`` when the focal node is the
    source, ``<-`` when it is the target -- so "Mira participated_in ->
    Siege" and "Siege participated_in <- Mira" read correctly from either
    side. A requested provenance section (WP7 ``include_provenance``) is
    appended, most-recent first, with body excerpts.
    """
    node = view.node
    stale = " [summary stale]" if node.summary_stale else ""
    lines = [
        f"{node.name} ({node.type}, id={node.id}){stale}",
        f"summary: {node.summary}",
    ]
    if node.story_time is not None:
        lines.append(f"story_time: {node.story_time}")
    if view.body:
        # The body IS the description (ADR 010); keep the LLM-facing label.
        lines.append(f"description: {view.body}")
    for key, value in sorted(node.fields.items()):
        lines.append(f"{key}: {value}")
    if view.edges:
        lines.append("edges:")
        for edge_type, pairs in view.edges.items():
            for edge, neighbor in pairs:
                arrow = "->" if edge.source == node.id else "<-"
                lines.append(
                    f"  {edge_type} {arrow} {neighbor.name} "
                    f"({neighbor.type}, id={neighbor.id})"
                )
    else:
        lines.append("edges: none")
    if view.provenance:
        lines.append(
            f"provenance ({len(view.provenance)} of {view.provenance_count}):"
        )
        for intent_node, excerpt in view.provenance:
            lines.append(f"  {intent_node.name} (id={intent_node.id}): {excerpt}")
    elif view.provenance_count > 0:
        lines.append(
            f"provenance: {view.provenance_count} intent record(s) touched this "
            "node (pass include_provenance=1-3 to view)"
        )
    return "\n".join(lines)


def render_path(path: Path | None) -> str:
    """``find_path`` rendering: a chain, or an honest 'no path'."""
    if path is None:
        return ("No path found within the length bound. Try raising max_length "
                "or widening edge_types.")
    if not path.edges:
        return f"{path.nodes[0].name} (start and target are the same node)"
    parts = [f"{path.nodes[0].name} ({path.nodes[0].type})"]
    for edge, node in zip(path.edges, path.nodes[1:], strict=True):
        arrow = "->" if edge.target == node.id else "<-"
        parts.append(f" —{edge.type}{arrow} {node.name} ({node.type})")
    return "".join(parts)
