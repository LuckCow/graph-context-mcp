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

from enum import StrEnum

from graph_context.application.node_reader import NodeView
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Node
from graph_context.domain.overview import GraphOverview
from graph_context.domain.pathfinding import Path
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreResult

_RECENT_SHOWN = 3
PROSE_EXCERPT_CHARS = 300  # WP3 starting point; tune by dogfooding


class Detail(StrEnum):
    NAMES = "names"
    SUMMARIES = "summaries"
    FULL = "full"


def render_context_header(session: SessionState, graph: GraphIndex) -> str:
    project = session.project or "-"
    focus = ", ".join(
        _name_with_type(graph, entry.node_id, pinned=entry.pinned)
        for entry in session.focus.entries
        if graph.has_node(entry.node_id)
    )
    recent = ", ".join(
        graph.node(node_id).name
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
        return "overview: no story nodes yet -- use create_node to begin a world."
    types = ", ".join(f"{tc.type} {tc.count}" for tc in overview.type_counts)
    lines = [
        f"overview: {overview.total_story_nodes} story nodes across "
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


def render_explore_result(result: ExploreResult, detail: Detail) -> str:
    lines = [_render_hit_line(hit.node, hit.depth, detail) for hit in result.hits]
    if result.truncated:
        lines.append(
            "... result limit reached; narrow filters or raise `limit` to see more."
        )
    return "\n".join(lines)


def _render_hit_line(node: Node, depth: int, detail: Detail) -> str:
    indent = "  " * depth
    base = f"{indent}- {node.name} ({node.type}, id={node.id})"
    if detail is Detail.NAMES:
        return base
    stale = " [summary stale]" if node.summary_stale else ""
    if detail is Detail.SUMMARIES:
        return f"{base}{stale}: {node.summary}"
    description = f"\n{indent}    {node.description}" if node.description else ""
    return f"{base}{stale}: {node.summary}{description}"


def _name_with_type(graph: GraphIndex, node_id: str, *, pinned: bool) -> str:
    node = graph.node(node_id)
    pin_mark = "*" if pinned else ""
    return f"{node.name}{pin_mark} ({node.type})"


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
    if node.description:
        lines.append(f"description: {node.description}")
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
        lines.append("prose:")
        for prose_node, excerpt in view.prose:
            lines.append(f"  {prose_node.name} (id={prose_node.id}): {excerpt}")
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
