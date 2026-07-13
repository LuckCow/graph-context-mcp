"""Session state: the curated working set, recent history, and scratchpad.

Three tiers of cross-turn context, from deliberate to automatic (WP15):

    * The **working set** is LLM-curated: a node is in it because the
      model *held* it there, at a granularity bucket -- ``full`` (body +
      connections echoed every turn) or ``summaries`` (one-liner). Tool
      activity never pushes into it.
    * **Recent history** is automatic: a most-recently-used ring of the
      last N *distinct* ids any read or write touched.
    * The **scratchpad** is free text the model replaces wholesale
      between turns -- intentions and open threads, not durable facts
      (those belong in the graph).

Queries that omit a start node default to the working-set top, falling
back to the most recently touched node.

Rules:
    * ``hold`` moves an already-present id to the top (re-holding
      re-levels it) instead of duplicating.
    * Bucket caps live HERE and only here: holding beyond the full slots
      demotes the oldest full entry to ``summaries``; overflowing the
      summary slots evicts the oldest summary entry (still reachable via
      recent history). The outcome reports both so tools can say so.
    * Recent history de-dupes: re-recording an id moves it to the front
      (so a composite create that re-touches a link target can't leave
      "A, B, A" in the trail).

Persistence (mirroring to the ``SessionContext`` meta-node) is an
infrastructure concern behind ``ports.SessionStore``; this module is pure.
"""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from graph_context.domain.models import Detail, NodeId

DEFAULT_FULL_SLOTS = 2
DEFAULT_SUMMARY_SLOTS = 6
DEFAULT_RECENT_SIZE = 12
SCRATCHPAD_MAX_CHARS = 2000  # over-cap is an error that teaches condensing

_HOLD_LEVELS = (Detail.SUMMARIES, Detail.FULL)


@dataclass(frozen=True, slots=True)
class WorkingSetEntry:
    node_id: NodeId
    detail: Detail = Detail.SUMMARIES


@dataclass(frozen=True, slots=True)
class HoldOutcome:
    """What a ``hold`` displaced, so the tool response can report it."""

    demoted: tuple[NodeId, ...] = ()  # full -> summaries (full slots overflowed)
    evicted: tuple[NodeId, ...] = ()  # dropped from the set (summary slots overflowed)


