"""Use-case: recording body revisions of tracked nodes (WP41, ADR 049).

The space's ``gc_tracked_types`` list (a Space Context property, read
off the INDEX by role -- the rule-engine pattern) names the node types
whose bodies get revision history. Every history rule -- segmentation,
normalized-hash identity, keyframe/delta encoding, blame, compaction --
lives in ``domain/revisions.py``; this service owns the side effects:

* ONE hidden ``gc_node_history`` sidecar per tracked node (found by its
  ``gc_history_of`` discriminator), body = the rendered log. Written
  straight through the repository like the recorders -- dedicated infra
  writer, never journalled (bookkeeping must not card).
* Baselines come from ``fetch_body`` output, NEVER from text we sent:
  the store normalizes markdown on write (ADR 010), and baselining on
  what it actually rendered is what keeps normalization drift from
  minting phantom revisions.
* Everything is compare-to-baseline, never event-driven: replays and
  restarts record nothing (``rebuild`` reloads baselines from the
  sidecars, then one catch-up compare per tracked node picks up edits
  made while the bot was down -- ADR 019's offline promise).

Two callers, both structural: the pipeline's turn boundary (bot writes,
with real attribution) and the ADR 044 change tick (everything else,
recorded as the generic ``human`` -- the API exposes no last-modified-by
identity; see the ADR's open spike).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from graph_context.domain import revisions
from graph_context.domain.models import NodeDraft, NodeId
from graph_context.domain.schema import INFRA_ROLES, Role
from graph_context.errors import (
    GraphContextError,
    LockedSectionsChanged,
    StaleSectionMark,
)
from graph_context.ports.graph_repository import GraphRepository

logger = logging.getLogger(__name__)

HISTORY_TYPE = "gc_node_history"
SIDECAR_SUMMARY = "Revision history (bot-maintained; do not edit)."


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class _Baseline:
    """One tracked node's in-memory history state."""

    sidecar_id: NodeId
    entries: tuple[revisions.LogEntry, ...]  # revisions + section marks
    records: tuple[revisions.RevisionRecord, ...]  # the revisions-only view
    hashes: tuple[str, ...]
    seq: int
    known_hashes: frozenset[str]


def _state(
    sidecar_id: NodeId, entries: tuple[revisions.LogEntry, ...]
) -> _Baseline:
    records = tuple(
        e for e in entries if isinstance(e, revisions.RevisionRecord)
    )
    usable = [r for r in records if r.kind != revisions.KIND_TRUNCATED]
    return _Baseline(
        sidecar_id=sidecar_id,
        entries=entries,
        records=records,
        hashes=revisions.current_hashes(records),
        seq=max((r.seq for r in usable), default=0),
        known_hashes=frozenset(revisions.texts_of(records)),
    )


