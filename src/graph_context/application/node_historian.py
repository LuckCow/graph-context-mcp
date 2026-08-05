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
from collections.abc import Callable, Iterable, Sequence
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


@dataclass(frozen=True, slots=True)
class MarkRequest:
    """One requested status/intent mark -- the unit of the batch write
    (WP48): a multi-paragraph selection arrives as one request list and
    lands in ONE sidecar rewrite. ``start``/``end`` (both or neither)
    are token indices over the block's raw text."""

    kind: str
    block_hash: str
    value: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class _SurfaceWording:
    """Errors are prompts: the shared anchor checks keep their
    per-surface wording (marks vs comments) through these nouns."""

    unit: str          # names the range in the range messages
    verb: str          # what the caller is doing to the section
    carries: str       # what a word-free block cannot carry
    stale_action: str  # what to redo after reloading a stale view


_MARK_WORDING = _SurfaceWording(
    unit="mark", verb="mark", carries="review state",
    stale_action="re-apply the mark",
)
_COMMENT_WORDING = _SurfaceWording(
    unit="comment", verb="comment on", carries="comments",
    stale_action="re-add the comment",
)


class _DerivedViews:
    """Lazy compute-once folds over one baseline's immutable log.

    The folds (``token_states``/``blame``/``word_token_authors``/
    ``comment_states``) walk every revision with difflib lineage
    matching -- ~1s near the log cap -- and the prose page's doc wire
    reads five of them per payload. Memoizing here makes each fold run
    ONCE per log state: every write path replaces the whole
    :class:`_Baseline` via :func:`_state`, so a fresh baseline is a
    fresh empty memo and invalidation is correct by construction.
    Cached results are shared between callers -- treat them as frozen.
    """

    __slots__ = (
        "_entries", "_records", "_known_texts",
        "_token", "_badges", "_blame", "_word_authors", "_comments",
    )

    def __init__(
        self,
        entries: tuple[revisions.LogEntry, ...],
        records: tuple[revisions.RevisionRecord, ...],
        known_texts: dict[str, str],
    ) -> None:
        self._entries = entries
        self._records = records
        self._known_texts = known_texts
        self._token: dict[str, revisions.TokenState] | None = None
        self._badges: dict[str, revisions.SectionState] | None = None
        self._blame: dict[str, revisions.BlameEntry] | None = None
        self._word_authors: dict[str, tuple[str, ...]] | None = None
        self._comments: tuple[revisions.CommentState, ...] | None = None

    def seed_records_views(self, other: _DerivedViews) -> None:
        """Carry the records-only folds from a predecessor baseline
        whose ``records`` compare equal (marks/comments-only writes):
        ``blame`` and ``word_token_authors`` fold records alone, so
        their results are unchanged by construction."""
        self._blame = other._blame
        self._word_authors = other._word_authors

    def token_states(self) -> dict[str, revisions.TokenState]:
        if self._token is None:
            self._token = revisions.token_states(self._entries)
        return self._token

    def section_states(self) -> dict[str, revisions.SectionState]:
        if self._badges is None:
            self._badges = revisions.badges_of(self.token_states())
        return self._badges

    def blame(self) -> dict[str, revisions.BlameEntry]:
        if self._blame is None:
            self._blame = revisions.blame(self._records)
        return self._blame

    def word_token_authors(self) -> dict[str, tuple[str, ...]]:
        # Folding all records equals folding the usable view: the log
        # walk yields no step for the compaction marker.
        if self._word_authors is None:
            self._word_authors = revisions.word_token_authors(self._records)
        return self._word_authors

    def comment_states(self) -> tuple[revisions.CommentState, ...]:
        if self._comments is None:
            self._comments = revisions.comment_states(self._entries)
        return self._comments

    def locked_runs(self) -> dict[str, tuple[str, ...]]:
        return revisions.locked_runs(self.token_states(), self._known_texts)

    def missing_locked(
        self, new_body: str
    ) -> tuple[tuple[str, str], ...]:
        return revisions.missing_locked(
            self.token_states(), self._known_texts, new_body
        )


