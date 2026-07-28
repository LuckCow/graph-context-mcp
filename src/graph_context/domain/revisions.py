"""Node revision history: the pure rules (WP41, ADR 049).

The single home of every segmentation / normalization / blame rule. A
tracked node's body is split into markdown BLOCKS; each block's identity
is a hash of its NORMALIZED text, so an unchanged paragraph keeps its
identity across moves and across edits elsewhere -- no stored offsets,
nothing to re-anchor. Revisions are an append-only log of keyframe +
delta records (hash sequences, plus the text of first-seen blocks);
blame is DERIVED from the log at read time, never stored.

Normalization exists because the store rewrites markdown (ADR 010:
nothing may compare bodies byte-exact; quirk A9 flattens a leading
heading, A13 drops fence info strings, whitespace shifts on round-trip).
ALL body comparison anywhere in the system must route through
:func:`hash_sequence` -- a second comparison rule would be a second
place to get it wrong.

Pure module: no I/O, no clocks -- timestamps are injected strings; the
historian (``application/node_historian.py``) owns the side effects.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from graph_context.errors import GraphContextError, SectionAnchorNotFound

KEYFRAME_INTERVAL = 20    # every Nth revision stores the full hash list
LOG_SOFT_CAP = 400_000    # rendered-log chars; compaction target
MIN_BLAME_CHARS = 20      # shorter blocks (scene separators) skip blame
SIMILARITY_THRESHOLD = 0.6  # edited-block lineage match (difflib ratio)

AUTHOR_MODEL = "model"
AUTHOR_HUMAN = "human"

# The Space Context's tracked-types list (ADR 049): type display names,
# comma/newline separated, human-edited in Anytype. Domain-homed like the
# scheduling/rule keys so every layer shares one spelling.
FIELD_TRACKED_TYPES = "gc_tracked_types"
# The history sidecar's discriminator: the tracked node's id (the
# ``gc_session_key`` pattern -- text, not an ``objects`` relation, so the
# link never reflects as a graph edge or grows a connections footer).
FIELD_HISTORY_OF = "gc_history_of"

HISTORY_FIELDS: dict[str, str] = {  # key -> format; bootstrap mints these
    FIELD_HISTORY_OF: "text",
}
TRACKED_TYPES_FIELDS: dict[str, str] = {  # on the Space Context object
    FIELD_TRACKED_TYPES: "text",
}

KIND_KEYFRAME = "keyframe"
KIND_DELTA = "delta"
KIND_TRUNCATED = "truncated"  # compaction marker: older history dropped

# Section marks (WP42): status/intent records interleaved in the SAME
# log. No seq -- file order is fold order.
MARK_STATUS = "status"
MARK_INTENT = "intent"

STATUS_RAW_AI = "raw_ai"
STATUS_APPROVED = "approved"
STATUS_HUMAN = "human"
STATUS_VALUES = frozenset({STATUS_RAW_AI, STATUS_APPROVED, STATUS_HUMAN})

INTENT_LOCKED = "locked"
INTENT_FLEXIBLE = "flexible"
INTENT_NEEDS_CHANGE = "needs_change"
INTENT_VALUES = frozenset({INTENT_LOCKED, INTENT_FLEXIBLE, INTENT_NEEDS_CHANGE})

# edit_body's only non-hash anchor: insert_after "top" prepends.
ANCHOR_TOP = "top"
EDIT_ACTIONS = ("replace", "insert_after", "delete")
_MIN_ANCHOR_CHARS = 4  # shortest accepted unique hash prefix

_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_WORD = re.compile(r"\S+\s*|\s+")  # word_diff tokens; join reproduces input


def parse_tracked_types(raw: str) -> tuple[str, ...]:
    """The human-typed tracked-types text -> clean type display names.

    Comma/newline/semicolon separated; case-insensitively deduped with
    the first spelling kept (matching is case-insensitive everywhere).
    """
    parts = re.split(r"[,\n;]+", raw or "")
    seen: dict[str, str] = {}
    for part in parts:
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


# -- segmentation and identity --------------------------------------------

def split_blocks(body: str) -> tuple[str, ...]:
    """Markdown -> blocks: blank-line separated, fenced blocks kept whole."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        if not in_fence and not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return tuple(blocks)


