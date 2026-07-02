"""Presenters: turning domain results into the strings tools return.

The proposal's "context echo" lives here: every tool response begins with
a compact header rendered from :class:`SessionState`, e.g.::

    [project: Ashfall | focus: Mira (Character), The Undercroft (Location) | recent: Siege of Brakk]

Detail levels (``names`` / ``summaries`` / ``full``) are an interface
concern -- the traversal always returns full nodes; how much of each node
reaches the LLM's context window is decided at the edge. Keeping this out
of the domain means response-budget tuning never touches tested logic.

Nodes referenced by the session but missing from the graph (deleted, or
removed by an out-of-band human edit before a resync) are skipped
silently: the header must never crash a tool response.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from graph_context.application.node_reader import NodeView
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Node
from graph_context.domain.overview import GraphOverview
from graph_context.domain.pathfinding import Path
from graph_context.domain.schema import INFRA_ROLES
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreResult

_RECENT_SHOWN = 3
_FOCUS_SHOWN = 3  # header shows the top of the stack, not the whole working set
_HEADER_NAME_CHARS = 32  # ellipsize pathological titles; the header is an echo
PROSE_EXCERPT_CHARS = 300  # WP3 starting point; tune by dogfooding


class Detail(StrEnum):
    NAMES = "names"
    SUMMARIES = "summaries"
    FULL = "full"


def render_context_header(session: SessionState, graph: GraphIndex) -> str:
    project = session.project or "-"
    focus_entries = [
        entry for entry in session.focus.entries if graph.has_node(entry.node_id)
    ]
    focus_parts = [
        _name_with_type(graph, entry.node_id, pinned=entry.pinned)
        for entry in focus_entries[:_FOCUS_SHOWN]
    ]
    if len(focus_entries) > _FOCUS_SHOWN:
        focus_parts.append(f"(+{len(focus_entries) - _FOCUS_SHOWN} more)")
    focus = ", ".join(focus_parts)
    recent = ", ".join(
        _truncate(graph.node(node_id).name)
        for node_id in session.recent.items[:_RECENT_SHOWN]
        if graph.has_node(node_id)
    )
    return f"[project: {project} | focus: {focus or '-'} | recent: {recent or '-'}]"


def render_overview(overview: GraphOverview) -> str:
    """Render the cold-start entry-point map (``context action='overview'``).

    Ids are the last token before the colon on every hub line, so the LLM
    can copy one straight into ``explore`` / ``get_node`` / ``focus``.
    """
    if overview.total_story_nodes == 0:
        return "overview: no nodes yet -- use create_node to add the first one."
    types = ", ".join(f"{tc.type} {tc.count}" for tc in overview.type_counts)
    lines = [
        f"overview: {overview.total_story_nodes} nodes across "
        f"{len(overview.type_counts)} types (derived entry-point map).",
        f"types: {types}",
        "entry points (highest-degree nodes; pass an id to explore, "
        "get_node, or context action='focus'):",
    ]
    for hub in overview.hubs:
        node = hub.node
        stale = " [summary stale]" if node.summary_stale else ""
        lines.append(
            f"- {node.name} ({node.type}, id={node.id}){stale}: {node.summary}"
        )
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


def _render_hit_line(node: Node, depth: int, detail: Detail, body: str = "") -> str:
    indent = "  " * depth
    base = f"{indent}- {node.name} ({node.type}, id={node.id})"
    if detail is Detail.NAMES:
        return base
    stale = " [summary stale]" if node.summary_stale else ""
    if detail is Detail.SUMMARIES or not body:
        return f"{base}{stale}: {node.summary}"
    return f"{base}{stale}: {node.summary}\n{indent}    {body}"


def _name_with_type(graph: GraphIndex, node_id: str, *, pinned: bool) -> str:
    node = graph.node(node_id)
    pin_mark = "*" if pinned else ""
    return f"{_truncate(node.name)}{pin_mark} ({node.type})"


def _truncate(name: str, limit: int = _HEADER_NAME_CHARS) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def render_node_view(view: NodeView) -> str:
    """Deep single-node rendering for ``get_node``.

    Arrow direction is derived per edge: ``->`` when the focal node is the
    source, ``<-`` when it is the target -- so "Mira participated_in ->
    Siege" and "Siege participated_in <- Mira" read correctly from either
    side. A requested prose section (WP3 ``include_prose``) is appended,
    most-recent first, with body excerpts.
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
    if view.prose:
        lines.append(f"prose ({len(view.prose)} of {view.prose_count}):")
        for prose_node, excerpt in view.prose:
            lines.append(f"  {prose_node.name} (id={prose_node.id}): {excerpt}")
    elif view.prose_count > 0:
        lines.append(
            f"prose: {view.prose_count} passage(s) reference this node "
            "(pass include_prose=1-3 to view)"
        )
    elif node.role not in INFRA_ROLES:
        # An explicit signal so "no prior prose" is never an inference.
        lines.append("prose: none recorded")
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
