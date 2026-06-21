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

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Node
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreResult

_RECENT_SHOWN = 3


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


def render_explore_result(result: ExploreResult, detail: Detail) -> str:
    lines = [_render_hit_line(hit.node, hit.depth, detail) for hit in result.hits]
    if result.truncated:
        lines.append(
            "... result limit reached; narrow filters or raise `limit` to see more."
        )
    return "\n".join(lines)


def _render_hit_line(node: Node, depth: int, detail: Detail) -> str:
    indent = "  " * depth
    base = f"{indent}- {node.name} ({node.type.value}, id={node.id})"
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
    return f"{node.name}{pin_mark} ({node.type.value})"