def normalize_block(text: str) -> str:
    """One block -> its comparison form, absorbing the pinned store drift.

    Leading ``#`` marks go (A9 flattens a first-line heading -- a heading
    and its flattened text must hash alike), fence info strings go (A13),
    whitespace runs collapse, edges strip. The result is an IDENTITY
    key, not display text.
    """
    lines = []
    for index, line in enumerate(text.splitlines()):
        fence = _FENCE.match(line)
        if fence:
            # "```python" and "```" must hash alike (A13).
            line = fence.group(1)
        elif index == 0:
            line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def block_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def hash_sequence(body: str) -> tuple[tuple[str, str], ...]:
    """The body's ordered ``(hash, normalized_text)`` pairs; empty
    blocks (nothing survives normalization) are skipped."""
    pairs = []
    for block in split_blocks(body):
        normalized = normalize_block(block)
        if normalized:
            pairs.append((block_hash(normalized), normalized))
    return tuple(pairs)


def body_blocks(body: str) -> tuple[tuple[str, str], ...]:
    """The body's ordered ``(hash, RAW block text)`` pairs -- the display
    and anchor listing (hash_sequence's normalized twin). Blocks whose
    normalization is empty are skipped: they have no identity to anchor."""
    pairs = []
    for block in split_blocks(body):
        normalized = normalize_block(block)
        if normalized:
            pairs.append((block_hash(normalized), block))
    return tuple(pairs)


