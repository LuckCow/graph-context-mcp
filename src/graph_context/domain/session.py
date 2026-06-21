"""Session state: the focus stack and recent-history breadcrumbs.

The proposal's "context echo" depends on this module: every tool response
renders a header from :class:`SessionState`, and queries that omit a start
node default to ``focus.top``. Scene work touches several entities at
once, so the working set is a small *stack* (default 6) rather than a
single current-node pointer.

Rules:
    * ``push`` moves an already-present id to the top instead of
      duplicating it (most-recently-touched ordering).
    * Pinned entries are never evicted by overflow; if everything is
      pinned the stack may temporarily exceed ``max_size`` (the user asked
      for exactly that working set -- honour it).
    * Recent history is an append-only ring of the last N visited ids,
      skipping consecutive duplicates.

Persistence (mirroring to the ``SessionContext`` meta-node) is an
infrastructure concern behind ``ports.SessionStore``; this module is pure.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from graph_context.domain.models import NodeId

DEFAULT_FOCUS_SIZE = 6
DEFAULT_RECENT_SIZE = 12


@dataclass(frozen=True, slots=True)
class FocusEntry:
    node_id: NodeId
    pinned: bool = False


class FocusStack:
    """Ordered working set of recently touched / explicitly focused nodes."""

    def __init__(self, max_size: int = DEFAULT_FOCUS_SIZE) -> None:
        if max_size < 1:
            raise ValueError("focus stack max_size must be >= 1")
        self._max_size = max_size
        self._entries: list[FocusEntry] = []  # index 0 == top of stack

    @property
    def top(self) -> NodeId | None:
        return self._entries[0].node_id if self._entries else None

    @property
    def entries(self) -> tuple[FocusEntry, ...]:
        return tuple(self._entries)

    def push(self, node_id: NodeId) -> None:
        """Put ``node_id`` on top, preserving its pinned state if present."""
        existing = self._take(node_id)
        self._entries.insert(0, existing or FocusEntry(node_id=node_id))
        self._evict_overflow()

    def pin(self, node_id: NodeId) -> None:
        self._set_pinned(node_id, True)

    def unpin(self, node_id: NodeId) -> None:
        self._set_pinned(node_id, False)

    def remove(self, node_id: NodeId) -> None:
        self._take(node_id)

    def clear(self, *, keep_pinned: bool = True) -> None:
        self._entries = [e for e in self._entries if keep_pinned and e.pinned]

    def __contains__(self, node_id: NodeId) -> bool:
        return any(e.node_id == node_id for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals -------------------------------------------------------

    def _take(self, node_id: NodeId) -> FocusEntry | None:
        for i, entry in enumerate(self._entries):
            if entry.node_id == node_id:
                return self._entries.pop(i)
        return None

    def _set_pinned(self, node_id: NodeId, pinned: bool) -> None:
        for i, entry in enumerate(self._entries):
            if entry.node_id == node_id:
                self._entries[i] = FocusEntry(node_id=node_id, pinned=pinned)
                return

    def _evict_overflow(self) -> None:
        while len(self._entries) > self._max_size:
            # Never evict the just-pushed top entry; that would turn the
            # push into a silent no-op. Search victims bottom-up below it.
            victim = next(
                (e for e in reversed(self._entries[1:]) if not e.pinned), None
            )
            if victim is None:
                return  # everything else pinned: allow temporary overflow
            self._entries.remove(victim)


class RecentHistory:
    """Breadcrumb trail of recently visited nodes (beyond the focus stack)."""

    def __init__(self, max_size: int = DEFAULT_RECENT_SIZE) -> None:
        self._items: deque[NodeId] = deque(maxlen=max_size)

    def record(self, node_id: NodeId) -> None:
        if self._items and self._items[-1] == node_id:
            return  # skip consecutive duplicates
        self._items.append(node_id)

    @property
    def items(self) -> tuple[NodeId, ...]:
        """Most recent first."""
        return tuple(reversed(self._items))


@dataclass(slots=True)
class SessionState:
    """Everything the context header renders and query defaults read."""

    project: str | None = None
    focus: FocusStack = field(default_factory=FocusStack)
    recent: RecentHistory = field(default_factory=RecentHistory)

    def touch(self, node_id: NodeId) -> None:
        """Register that a read or write just involved ``node_id``."""
        self.focus.push(node_id)
        self.recent.record(node_id)
