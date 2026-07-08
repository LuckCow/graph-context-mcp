"""The turn-start context block (WP15, ADR 020).

The orchestrator opens every LLM turn with one block assembled from the
session: the scratchpad, the curated working set at its granularity
buckets, and the recent trail. This is the successor to the removed
per-response ``[project | focus | recent]`` header -- same idea, but
built ONCE per turn instead of echoed on every tool response, and the
model curates the content itself via the ``context`` tool.

Everything here is LLM-facing copy; the unit tests pin it like a golden.
Rules the block guarantees:

    * A session with nothing to say costs nothing -- empty scratchpad,
      working set, and recent trail render as ``""`` and the pipeline
      injects no event.
    * Entries whose node vanished (a human deleted it in Anytype) are
      silently skipped; the block must never fail a turn.
    * The block never exceeds its character budget by more than one
      capped scratchpad: over budget it degrades in order -- full-entry
      bodies first (each replaced by an explicit omission note), then
      summary-entry edge lines, then the recent trail. Full-entry edge
      lines survive to the end: the connections of the node being worked
      from are the block's whole point.
"""

from __future__ import annotations

from graph_context.domain import schema
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Detail, Node, NodeId
from graph_context.domain.session import WorkingSetEntry
from graph_context.interface.tools import Services

DEFAULT_BUDGET_CHARS = 3500
_MAX_EDGES_PER_ENTRY = 12  # a hub node must not flood the block

_HEADER = (
    "[session context -- assembled fresh each turn; curate it with the "
    "`context` tool]"
)
_BODY_OMITTED = (
    "    [body omitted: over context budget -- get_node fetches it]"
)


async def build_turn_context(
    services: Services, *, budget_chars: int = DEFAULT_BUDGET_CHARS
) -> str:
    """Render the session into the turn-opening block (``""`` if empty)."""
    session = services.session
    graph = services.repository.graph
    held = [
        entry for entry in session.working_set.entries
        if graph.has_node(entry.node_id)  # vanished nodes: skip, never crash
    ]
    recent = [i for i in session.recent.items if graph.has_node(i)]
    if not session.scratchpad and not held and not recent:
        return ""

    full_ids = [e.node_id for e in held if e.detail is Detail.FULL]
    bodies = await services.explorer.bodies_for(full_ids) if full_ids else {}

    # Degradation ladder: each level buys room by dropping the least
    # load-bearing content first. Level 3 is the floor -- scratchpad and
    # held-node lines are already capped by their own limits.
    for with_bodies, with_summary_edges, with_recent in (
        (True, True, True),
        (False, True, True),
        (False, False, True),
        (False, False, False),
    ):
        block = _render(
            services, held, recent, bodies,
            with_bodies=with_bodies,
            with_summary_edges=with_summary_edges,
            with_recent=with_recent,
        )
        if len(block) <= budget_chars:
            return block
    return block


def _render(
    services: Services,
    held: list[WorkingSetEntry],
    recent: list[NodeId],
    bodies: dict[NodeId, str],
    *,
    with_bodies: bool,
    with_summary_edges: bool,
    with_recent: bool,
) -> str:
    session = services.session
    graph = services.repository.graph
    lines = [_HEADER]
    if session.project:
        lines.append(f"project: {session.project}")
    if session.scratchpad:
        lines.append("scratchpad (yours; replace with context action='note'):")
        lines.append(f"  {session.scratchpad}")
    if held:
        lines.append(
            "working set (yours; curate with context action='hold'/'release'):"
        )
        # Full bucket first: the nodes being worked from lead the block.
        for entry in sorted(held, key=lambda e: e.detail is not Detail.FULL):
            node = graph.node(entry.node_id)
            stale = " [summary stale]" if node.summary_stale else ""
            lines.append(
                f"- {node.name} ({node.type}, id={node.id}) "
                f"[{entry.detail.value}]{stale}: {node.summary}"
            )
            if entry.detail is Detail.FULL:
                body = bodies.get(entry.node_id, "")
                if body:
                    lines.append(
                        f"    {body}" if with_bodies else _BODY_OMITTED
                    )
                lines.extend(_edge_lines(graph, node))
            elif with_summary_edges:
                lines.extend(_edge_lines(graph, node))
    if with_recent and recent:
        names = ", ".join(graph.node(i).name for i in recent)
        lines.append(f"recent (automatic trail): {names}")
    return "\n".join(lines)


def _edge_lines(graph: GraphIndex, node: Node) -> list[str]:
    """One hop of the graph, names only -- the associative memory: each
    neighbor is a thread the model can pull with explore(start=<id>)."""
    rendered = []
    for edge, neighbor in graph.neighbors(node.id):
        if neighbor.role in schema.INFRA_ROLES:
            continue  # bookkeeping stays invisible, as everywhere else
        arrow = "->" if edge.source == node.id else "<-"
        rendered.append(f"{edge.type} {arrow} {neighbor.name} ({neighbor.type})")
    if not rendered:
        return []
    rendered.sort()  # deterministic: index adjacency is set-backed
    shown = rendered[:_MAX_EDGES_PER_ENTRY]
    suffix = (
        [f"... {len(rendered) - _MAX_EDGES_PER_ENTRY} more (get_node shows all)"]
        if len(rendered) > _MAX_EDGES_PER_ENTRY
        else []
    )
    return ["    edges: " + "; ".join(shown + suffix)]
