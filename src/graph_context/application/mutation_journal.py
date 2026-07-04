"""MutationJournal: writers report what they touched, at the source (WP7).

ADR 008 makes provenance a harness responsibility -- but only the writers
know which nodes a turn created or modified, so they report it here rather
than anyone parsing presenter output. The journal is deliberately dumb:
an append-only list with a ``drain()``.

Two deployments, one seam:

* The MCP server wires the default :class:`NullJournal` -- it has no turn
  boundary to drain at, and an accumulating journal would just leak.
* The orchestrator wires a real :class:`MutationJournal` and drains it at
  each turn's end; the drained records feed the ``IntentRecorder``.

The IntentRecorder itself writes through the repository directly (not the
journalled writers), so recording provenance never journals itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graph_context.domain.models import NodeId


@dataclass(frozen=True, slots=True)
class MutationRecord:
    """One touched node: ``action`` is ``"created"`` or ``"modified"``."""

    node_id: NodeId
    action: str


@dataclass(slots=True)
class MutationJournal:
    """Per-turn collector; ``drain()`` returns and clears, keeping order.

    Records are deduplicated on drain (first action wins: a node created
    and then modified in the same turn is simply "created").
    """

    _records: list[MutationRecord] = field(default_factory=list)

    def created(self, node_id: NodeId) -> None:
        self._records.append(MutationRecord(node_id, "created"))

    def modified(self, node_id: NodeId) -> None:
        self._records.append(MutationRecord(node_id, "modified"))

    def drain(self) -> tuple[MutationRecord, ...]:
        seen: dict[NodeId, MutationRecord] = {}
        for record in self._records:
            seen.setdefault(record.node_id, record)
        self._records.clear()
        return tuple(seen.values())


class NullJournal(MutationJournal):
    """The MCP server's journal: reports vanish (no turn boundary exists)."""

    def created(self, node_id: NodeId) -> None:  # noqa: D102
        pass

    def modified(self, node_id: NodeId) -> None:  # noqa: D102
        pass