class NodeHistorian:
    """Per-space revision recorder over the shared repository."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        now: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._repository = repository
        self._now = now
        self._baselines: dict[NodeId, _Baseline] = {}

    # -- configuration ---------------------------------------------------

    def tracked_types(self) -> tuple[str, ...]:
        """The space's tracked-type names, read off the index."""
        for node in self._repository.graph.nodes():
            if node.role is Role.SPACE_CONTEXT:
                return revisions.parse_tracked_types(
                    str(node.fields.get(revisions.FIELD_TRACKED_TYPES) or "")
                )
        return ()

    def is_tracked(self, node_id: NodeId) -> bool:
        """Whether a body change on this node should record. Infra nodes
        never track, whatever the human typed into the list."""
        graph = self._repository.graph
        if not graph.has_node(node_id):
            return False  # deleted/unknown: nothing to fetch a body from
        node = graph.node(node_id)
        if node.role in INFRA_ROLES:
            return False
        if node_id in self._baselines:
            return True
        tracked = {name.lower() for name in self.tracked_types()}
        return node.type.strip().lower() in tracked

    # -- startup ---------------------------------------------------------

    async def rebuild(self) -> None:
        """Reload baselines from the sidecars, then catch up on edits
        made while the bot was down (recorded as ``human``). Nothing
        double-records: a clean replay compares equal everywhere."""
        self._baselines.clear()
        sidecars = [
            node for node in self._repository.graph.nodes()
            if node.role is Role.NODE_HISTORY
        ]
        for sidecar in sidecars:
            target = str(
                sidecar.fields.get(revisions.FIELD_HISTORY_OF) or ""
            ).strip()
            if not target:
                logger.warning(
                    "historian: sidecar %s names no tracked node; skipping",
                    sidecar.id,
                )
                continue
            if target in self._baselines:
                logger.warning(
                    "historian: duplicate sidecar %s for %s; keeping the "
                    "first", sidecar.id, target,
                )
                continue
            parsed = revisions.parse_log(
                await self._repository.fetch_body(sidecar.id)
            )
            if parsed.skipped:
                logger.warning(
                    "historian: sidecar %s has %d unreadable log lines",
                    sidecar.id, parsed.skipped,
                )
            self._baselines[target] = _state(sidecar.id, parsed.entries)
        for node_id in list(self._baselines):
            if self._repository.graph.has_node(node_id):
                await self.record_external_revision(node_id)
        if self._baselines:
            logger.info(
                "historian: tracking %d nodes", len(self._baselines)
            )

    # -- recording -------------------------------------------------------

    async def record_bot_revision(
        self, node_id: NodeId, *, author_detail: str
    ) -> bool:
        """Record the current body as a model revision (the turn is the
        granularity -- one record however many PATCHes the turn made).
        Returns whether anything was recorded."""
        return await self._record(
            node_id, revisions.AUTHOR_MODEL, author_detail
        )

    async def record_external_revision(self, node_id: NodeId) -> bool:
        """Record the current body as a human revision (change tick /
        startup catch-up; identity is the generic ``human`` until the
        last-modified-by spike lands)."""
        return await self._record(
            node_id, revisions.AUTHOR_HUMAN, revisions.AUTHOR_HUMAN
        )

    async def sweep(self, changed: Iterable[NodeId]) -> None:
        """The change-tick entry point: record a human revision for every
        changed tracked node. Bot writes already advanced the baseline,
        so their tick shows no diff -- idempotent by construction."""
        for node_id in changed:
            if self.is_tracked(node_id):
                await self.record_external_revision(node_id)

    async def _record(
        self, node_id: NodeId, author_kind: str, author_detail: str
    ) -> bool:
        body = await self._repository.fetch_body(node_id)
        pairs = revisions.hash_sequence(body)
        baseline = self._baselines.get(node_id)
        if baseline is None and not pairs:
            return False  # tracking starts at the first body-bearing state
        hashes = tuple(h for h, _ in pairs)
        if baseline is not None and hashes == baseline.hashes:
            return False
        record = revisions.next_record(
            baseline.hashes if baseline else (),
            baseline.seq if baseline else 0,
            pairs,
            at=self._now(),
            author_kind=author_kind,
            author_detail=author_detail,
            known_hashes=baseline.known_hashes if baseline else frozenset(),
        )
        entries = revisions.compact(
            (*(baseline.entries if baseline else ()), record)
        )
        rendered = revisions.render_log(entries)
        if baseline is None:
            sidecar = await self._repository.create_node(NodeDraft(
                type=HISTORY_TYPE,
                name=f"History: {self._name_of(node_id)}",
                summary=SIDECAR_SUMMARY,
                fields={revisions.FIELD_HISTORY_OF: node_id},
                body=rendered,
            ))
            sidecar_id = sidecar.id
        else:
            sidecar_id = baseline.sidecar_id
            await self._repository.update_node(sidecar_id, body=rendered)
        self._baselines[node_id] = _state(sidecar_id, entries)
        logger.info(
            "historian: recorded %s revision %d of %s",
            author_kind, record.seq, node_id,
        )
        return True

    async def record_mark(
        self, node_id: NodeId, *, kind: str, block_hash: str,
        value: str, by: str,
    ) -> revisions.SectionState:
        """Append one status/intent mark (WP42) and return the block's
        folded state. Validates against the CURRENT baseline -- a hash
        that is no longer live means the page is stale, and the error
        says to reload rather than guessing lineage."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            raise GraphContextError(
                f"no revision history for {node_id!r} yet; marks attach "
                "to recorded sections only."
            )
        if kind not in (revisions.MARK_STATUS, revisions.MARK_INTENT):
            raise GraphContextError(
                f"unknown mark kind {kind!r}; allowed: "
                f"{revisions.MARK_STATUS}, {revisions.MARK_INTENT}."
            )
        allowed = (
            revisions.STATUS_VALUES if kind == revisions.MARK_STATUS
            else revisions.INTENT_VALUES
        )
        if value not in allowed:
            raise GraphContextError(
                f"unknown {kind} value {value!r}; allowed: "
                f"{', '.join(sorted(allowed))}."
            )
        if block_hash not in baseline.hashes:
            raise StaleSectionMark(
                f"section {block_hash} is not in the current version of "
                f"{self._name_of(node_id)!r} -- it changed since this view "
                "loaded; reload and re-apply the mark."
            )
        texts = revisions.texts_of(baseline.records)
        if len(texts.get(block_hash, "")) < revisions.MIN_BLAME_CHARS:
            raise GraphContextError(
                "this section is too short to mark (separators share "
                "hashes and cannot carry review state)."
            )
        current = revisions.section_states(baseline.entries).get(
            block_hash, revisions.SectionState()
        )
        already = (
            current.status if kind == revisions.MARK_STATUS
            else current.intent
        )
        if already == value:
            return current  # change-only: no log line, no PATCH
        mark = revisions.SectionMark(
            kind=kind, hash=block_hash, value=value, at=self._now(), by=by,
        )
        entries = revisions.compact((*baseline.entries, mark))
        await self._repository.update_node(
            baseline.sidecar_id, body=revisions.render_log(entries)
        )
        self._baselines[node_id] = _state(baseline.sidecar_id, entries)
        logger.info(
            "historian: marked %s %s=%s on %s", block_hash, kind, value,
            node_id,
        )
        return revisions.section_states(entries).get(
            block_hash, revisions.SectionState()
        )

    def _name_of(self, node_id: NodeId) -> str:
        graph = self._repository.graph
        return graph.node(node_id).name if graph.has_node(node_id) else node_id

    # -- read surface (Phase 3/4) ----------------------------------------

    def tracked_ids(self) -> frozenset[NodeId]:
        return frozenset(self._baselines)

    def history(
        self, node_id: NodeId
    ) -> tuple[revisions.RevisionRecord, ...]:
        baseline = self._baselines.get(node_id)
        return baseline.records if baseline else ()

    def blame(self, node_id: NodeId) -> dict[str, revisions.BlameEntry]:
        return revisions.blame(self.history(node_id))

    def entries(self, node_id: NodeId) -> tuple[revisions.LogEntry, ...]:
        baseline = self._baselines.get(node_id)
        return baseline.entries if baseline else ()

    def section_states(
        self, node_id: NodeId
    ) -> dict[str, revisions.SectionState]:
        baseline = self._baselines.get(node_id)
        return revisions.section_states(baseline.entries) if baseline else {}

    # -- the section guard (WP42) ------------------------------------------

    def check_body_update(self, node_id: NodeId, new_body: str) -> None:
        """NodeWriter's injected body-guard: raise if the new body drops
        a LOCKED section. Sync on purpose (in-memory fold, no awaits in
        the writer); untracked / never-recorded nodes pass freely."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            return
        states = revisions.section_states(baseline.entries)
        missing = revisions.missing_locked(states, new_body)
        if not missing:
            return
        texts = revisions.texts_of(baseline.records)
        raise LockedSectionsChanged(
            self._name_of(node_id),
            tuple((h, texts.get(h, "")[:60]) for h in missing),
        )