class WorkingSet:
    """LLM-curated nodes, each held at a granularity bucket.

    Ordered most-recently-held first; index 0 is the query-default top.
    """

    def __init__(
        self,
        full_slots: int = DEFAULT_FULL_SLOTS,
        summary_slots: int = DEFAULT_SUMMARY_SLOTS,
    ) -> None:
        if full_slots < 1 or summary_slots < 1:
            raise ValueError("working-set bucket sizes must be >= 1")
        self._full_slots = full_slots
        self._summary_slots = summary_slots
        self._entries: list[WorkingSetEntry] = []  # index 0 == most recent

    @property
    def top(self) -> NodeId | None:
        return self._entries[0].node_id if self._entries else None

    @property
    def entries(self) -> tuple[WorkingSetEntry, ...]:
        return tuple(self._entries)

    @property
    def full_slots(self) -> int:
        return self._full_slots

    @property
    def summary_slots(self) -> int:
        return self._summary_slots

    def hold(self, node_id: NodeId, detail: Detail = Detail.SUMMARIES) -> HoldOutcome:
        """Keep ``node_id`` at ``detail``, on top; re-holding re-levels."""
        if detail not in _HOLD_LEVELS:
            raise ValueError(
                "working-set entries are held at 'summaries' or 'full'"
            )
        self._take(node_id)
        self._entries.insert(0, WorkingSetEntry(node_id=node_id, detail=detail))
        return self._enforce_caps()

    def release(self, node_id: NodeId) -> bool:
        return self._take(node_id) is not None

    def clear(self) -> None:
        self._entries = []

    @classmethod
    def restore(
        cls,
        entries: list[WorkingSetEntry],
        full_slots: int = DEFAULT_FULL_SLOTS,
        summary_slots: int = DEFAULT_SUMMARY_SLOTS,
    ) -> WorkingSet:
        """Rebuild from a persisted snapshot (top-first), re-capping leniently."""
        working_set = cls(full_slots, summary_slots)
        working_set._entries = list(entries)
        working_set._enforce_caps()
        return working_set

    def __contains__(self, node_id: NodeId) -> bool:
        return any(e.node_id == node_id for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals -------------------------------------------------------

    def _take(self, node_id: NodeId) -> WorkingSetEntry | None:
        for i, entry in enumerate(self._entries):
            if entry.node_id == node_id:
                return self._entries.pop(i)
        return None

    def _enforce_caps(self) -> HoldOutcome:
        demoted: list[NodeId] = []
        evicted: list[NodeId] = []
        full = [e for e in self._entries if e.detail is Detail.FULL]
        while len(full) > self._full_slots:
            victim = full.pop()  # oldest full entry (furthest from the top)
            index = self._entries.index(victim)
            self._entries[index] = WorkingSetEntry(
                node_id=victim.node_id, detail=Detail.SUMMARIES
            )
            demoted.append(victim.node_id)
        summaries = [e for e in self._entries if e.detail is not Detail.FULL]
        while len(summaries) > self._summary_slots:
            victim = summaries.pop()
            self._entries.remove(victim)
            evicted.append(victim.node_id)
        return HoldOutcome(demoted=tuple(demoted), evicted=tuple(evicted))


class RecentHistory:
    """Breadcrumb trail of recently visited nodes (the automatic tier)."""

    def __init__(self, max_size: int = DEFAULT_RECENT_SIZE) -> None:
        self._items: deque[NodeId] = deque(maxlen=max_size)

    def record(self, node_id: NodeId) -> None:
        if self._items and self._items[-1] == node_id:
            return  # already on top -- nothing to do
        with contextlib.suppress(ValueError):
            self._items.remove(node_id)  # de-dupe: drop the older occurrence (if any)
        self._items.append(node_id)  # (re)insert at the most-recent end

    @property
    def items(self) -> tuple[NodeId, ...]:
        """Most recent first."""
        return tuple(reversed(self._items))

    @classmethod
    def restore(cls, items: list[NodeId], max_size: int = DEFAULT_RECENT_SIZE) -> RecentHistory:
        """Rebuild from a snapshot's ``items`` list (which is most-recent-first)."""
        history = cls(max_size)
        for node_id in reversed(items):  # items arrive most-recent-first
            history._items.append(node_id)
        return history


@dataclass(slots=True)
class SessionState:
    """The session's cross-turn context: working set, trail, scratchpad.

    ``mode`` is an opaque label (like ``project``): the orchestrator's
    active-mode name, persisted here so each session resumes in the mode
    it was left in (WP8). The domain never interprets it.
    """

    project: str | None = None
    working_set: WorkingSet = field(default_factory=WorkingSet)
    recent: RecentHistory = field(default_factory=RecentHistory)
    scratchpad: str = ""
    mode: str = ""

    def touch(self, node_id: NodeId) -> None:
        """Register that a read or write just involved ``node_id``."""
        self.recent.record(node_id)

    def default_start(self) -> NodeId | None:
        """The node queries default to: held first, else most recently touched."""
        if self.working_set.top is not None:
            return self.working_set.top
        items = self.recent.items
        return items[0] if items else None

    # -- persistence snapshot (WP3): plain-dict round-trip ----------------
    # The SessionStore port deals in these dicts; keeping (de)serialization
    # here means the JSON shape and the domain model can never drift apart.

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "version": 2,
            "project": self.project,
            "scratchpad": self.scratchpad,
            "mode": self.mode,
            "working_set": [
                {"node_id": e.node_id, "detail": e.detail.value}
                for e in self.working_set.entries
            ],
            "recent": list(self.recent.items),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> SessionState:
        """Lenient restore: a corrupt snapshot degrades to a fresh session
        field-by-field rather than crashing startup (WP3 contract). A v1
        snapshot's focus entries become summary-bucket holds."""
        raw_entries = data.get("working_set")
        if raw_entries is None:
            raw_entries = data.get("focus", [])  # v1: FocusEntry dicts
        working_set = WorkingSet.restore([
            WorkingSetEntry(
                node_id=str(e.get("node_id", "")),
                detail=_restore_detail(e.get("detail")),
            )
            for e in raw_entries if e.get("node_id")
        ])
        recent = RecentHistory.restore([str(i) for i in data.get("recent", [])])
        scratchpad = data.get("scratchpad")
        mode = data.get("mode")
        return cls(
            project=data.get("project"),
            working_set=working_set,
            recent=recent,
            scratchpad=scratchpad if isinstance(scratchpad, str) else "",
            mode=mode if isinstance(mode, str) else "",
        )


def _restore_detail(value: Any) -> Detail:
    """Snapshot leniency: unknown or unholdable levels degrade to summaries."""
    try:
        detail = Detail(value)
    except ValueError:
        return Detail.SUMMARIES
    return detail if detail in _HOLD_LEVELS else Detail.SUMMARIES