@dataclass(slots=True)
class _Baseline:
    """One tracked node's in-memory history state."""

    sidecar_id: NodeId
    entries: tuple[revisions.LogEntry, ...]  # revisions + section marks
    records: tuple[revisions.RevisionRecord, ...]  # the revisions-only view
    hashes: tuple[str, ...]
    seq: int
    known_texts: dict[str, str]  # texts_of(records): hash -> raw text
    views: _DerivedViews  # lazy fold memo, dies with the baseline


def _state(
    sidecar_id: NodeId,
    entries: tuple[revisions.LogEntry, ...],
    previous: _Baseline | None = None,
) -> _Baseline:
    records = tuple(
        e for e in entries if isinstance(e, revisions.RevisionRecord)
    )
    usable = revisions.usable_records(records)
    known_texts = revisions.texts_of(records)
    views = _DerivedViews(entries, records, known_texts)
    if previous is not None and previous.records == records:
        views.seed_records_views(previous.views)
    return _Baseline(
        sidecar_id=sidecar_id,
        entries=entries,
        records=records,
        hashes=revisions.current_hashes(records),
        seq=max((r.seq for r in usable), default=0),
        known_texts=known_texts,
        views=views,
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
        # Fired after every sidecar write (revision or mark) with the
        # tracked node's id -- the prose page's live-update signal
        # (WP48). Injected by the composition root (the bridge's
        # registration); None = no listener. Must not raise.
        self.on_record: Callable[[NodeId], None] | None = None

    def _notify(self, node_id: NodeId) -> None:
        if self.on_record is not None:
            self.on_record(node_id)

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

    async def record_external_revision(
        self, node_id: NodeId, *, detail: str = revisions.AUTHOR_HUMAN
    ) -> bool:
        """Record the current body as a human revision (change tick /
        startup catch-up; identity is the generic ``human`` until the
        last-modified-by spike lands). Surfaces that DO know who edited
        pass ``detail`` -- the prose page stamps ``human:prose-page``."""
        return await self._record(
            node_id, revisions.AUTHOR_HUMAN, detail
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
        """Record the current body state; returns whether the sidecar
        was rewritten. Consecutive human revisions COALESCE (WP44): a
        human recording whose pending human tail is still the last log
        entry re-records against the pre-tail base (same seq), so one
        editing session is one revision; a model revision or any mark
        solidifies the tail by displacing it as last entry.

        A hash-equal body whose stored texts drifted from the live raw
        text (a store rewrite, or a pre-ADR-054 normalized-text log)
        still records: an all-equal delta that only refreshes texts."""
        body = await self._repository.fetch_body(node_id)
        pairs = revisions.body_blocks(body)
        baseline = self._baselines.get(node_id)
        if baseline is None and not pairs:
            return False  # tracking starts at the first body-bearing state
        hashes = tuple(h for h, _ in pairs)
        if (
            baseline is not None
            and hashes == baseline.hashes
            and all(
                baseline.known_texts.get(h) == text for h, text in pairs
            )
        ):
            return False  # BEFORE roll-up: idle ticks must not rewrite
        base = baseline
        if (
            baseline is not None
            and author_kind == revisions.AUTHOR_HUMAN
            and (trimmed := revisions.rollup_base(baseline.entries)) is not None
        ):
            base = _state(baseline.sidecar_id, trimmed)
            if hashes == base.hashes and all(
                base.known_texts.get(h) == text for h, text in pairs
            ):
                # The human undid the whole pending revision: it
                # disappears rather than becoming an empty delta.
                await self._write_log(
                    node_id, base.sidecar_id, trimmed,
                    "historian: human edits reverted; dropped the pending "
                    "revision of %s", node_id,
                )
                return True
        record = revisions.next_record(
            base.hashes if base else (),
            base.seq if base else 0,
            pairs,
            at=self._now(),
            author_kind=author_kind,
            author_detail=author_detail,
            known_texts=base.known_texts if base else {},
        )
        entries = revisions.compact(
            (*(base.entries if base else ()), record)
        )
        if baseline is None:
            # Create is not the epilogue's PATCH: keep its tail inline
            # (routing it through _write_log would add a second write).
            sidecar = await self._repository.create_node(NodeDraft(
                type=HISTORY_TYPE,
                name=f"History: {self._name_of(node_id)}",
                summary=SIDECAR_SUMMARY,
                fields={revisions.FIELD_HISTORY_OF: node_id},
                body=revisions.render_log(entries),
            ))
            self._baselines[node_id] = _state(sidecar.id, entries)
            logger.info(
                "historian: recorded %s revision %d of %s",
                author_kind, record.seq, node_id,
            )
            self._notify(node_id)
        else:
            await self._write_log(
                node_id, baseline.sidecar_id, entries,
                "historian: recorded %s revision %d of %s",
                author_kind, record.seq, node_id,
            )
        return True

    def _validate_anchor(
        self, node_id: NodeId, baseline: _Baseline, *,
        block_hash: str, start: int | None, end: int | None,
        wording: _SurfaceWording,
    ) -> None:
        """THE anchor checks both write surfaces share -- hash still
        live, block has words, range shape and bounds -- in the order
        they always ran; ``wording`` keeps each surface's exact error
        prose."""
        w = wording
        if block_hash not in baseline.hashes:
            raise StaleSectionMark(
                f"section {block_hash} is not in the current version "
                f"of {self._name_of(node_id)!r} -- it changed since "
                f"this view loaded; reload and {w.stale_action}."
            )
        text = baseline.known_texts.get(block_hash, "")
        if not revisions.has_words(text):
            raise GraphContextError(
                f"this section has no words to {w.verb} (separators "
                f"share hashes and cannot carry {w.carries})."
            )
        if (start is None) != (end is None):
            raise GraphContextError(
                f"a ranged {w.unit} needs BOTH start and end (token "
                f"indices); omit both to {w.verb} the whole section."
            )
        token_count = len(revisions.block_tokens(text))
        if start is not None and end is not None and not (
            0 <= start < end <= token_count
        ):
            raise GraphContextError(
                f"{w.unit} range {start}..{end} is outside this "
                f"section's 0..{token_count} tokens; reload and "
                "reselect."
            )

    async def _write_log(
        self, node_id: NodeId, sidecar_id: NodeId,
        entries: tuple[revisions.LogEntry, ...],
        log_msg: str, *log_args: object,
    ) -> None:
        """The shared write epilogue: PATCH the sidecar with the
        rendered log, refresh the baseline, log, notify the page.
        Callers compact first where they mean to -- the roll-up drop
        deliberately writes the uncompacted trimmed log."""
        await self._repository.update_node(
            sidecar_id, body=revisions.render_log(entries)
        )
        self._baselines[node_id] = _state(
            sidecar_id, entries, previous=self._baselines.get(node_id)
        )
        logger.info(log_msg, *log_args)
        self._notify(node_id)

    async def record_mark(
        self, node_id: NodeId, *, kind: str, block_hash: str,
        value: str, by: str,
        start: int | None = None, end: int | None = None,
    ) -> revisions.SectionState:
        """Append one status/intent mark (WP42; token-ranged since WP46)
        and return the block's folded BADGE state. A one-item
        :meth:`record_marks`."""
        states = await self.record_marks(
            node_id,
            requests=(MarkRequest(
                kind=kind, block_hash=block_hash, value=value,
                start=start, end=end,
            ),),
            by=by,
        )
        return states.get(block_hash, revisions.SectionState())

    async def record_marks(
        self, node_id: NodeId, *,
        requests: Sequence[MarkRequest], by: str,
    ) -> dict[str, revisions.SectionState]:
        """Append a BATCH of status/intent marks in one sidecar rewrite
        (WP48: a multi-paragraph selection is one gesture -> one write,
        one compaction, one PATCH). Every request validates against the
        CURRENT baseline before anything lands -- all-or-nothing; a
        hash that is no longer live means the page is stale, and the
        error says to reload rather than guessing lineage. Requests
        whose slice already folds to the value drop out silently
        (change-only, tracked across the batch). Returns the folded
        badge per requested hash."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            raise GraphContextError(
                f"no revision history for {node_id!r} yet; marks attach "
                "to recorded sections only."
            )
        folded = baseline.views.token_states()
        simulated: dict[tuple[str, str], list[str]] = {}
        for block, state in folded.items():
            simulated[(revisions.MARK_STATUS, block)] = list(state.status)
            simulated[(revisions.MARK_INTENT, block)] = list(state.intent)
        marks: list[revisions.SectionMark] = []
        for request in requests:
            kind, value = request.kind, request.value
            block_hash, start, end = (
                request.block_hash, request.start, request.end
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
            self._validate_anchor(
                node_id, baseline, block_hash=block_hash,
                start=start, end=end, wording=_MARK_WORDING,
            )
            target = simulated.get((kind, block_hash))
            if target is not None:
                applied, changed = revisions.apply_mark(
                    target, value,
                    start if start is not None else -1,
                    end if end is not None else -1,
                )
                if applied and not changed:
                    continue  # change-only: no log line, no PATCH
            marks.append(revisions.SectionMark(
                kind=kind, hash=block_hash, value=value,
                at=self._now(), by=by,
                start=start if start is not None else -1,
                end=end if end is not None else -1,
            ))
        if marks:
            await self._write_log(
                node_id, baseline.sidecar_id,
                revisions.compact((*baseline.entries, *marks)),
                "historian: marked %d section(s) on %s",
                len(marks), node_id,
            )
        states = self._baselines[node_id].views.section_states()
        return {
            request.block_hash: states.get(
                request.block_hash, revisions.SectionState()
            )
            for request in requests
        }

    # -- comments (WP50, ADR 056) ----------------------------------------

    async def record_comment(
        self, node_id: NodeId, *, block_hash: str, text: str, by: str,
        start: int | None = None, end: int | None = None,
    ) -> revisions.CommentState:
        """Append one comment on a selection (``start``/``end`` are token
        indices over the block's raw text; both or neither) and return
        its folded state. Same discipline as marks: validate against the
        CURRENT baseline, one compaction, one sidecar rewrite; an
        identical comment written in the same second folds to the same
        id and drops out as a change-only no-op."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            raise GraphContextError(
                f"no revision history for {node_id!r} yet; comments "
                "attach to recorded sections only."
            )
        cleaned = text.strip()
        if not cleaned:
            raise GraphContextError("a comment needs text.")
        if len(cleaned) > revisions.COMMENT_TEXT_CAP:
            raise GraphContextError(
                f"comment text is {len(cleaned)} chars; the cap is "
                f"{revisions.COMMENT_TEXT_CAP}."
            )
        self._validate_anchor(
            node_id, baseline, block_hash=block_hash,
            start=start, end=end, wording=_COMMENT_WORDING,
        )
        at = self._now()
        cid = revisions.comment_id(at, by, block_hash, cleaned)
        live = {
            c.id: c for c in baseline.views.comment_states()
        }
        if cid in live:
            return live[cid]  # change-only: no log line, no PATCH
        entry = revisions.CommentEntry(
            id=cid, hash=block_hash, text=cleaned, at=at, by=by,
            start=start if start is not None else -1,
            end=end if end is not None else -1,
        )
        entries = revisions.compact((*baseline.entries, entry))
        await self._write_log(
            node_id, baseline.sidecar_id, entries,
            "historian: comment %s on %s", cid, node_id,
        )
        return next(
            c for c in self._baselines[node_id].views.comment_states()
            if c.id == cid
        )

    async def set_comment_state(
        self, node_id: NodeId, *, comment_id: str, value: str, by: str,
    ) -> revisions.CommentState | None:
        """Transition a comment: ``addressed`` (the model acted on it)
        or ``resolved`` (the human closed it). Returns the folded state,
        or None once resolved. The unknown-id error lists the live ids
        -- its consumer may be the model (errors are prompts)."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            raise GraphContextError(
                f"no revision history for {node_id!r} yet."
            )
        if value not in revisions.COMMENT_STATE_VALUES:
            raise GraphContextError(
                f"unknown comment state {value!r}; allowed: "
                f"{', '.join(sorted(revisions.COMMENT_STATE_VALUES))}."
            )
        live = {
            c.id: c for c in baseline.views.comment_states()
        }
        target = live.get(comment_id)
        if target is None:
            listed = ", ".join(f"#{cid}" for cid in live) or "none"
            raise GraphContextError(
                f"no live comment {comment_id!r} on "
                f"{self._name_of(node_id)!r}; live comments: {listed}."
            )
        if value == revisions.COMMENT_ADDRESSED and (
            target.state == revisions.COMMENT_ADDRESSED
        ):
            return target  # change-only: no log line, no PATCH
        entry = revisions.CommentStateEntry(
            id=comment_id, value=value, at=self._now(), by=by,
        )
        entries = revisions.compact((*baseline.entries, entry))
        await self._write_log(
            node_id, baseline.sidecar_id, entries,
            "historian: comment %s %s on %s", comment_id, value, node_id,
        )
        for state in self._baselines[node_id].views.comment_states():
            if state.id == comment_id:
                return state
        return None  # resolved

    async def edit_comment(
        self, node_id: NodeId, *, comment_id: str, text: str, by: str,
    ) -> revisions.CommentState:
        """Rewrite a live comment's text (ADR 056 amendment): one
        ``comment_edit`` line, folded last-wins -- id, anchor, and
        creation stamps stay; an ``addressed`` comment reopens (the
        model acted on the old wording). Unchanged text is a change-only
        no-op; the unknown-id error lists the live ids."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            raise GraphContextError(
                f"no revision history for {node_id!r} yet."
            )
        cleaned = text.strip()
        if not cleaned:
            raise GraphContextError("a comment needs text.")
        if len(cleaned) > revisions.COMMENT_TEXT_CAP:
            raise GraphContextError(
                f"comment text is {len(cleaned)} chars; the cap is "
                f"{revisions.COMMENT_TEXT_CAP}."
            )
        live = {
            c.id: c for c in baseline.views.comment_states()
        }
        target = live.get(comment_id)
        if target is None:
            listed = ", ".join(f"#{cid}" for cid in live) or "none"
            raise GraphContextError(
                f"no live comment {comment_id!r} on "
                f"{self._name_of(node_id)!r}; live comments: {listed}."
            )
        if target.text == cleaned:
            return target  # change-only: no log line, no PATCH
        entry = revisions.CommentEditEntry(
            id=comment_id, text=cleaned, at=self._now(), by=by,
        )
        entries = revisions.compact((*baseline.entries, entry))
        await self._write_log(
            node_id, baseline.sidecar_id, entries,
            "historian: comment %s edited on %s", comment_id, node_id,
        )
        return next(
            c for c in self._baselines[node_id].views.comment_states()
            if c.id == comment_id
        )

    def comments(self, node_id: NodeId) -> tuple[revisions.CommentState, ...]:
        """The node's live comments (open + addressed), current anchors."""
        baseline = self._baselines.get(node_id)
        return baseline.views.comment_states() if baseline else ()

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
        baseline = self._baselines.get(node_id)
        return baseline.views.blame() if baseline else {}

    def entries(self, node_id: NodeId) -> tuple[revisions.LogEntry, ...]:
        baseline = self._baselines.get(node_id)
        return baseline.entries if baseline else ()

    def section_states(
        self, node_id: NodeId
    ) -> dict[str, revisions.SectionState]:
        baseline = self._baselines.get(node_id)
        return baseline.views.section_states() if baseline else {}

    def token_states(
        self, node_id: NodeId
    ) -> dict[str, revisions.TokenState]:
        baseline = self._baselines.get(node_id)
        return baseline.views.token_states() if baseline else {}

    def word_token_authors(
        self, node_id: NodeId
    ) -> dict[str, tuple[str, ...]]:
        """Final-state hash -> one author per token (WP45/46), memoized
        on the baseline -- the prose wire's fifth fold, carried across
        marks/comments-only writes since records don't change."""
        baseline = self._baselines.get(node_id)
        return baseline.views.word_token_authors() if baseline else {}

    def locked_runs(self, node_id: NodeId) -> dict[str, tuple[str, ...]]:
        """Per block: the locked runs' verbatim text (WP46) -- what the
        context block shows the model beside partially-locked blocks."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            return {}
        return baseline.views.locked_runs()

    # -- the section guard (WP42) ------------------------------------------

    def check_body_update(self, node_id: NodeId, new_body: str) -> None:
        """NodeWriter's injected body-guard: raise if the new body drops
        LOCKED text -- verbatim-presence per locked run since WP46
        (moving locked text is fine, changing or deleting it is not).
        Sync on purpose (in-memory fold, no awaits in the writer);
        untracked / never-recorded nodes pass freely."""
        baseline = self._baselines.get(node_id)
        if baseline is None:
            return
        missing = baseline.views.missing_locked(new_body)
        if not missing:
            return
        raise LockedSectionsChanged(
            self._name_of(node_id),
            tuple(
                (block, run if len(run) <= 240 else run[:240] + "…")
                for block, run in missing
            ),
        )