def resolve_anchor(
    anchor: str,
    hashes: Sequence[str],
    sections: tuple[tuple[str, str], ...] = (),
) -> int:
    """A hash (or unique prefix, git-style, >= _MIN_ANCHOR_CHARS) -> its
    index in ``hashes``. Raises SectionAnchorNotFound on miss or
    ambiguity; ``sections`` (hash, first-line) pairs feed the prompt."""
    wanted = anchor.strip().lstrip("§").lower()
    if len(wanted) >= _MIN_ANCHOR_CHARS:
        matches = [i for i, h in enumerate(hashes) if h.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SectionAnchorNotFound(
                anchor, sections,
                reason=f"{len(matches)} sections match the prefix",
            )
    raise SectionAnchorNotFound(anchor, sections)


def edit_body(
    body: str, *, action: str, anchor: str, text: str = ""
) -> str:
    """Splice ONE section: ``replace`` / ``insert_after`` / ``delete``,
    anchored on a block hash (``insert_after`` also accepts ANCHOR_TOP
    to prepend). Untouched blocks are carried verbatim, so their hashes
    are stable by construction; separators normalize to one blank line
    (the store rewrites whitespace anyway, ADR 010)."""
    if action not in EDIT_ACTIONS:
        raise GraphContextError(
            f"unknown edit action {action!r}; allowed: "
            f"{', '.join(EDIT_ACTIONS)}."
        )
    blocks = list(split_blocks(body))
    hashes = [
        block_hash(normalized) if (normalized := normalize_block(b)) else ""
        for b in blocks
    ]
    sections = tuple(
        (h, b.splitlines()[0][:60] if b else "")
        for h, b in zip(hashes, blocks, strict=True) if h
    )
    if action == "insert_after" and anchor.strip().lower() == ANCHOR_TOP:
        index = -1
    else:
        index = resolve_anchor(anchor, hashes, sections)
    if action == "replace":
        blocks[index] = text
    elif action == "insert_after":
        blocks.insert(index + 1, text)
    else:  # delete
        del blocks[index]
    return "\n\n".join(block for block in blocks if block.strip())


def word_diff(old: str, new: str) -> tuple[tuple[str, str], ...]:
    """Word-level spans between two block texts: ``("eq"|"add"|"del",
    text)`` in reading order -- WP43's server-side intra-block diff (the
    page renders spans; no client diff library, ADR 025)."""
    tokens_old = _WORD.findall(old)
    tokens_new = _WORD.findall(new)
    matcher = difflib.SequenceMatcher(
        a=tokens_old, b=tokens_new, autojunk=False
    )
    spans: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            spans.append(("eq", "".join(tokens_old[i1:i2])))
            continue
        if tag in ("replace", "delete") and i2 > i1:
            spans.append(("del", "".join(tokens_old[i1:i2])))
        if tag in ("replace", "insert") and j2 > j1:
            spans.append(("add", "".join(tokens_new[j1:j2])))
    return tuple(spans)


# -- the revision log ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DeltaOp:
    """One SequenceMatcher opcode over hash sequences, JSON-shaped.

    ``tag`` is difflib's (``equal``/``delete``/``replace``/``insert``);
    ``i1``/``i2`` index the PREVIOUS sequence; ``emit`` is the new
    hashes this op contributes (empty for equal/delete -- equal copies
    the previous slice instead).
    """

    tag: str
    i1: int
    i2: int
    emit: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """One recorded revision of one tracked node's body."""

    seq: int
    at: str                  # injected ISO timestamp
    author_kind: str         # AUTHOR_MODEL | AUTHOR_HUMAN
    author_detail: str       # "model · mode · user" or "human"
    kind: str                # keyframe | delta | truncated
    hashes: tuple[str, ...] = ()        # keyframe: the full sequence
    ops: tuple[DeltaOp, ...] = ()       # delta: ops against the previous
    new_blocks: Mapping[str, str] = field(default_factory=dict)
    # ^ normalized text for hashes first seen in this revision


@dataclass(frozen=True, slots=True)
class SectionMark:
    """A per-block status or intent record (WP42), keyed by block hash.

    Marks live in the same JSONL log as revisions; a mark applies at its
    file position iff its hash is live in the sequence at that point (a
    stale mark folds to nothing, harmlessly).
    """

    kind: str    # MARK_STATUS | MARK_INTENT
    hash: str    # the block hash the mark keys on
    value: str   # STATUS_VALUES | INTENT_VALUES member
    at: str      # injected ISO timestamp
    by: str      # who set it ("user", "human:prose-page", ...)


LogEntry = RevisionRecord | SectionMark


@dataclass(frozen=True, slots=True)
class SectionState:
    """One block's folded review state (WP42). Defaults are the state of
    a never-marked model-authored block."""

    status: str = STATUS_RAW_AI
    intent: str = INTENT_FLEXIBLE
    status_at: str = ""
    status_by: str = ""
    intent_at: str = ""
    intent_by: str = ""


@dataclass(frozen=True, slots=True)
class LogParse:
    """A parsed log; ``skipped`` counts unparseable lines (a human who
    edited the sidecar must degrade history, never brick it).

    ``records`` is the revisions-only view (state walks, blame);
    ``entries`` interleaves section marks in file order (the fold input).
    """

    records: tuple[RevisionRecord, ...]
    skipped: int = 0
    entries: tuple[LogEntry, ...] = ()


def apply_ops(prev: Sequence[str], ops: Sequence[DeltaOp]) -> tuple[str, ...]:
    result: list[str] = []
    for op in ops:
        if op.tag == "equal":
            result.extend(prev[op.i1:op.i2])
        elif op.tag in ("replace", "insert"):
            result.extend(op.emit)
        # delete contributes nothing
    return tuple(result)


def _ops_between(
    prev: Sequence[str], new: Sequence[str]
) -> tuple[DeltaOp, ...]:
    matcher = difflib.SequenceMatcher(a=list(prev), b=list(new), autojunk=False)
    # Deletes are implicit (apply_ops emits nothing for missing ranges),
    # so they never take log space.
    return tuple(
        DeltaOp(
            tag=tag, i1=i1, i2=i2,
            emit=tuple(new[j1:j2]) if tag in ("replace", "insert") else (),
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "delete"
    )


def next_record(
    prev_hashes: Sequence[str],
    prev_seq: int,
    pairs: Sequence[tuple[str, str]],
    *,
    at: str,
    author_kind: str,
    author_detail: str,
    known_hashes: frozenset[str],
) -> RevisionRecord:
    """The record for a new body state (``pairs`` from
    :func:`hash_sequence`). Keyframes recur every KEYFRAME_INTERVAL and
    open every log (seq 1); ``known_hashes`` = every hash whose text an
    earlier record already carries, so ``new_blocks`` stays minimal.
    """
    seq = prev_seq + 1
    hashes = tuple(h for h, _ in pairs)
    new_blocks = {h: text for h, text in pairs if h not in known_hashes}
    if seq == 1 or seq % KEYFRAME_INTERVAL == 0:
        return RevisionRecord(
            seq=seq, at=at, author_kind=author_kind,
            author_detail=author_detail, kind=KIND_KEYFRAME,
            hashes=hashes, new_blocks=new_blocks,
        )
    return RevisionRecord(
        seq=seq, at=at, author_kind=author_kind,
        author_detail=author_detail, kind=KIND_DELTA,
        ops=_ops_between(tuple(prev_hashes), hashes), new_blocks=new_blocks,
    )


def state_walk(
    records: Sequence[RevisionRecord],
) -> tuple[tuple[RevisionRecord, tuple[str, ...]], ...]:
    """Each usable record paired with the FULL hash sequence after it.

    Starts at the first keyframe (deltas before it -- possible only
    after a mangled compaction -- are unreconstructable and skipped).
    """
    states: list[tuple[RevisionRecord, tuple[str, ...]]] = []
    current: tuple[str, ...] | None = None
    for record in records:
        if record.kind == KIND_KEYFRAME:
            current = record.hashes
        elif record.kind == KIND_DELTA and current is not None:
            current = apply_ops(current, record.ops)
        else:
            continue
        states.append((record, current))
    return tuple(states)


def current_hashes(records: Sequence[RevisionRecord]) -> tuple[str, ...]:
    states = state_walk(records)
    return states[-1][1] if states else ()


def texts_of(records: Sequence[RevisionRecord]) -> dict[str, str]:
    """Every hash the log can still name -> its normalized text."""
    texts: dict[str, str] = {}
    for record in records:
        texts.update(record.new_blocks)
    return texts


# -- serialization ---------------------------------------------------------

_LOG_HEADER = (
    "Revision history (bot-maintained; do not edit). One JSON record "
    "per line inside the fence; blame and diffs derive from these."
)


def render_log(entries: Sequence[LogEntry]) -> str:
    """The sidecar body: a header sentence, then one JSON object per
    line in a single fence. No info string on the fence (A13 would drop
    it) and nothing heading-shaped on line one (A9 would flatten it)."""
    lines = [json.dumps(_entry_payload(e), ensure_ascii=False,
                        separators=(",", ":"), sort_keys=True)
             for e in entries]
    return _LOG_HEADER + "\n\n```\n" + "\n".join(lines) + "\n```"


def _entry_payload(entry: LogEntry) -> dict[str, Any]:
    if isinstance(entry, SectionMark):
        return {
            "kind": entry.kind, "hash": entry.hash, "value": entry.value,
            "at": entry.at, "by": entry.by,
        }
    return _record_payload(entry)


def _record_payload(record: RevisionRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seq": record.seq, "at": record.at, "author": record.author_kind,
        "detail": record.author_detail, "kind": record.kind,
    }
    if record.kind == KIND_KEYFRAME:
        payload["hashes"] = list(record.hashes)
    elif record.kind == KIND_DELTA:
        payload["ops"] = [
            [op.tag, op.i1, op.i2, *([list(op.emit)] if op.emit else [])]
            for op in record.ops
        ]
    if record.new_blocks:
        payload["new_blocks"] = dict(record.new_blocks)
    return payload


def parse_log(body: str) -> LogParse:
    """The sidecar body -> entries, leniently: lines that don't parse as
    record/mark JSON are counted, never fatal; text outside the fence is
    ignored (the header, or human notes)."""
    entries: list[LogEntry] = []
    skipped = 0
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence or not line.strip():
            continue
        entry = _parse_entry(line)
        if entry is None:
            skipped += 1
        else:
            entries.append(entry)
    return LogParse(
        records=tuple(e for e in entries if isinstance(e, RevisionRecord)),
        skipped=skipped,
        entries=tuple(entries),
    )


def _parse_entry(line: str) -> LogEntry | None:
    try:
        payload = json.loads(line)
        kind = str(payload["kind"])
        if kind in (MARK_STATUS, MARK_INTENT):
            mark = SectionMark(
                kind=kind,
                hash=str(payload["hash"]),
                value=str(payload["value"]),
                at=str(payload["at"]),
                by=str(payload["by"]),
            )
            allowed = STATUS_VALUES if kind == MARK_STATUS else INTENT_VALUES
            if not mark.hash or mark.value not in allowed:
                return None
            return mark
        ops = tuple(
            DeltaOp(
                tag=str(op[0]), i1=int(op[1]), i2=int(op[2]),
                emit=tuple(str(h) for h in op[3]) if len(op) > 3 else (),
            )
            for op in payload.get("ops", ())
        )
        return RevisionRecord(
            seq=int(payload["seq"]),
            at=str(payload["at"]),
            author_kind=str(payload["author"]),
            author_detail=str(payload["detail"]),
            kind=kind,
            hashes=tuple(str(h) for h in payload.get("hashes", ())),
            ops=ops,
            new_blocks={
                str(k): str(v)
                for k, v in payload.get("new_blocks", {}).items()
            },
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError):
        return None


# -- derived views ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BlameEntry:
    """Who introduced the block a current hash names."""

    author_kind: str
    author_detail: str
    at: str
    seq: int
    ancestor: str = ""  # the removed hash this block most resembles, if any


def blame(records: Sequence[RevisionRecord]) -> dict[str, BlameEntry]:
    """Current hash -> the revision that INTRODUCED it, derived by
    walking the log. An introduced block is similarity-matched against
    the same revision's removed blocks for lineage (an edit reads as
    "replaced its ancestor", a brand-new paragraph has none). Blocks
    shorter than MIN_BLAME_CHARS stay out (separators share hashes)."""
    states = state_walk(records)
    texts = texts_of(records)
    entries: dict[str, BlameEntry] = {}
    previous: tuple[str, ...] = ()
    for record, hashes in states:
        added = set(hashes) - set(previous)
        removed = set(previous) - set(hashes)
        for added_hash in added:
            text = texts.get(added_hash, "")
            entries[added_hash] = BlameEntry(
                author_kind=record.author_kind,
                author_detail=record.author_detail,
                at=record.at,
                seq=record.seq,
                ancestor=closest(text, removed, texts),
            )
        previous = hashes
    return {
        h: entry for h, entry in entries.items()
        if h in previous and len(texts.get(h, "")) >= MIN_BLAME_CHARS
    }


def closest(
    text: str, candidates: set[str], texts: Mapping[str, str]
) -> str:
    """The candidate hash whose text most resembles ``text`` (difflib
    ratio >= SIMILARITY_THRESHOLD), or "". THE lineage rule: blame,
    the WP42 status fold, and diff pairing all match through here."""
    if not text or not candidates:
        return ""
    best, best_ratio = "", SIMILARITY_THRESHOLD
    for candidate in sorted(candidates):
        other = texts.get(candidate, "")
        if not other:
            continue
        ratio = difflib.SequenceMatcher(a=text, b=other, autojunk=False).ratio()
        if ratio >= best_ratio:
            best, best_ratio = candidate, ratio
    return best


def section_states(entries: Sequence[LogEntry]) -> dict[str, SectionState]:
    """The fold (WP42): interleaved log entries -> current per-block
    status/intent, keyed to the FINAL hash sequence.

    Walked in file order. A revision's added blocks similarity-match its
    removed blocks through :func:`closest` (the one lineage rule):
    across a HUMAN edit the successor inherits its ancestor's status and
    intent (a no-ancestor human block starts ``human``); across a MODEL
    edit the successor drops to ``raw_ai`` -- any AI edit voids
    ``approved`` -- while intent follows. A mark applies iff its hash is
    live at that point in the fold; stale marks fold to nothing.
    """
    states: dict[str, SectionState] = {}
    texts: dict[str, str] = {}
    current: tuple[str, ...] | None = None
    for entry in entries:
        if isinstance(entry, SectionMark):
            if current is None or entry.hash not in current:
                continue
            prior = states.get(entry.hash, SectionState())
            if entry.kind == MARK_STATUS:
                states[entry.hash] = replace(
                    prior, status=entry.value,
                    status_at=entry.at, status_by=entry.by,
                )
            else:
                states[entry.hash] = replace(
                    prior, intent=entry.value,
                    intent_at=entry.at, intent_by=entry.by,
                )
            continue
        if entry.kind == KIND_KEYFRAME:
            new = entry.hashes
        elif entry.kind == KIND_DELTA and current is not None:
            new = apply_ops(current, entry.ops)
        else:
            continue
        texts.update(entry.new_blocks)
        previous = current or ()
        removed = set(previous) - set(new)
        for added_hash in set(new) - set(previous):
            ancestor = closest(texts.get(added_hash, ""), removed, texts)
            inherited = states.get(ancestor) if ancestor else None
            if entry.author_kind == AUTHOR_HUMAN:
                states[added_hash] = (
                    inherited if inherited is not None
                    else SectionState(
                        status=STATUS_HUMAN,
                        status_at=entry.at, status_by=entry.author_detail,
                    )
                )
            else:
                base = inherited or SectionState()
                states[added_hash] = replace(
                    base, status=STATUS_RAW_AI,
                    status_at=entry.at, status_by=entry.author_detail,
                )
        current = new
    final = set(current or ())
    return {h: s for h, s in states.items() if h in final}


def missing_locked(
    states: Mapping[str, SectionState], new_body: str
) -> tuple[str, ...]:
    """LOCKED hashes absent from ``new_body`` -- an order-insensitive
    presence check (moving a locked block is fine), no diffing (WP42)."""
    present = {h for h, _ in hash_sequence(new_body)}
    return tuple(sorted(
        h for h, state in states.items()
        if state.intent == INTENT_LOCKED and h not in present
    ))


def compact(
    entries: Sequence[LogEntry], cap: int = LOG_SOFT_CAP
) -> tuple[LogEntry, ...]:
    """Drop the oldest history until the rendered log fits ``cap``.

    The kept suffix must start at a keyframe (deltas need their base);
    a truncation-marker record notes what was dropped. If even the
    newest keyframe's suffix exceeds the cap, that suffix is kept
    anyway -- current-state blame must always survive. Dropped-era
    section marks whose hash is live in the first kept keyframe are
    hoisted to just after it, original order kept (fold-equivalent:
    they historically preceded everything in the suffix); marks on
    dead hashes drop with their era.
    """
    kept = list(entries)
    if len(render_log(kept)) <= cap:
        return tuple(entries)
    records = [e for e in entries if isinstance(e, RevisionRecord)]
    all_texts = texts_of(records)
    dropped_through = 0
    dropped_marks: list[SectionMark] = []
    while len(render_log(kept)) > cap:
        next_keyframe = next(
            (i for i, e in enumerate(kept)
             if i and isinstance(e, RevisionRecord)
             and e.kind == KIND_KEYFRAME),
            None,
        )
        if next_keyframe is None:
            break  # nothing older left to shed; over-cap is the lesser evil
        dropped = kept[:next_keyframe]
        dropped_through = max(
            r.seq for r in dropped if isinstance(r, RevisionRecord)
        )
        dropped_marks.extend(
            e for e in dropped if isinstance(e, SectionMark)
        )
        kept = kept[next_keyframe:]
    if dropped_through == 0:
        return tuple(entries)
    kept_records = [e for e in kept if isinstance(e, RevisionRecord)]
    # Blocks introduced in the dropped era but still alive lose their
    # text with the dropped records -- re-carry it on the first kept
    # keyframe, or blame and the status fold would go blind.
    reachable: set[str] = set()
    for _, hashes in state_walk(kept_records):
        reachable.update(hashes)
    carried = texts_of(kept_records)
    missing = {
        h: all_texts[h]
        for h in reachable - set(carried) if h in all_texts
    }
    first = kept[0]
    assert isinstance(first, RevisionRecord)  # suffix starts at a keyframe
    if missing:
        first = RevisionRecord(
            seq=first.seq, at=first.at, author_kind=first.author_kind,
            author_detail=first.author_detail, kind=first.kind,
            hashes=first.hashes, ops=first.ops,
            new_blocks={**missing, **first.new_blocks},
        )
    live = set(first.hashes)
    hoisted = [m for m in dropped_marks if m.hash in live]
    marker = RevisionRecord(
        seq=dropped_through, at=first.at, author_kind=AUTHOR_MODEL,
        author_detail="compaction", kind=KIND_TRUNCATED,
    )
    return (marker, first, *hoisted, *kept[1:])
