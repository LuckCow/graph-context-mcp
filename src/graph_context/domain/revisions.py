"""Node revision history: the pure rules (WP41, ADR 049; raw indexing
since WP48, ADR 054).

The single home of every segmentation / normalization / blame rule. A
tracked node's body is split into markdown BLOCKS; each block's identity
is a hash of its NORMALIZED text, so an unchanged paragraph keeps its
identity across moves and across edits elsewhere -- no stored offsets,
nothing to re-anchor. Revisions are an append-only log of keyframe +
delta records (hash sequences, plus the RAW text of blocks whose text
the log doesn't already carry); blame is DERIVED from the log at read
time, never stored.

Normalization exists because the store rewrites markdown (ADR 010:
nothing may compare bodies byte-exact; quirk A9 flattens a leading
heading, A13 drops fence info strings, whitespace shifts on round-trip).
IDENTITY comparison anywhere in the system must route through
:func:`hash_sequence` -- a second comparison rule would be a second
place to get it wrong. Everything ELSE -- stored block texts, word
tokens, mark ranges, authorship, locked runs -- indexes the RAW text as
fetched from the store (ADR 054): what the editor shows is what the
state is keyed on, and baselines always come from ``fetch_body`` output
so store rewrites surface as ordinary text refreshes, never as drift
between two indexing schemes.

Pure module: no I/O, no clocks -- timestamps are injected strings; the
historian (``application/node_historian.py``) owns the side effects.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, TypeVar

from graph_context.errors import GraphContextError, SectionAnchorNotFound

KEYFRAME_INTERVAL = 20    # every Nth revision stores the full hash list
LOG_SOFT_CAP = 400_000    # rendered-log chars; compaction target
SIMILARITY_THRESHOLD = 0.6  # edited-block lineage match (difflib ratio)
RECORD_VERSION = 2        # "v" on new records: 2 = raw new_blocks (ADR 054)

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

# Comments (WP50, ADR 056): human-authored notes on a selection,
# interleaved in the SAME log. Lifecycle open -> addressed (the model
# acted on it) -> resolved (the human closed it); transitions are
# separate append-only ``comment_state`` lines, folded last-wins with
# ``resolved`` terminal. ``open`` is implicit and never serialized.
KIND_COMMENT = "comment"
KIND_COMMENT_STATE = "comment_state"

COMMENT_OPEN = "open"
COMMENT_ADDRESSED = "addressed"
COMMENT_RESOLVED = "resolved"
COMMENT_STATE_VALUES = frozenset({COMMENT_ADDRESSED, COMMENT_RESOLVED})
COMMENT_TEXT_CAP = 2000  # comment text chars; they ride the LLM context

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
_WORD_CHAR = re.compile(r"\w")
_WS_RUN = re.compile(r"\s+")


def has_words(text: str) -> bool:
    """Whether a block can carry review state / blame / authorship.

    Word-free blocks (scene separators ``***``, rules ``---``) are
    duplicated across a manuscript and share hashes, so state on one
    would alias onto all -- they stay unmarkable. This replaced the old
    MIN_BLAME_CHARS length floor (ADR 054): short PROSE ("No.", "She
    ran.") is markable and blameable.
    """
    return _WORD_CHAR.search(text) is not None


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


def block_tokens(text: str) -> list[str]:
    """A block's word tokens -- THE token rule (WP45/46): span-mark
    ranges, authorship, and state inheritance all index these (joining
    them reproduces the text). Since ADR 054 the tokenized text is the
    RAW block text as fetched, not its normalization."""
    return _WORD.findall(text)


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


def block_offsets(body: str) -> tuple[tuple[str, int, int], ...]:
    """The body's identity-bearing blocks as ``(hash, start, end)``
    absolute character offsets (``body[start:end]`` is the raw block
    text) -- the document-level segment map the prose wire serves
    (ADR 054). Same segmentation and skip rules as :func:`body_blocks`;
    identical blocks repeat their shared hash at each position."""
    offsets: list[tuple[str, int, int]] = []
    spans: list[tuple[int, int]] = []  # (line start, line content end)
    lines: list[str] = []
    in_fence = False
    cursor = 0

    def _flush() -> None:
        if not lines:
            return
        normalized = normalize_block("\n".join(lines))
        if normalized:
            offsets.append((block_hash(normalized), spans[0][0], spans[-1][1]))
        lines.clear()
        spans.clear()

    for raw_line in body.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and not line.strip():
            _flush()
            cursor += len(raw_line)
            continue
        lines.append(line)
        spans.append((cursor, cursor + len(line)))
        cursor += len(raw_line)
    _flush()
    return tuple(offsets)


def char_range_to_tokens(text: str, start: int, end: int) -> tuple[int, int]:
    """A character range over a block's raw text -> the ``(s, e)`` token
    range it touches (any overlap counts; ``e`` is one past the last).
    The wire takes character offsets -- the browser selection's native
    unit -- and this is the ONLY place they become tokens; nothing
    offset-shaped is ever stored. Raises on a range that touches no
    token (empty or out of bounds)."""
    if start >= end:
        raise GraphContextError(
            f"empty mark range {start}..{end}; select at least one "
            "character."
        )
    s = e = -1
    cursor = 0
    for index, token in enumerate(block_tokens(text)):
        token_start, cursor = cursor, cursor + len(token)
        if token_start < end and cursor > start:
            if s < 0:
                s = index
            e = index + 1
    if s < 0:
        raise GraphContextError(
            f"mark range {start}..{end} is outside this section's "
            f"0..{len(text)} characters; reload and reselect."
        )
    return s, e


def token_range_to_chars(text: str, s: int, e: int) -> tuple[int, int]:
    """A ``(s, e)`` token range over a block's raw text -> the character
    offsets it spans -- :func:`char_range_to_tokens`'s inverse, for
    DISPLAY (the wire's absolute anchors, the context block's quoted
    words). The end trims the last token's trailing whitespace. Raises
    on a range the text doesn't have."""
    tokens = block_tokens(text)
    if not (0 <= s < e <= len(tokens)):
        raise GraphContextError(
            f"token range {s}..{e} is outside this section's "
            f"0..{len(tokens)} tokens."
        )
    start = sum(len(token) for token in tokens[:s])
    end = start + len("".join(tokens[s:e]).rstrip())
    return start, end


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
    tokens_old = block_tokens(old)
    tokens_new = block_tokens(new)
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
    # ^ RAW text for hashes first seen (or refreshed) in this revision;
    #   pre-ADR-054 logs carry normalized text here until refreshed


@dataclass(frozen=True, slots=True)
class SectionMark:
    """A per-block status or intent record (WP42), keyed by block hash.

    Marks live in the same JSONL log as revisions; a mark applies at its
    file position iff its hash is live in the sequence at that point (a
    stale mark folds to nothing, harmlessly).

    ``start``/``end`` (WP46) narrow the mark to a TOKEN range over the
    block's raw text (normalized in pre-ADR-054 logs) -- ``(-1, -1)``
    (the default, and the wire shape without ``s``/``e`` keys) means the
    whole block. Ranges are positions AT MARKING TIME; under later edits
    the state follows the tokens through the fold's positional
    inheritance, never a stored offset.
    """

    kind: str    # MARK_STATUS | MARK_INTENT
    hash: str    # the block hash the mark keys on
    value: str   # STATUS_VALUES | INTENT_VALUES member
    at: str      # injected ISO timestamp
    by: str      # who set it ("user", "human:prose-page", ...)
    start: int = -1  # first token of the marked range, -1 = whole block
    end: int = -1    # one past the last token, -1 = whole block


@dataclass(frozen=True, slots=True)
class CommentEntry:
    """A comment on a selection (WP50), keyed by block hash + optional
    token range -- same anchoring as a ranged :class:`SectionMark`, but
    a comment is an OBJECT with identity: its anchor rides edits forward
    through the fold (:func:`comment_states`), it is not consumed by
    them. ``hash`` may be ``""`` on a compaction-rewritten line whose
    anchor was already detached (it can never re-attach)."""

    id: str      # stable, clock-free: :func:`comment_id`
    hash: str    # anchor block hash AT WRITE TIME; "" = born detached
    text: str    # the human's note (<= COMMENT_TEXT_CAP)
    at: str      # injected ISO timestamp
    by: str      # who wrote it ("human:prose-page", ...)
    start: int = -1  # first token of the anchored range, -1 = whole block
    end: int = -1    # one past the last token, -1 = whole block


@dataclass(frozen=True, slots=True)
class CommentStateEntry:
    """A comment lifecycle transition (WP50): ``addressed`` (the model
    acted on it) or ``resolved`` (the human closed it). Folded
    last-wins; ``resolved`` is terminal; unknown ids fold to nothing."""

    id: str      # the comment this transitions
    value: str   # COMMENT_ADDRESSED | COMMENT_RESOLVED
    at: str      # injected ISO timestamp
    by: str      # who transitioned it ("model", "human:prose-page", ...)


LogEntry = RevisionRecord | SectionMark | CommentEntry | CommentStateEntry


def comment_id(at: str, by: str, anchor_hash: str, text: str) -> str:
    """A comment's stable id, clock-free (``at`` is injected like every
    timestamp): identical duplicates collide deliberately -- the
    historian drops them as change-only no-ops."""
    digest = hashlib.sha256(
        f"{at}|{by}|{anchor_hash}|{text}".encode()
    ).hexdigest()
    return "c" + digest[:8]


@dataclass(frozen=True, slots=True)
class SectionState:
    """One block's review state as a BADGE (WP42): derived from the
    per-token fold since WP46 -- ``approved``/``human`` only when every
    token agrees, mixed blocks read ``raw_ai``; intent is the strictest
    token's (`locked` > `needs_change` > `flexible`)."""

    status: str = STATUS_RAW_AI
    intent: str = INTENT_FLEXIBLE
    status_at: str = ""
    status_by: str = ""
    intent_at: str = ""
    intent_by: str = ""


@dataclass(frozen=True, slots=True)
class TokenState:
    """One block's folded review state per TOKEN (WP46), aligned with
    the ``_WORD`` tokens of its raw text. The at/by stamps are
    block-level: the last mark (or introducing revision) that touched
    the block."""

    status: tuple[str, ...]
    intent: tuple[str, ...]
    status_at: str = ""
    status_by: str = ""
    intent_at: str = ""
    intent_by: str = ""


@dataclass(frozen=True, slots=True)
class CommentState:
    """One live comment's folded state (WP50): its CURRENT anchor after
    riding every edit. ``hash`` is the anchor block hash, ``""`` when
    detached (the commented text was removed -- the comment stays listed
    until resolved); ``start``/``end`` are the current token range over
    the anchor, ``(-1, -1)`` = whole block. Resolved comments never
    appear here -- they stay in the log until compaction."""

    id: str
    text: str
    state: str   # COMMENT_OPEN | COMMENT_ADDRESSED
    hash: str    # current anchor; "" = detached
    start: int = -1
    end: int = -1
    at: str = ""
    by: str = ""
    state_at: str = ""
    state_by: str = ""


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
    known_texts: Mapping[str, str],
) -> RevisionRecord:
    """The record for a new body state (``pairs`` from
    :func:`body_blocks` -- hash, RAW text). Keyframes recur every
    KEYFRAME_INTERVAL and open every log (seq 1); ``known_texts`` =
    :func:`texts_of` over the earlier records, so ``new_blocks`` stays
    minimal -- but a hash whose STORED text differs from the live raw
    text re-emits (last-write-wins in :func:`texts_of`): one mechanism
    refreshes store-rewritten text AND migrates pre-ADR-054 logs whose
    stored texts are normalized.
    """
    seq = prev_seq + 1
    hashes = tuple(h for h, _ in pairs)
    new_blocks = {h: text for h, text in pairs if known_texts.get(h) != text}
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


@dataclass(frozen=True, slots=True)
class RevisionStep:
    """One usable record plus the walk scaffolding every derived view
    shares (blame, the review fold, authorship, the comment fold, the
    prose bridge's diff and timeline): the hash sequences around the
    record and its added/removed hashes."""

    record: RevisionRecord
    previous: tuple[str, ...]  # hash sequence BEFORE this record
    hashes: tuple[str, ...]    # hash sequence AFTER it
    added: tuple[str, ...]     # new at this step, in SEQUENCE order --
                               # a duplicated new block repeats its hash
    removed: frozenset[str]    # dropped at this step

    def ancestor(self, added_hash: str, texts: Mapping[str, str]) -> str:
        """Lineage for ONE added hash via :func:`closest` -- lazy, so
        walks that skip already-seen hashes never pay for the match."""
        return closest(texts.get(added_hash, ""), self.removed, texts)

    def ancestors(self, texts: Mapping[str, str]) -> dict[str, str]:
        """Added hash -> its ancestor ("" = none), the eager map the
        comment fold rides."""
        return {h: self.ancestor(h, texts) for h in self.added}


def _step(
    record: RevisionRecord,
    previous: tuple[str, ...],
    hashes: tuple[str, ...],
) -> RevisionStep:
    prev_set = set(previous)
    return RevisionStep(
        record=record, previous=previous, hashes=hashes,
        added=tuple(h for h in hashes if h not in prev_set),
        removed=frozenset(prev_set - set(hashes)),
    )


def _log_steps(
    entries: Sequence[LogEntry],
) -> Iterator[tuple[LogEntry, RevisionStep | None]]:
    """THE log walk: every entry in file order, paired with a
    :class:`RevisionStep` exactly when the entry advances the hash
    sequence -- a keyframe, or a delta with a reconstructable base
    (deltas before the first keyframe, possible only after a mangled
    compaction, yield ``None`` like marks and comments do). Folds over
    the interleaved log ride this; marks and comments consult the
    LATEST step's ``hashes``."""
    current: tuple[str, ...] = ()
    started = False
    for entry in entries:
        step: RevisionStep | None = None
        if isinstance(entry, RevisionRecord):
            if entry.kind == KIND_KEYFRAME:
                new = entry.hashes
            elif entry.kind == KIND_DELTA and started:
                new = apply_ops(current, entry.ops)
            else:
                yield entry, None
                continue
            step = _step(entry, current, new)
            current = new
            started = True
        yield entry, step


def revision_steps(
    records: Sequence[RevisionRecord],
) -> tuple[RevisionStep, ...]:
    """:func:`_log_steps` restricted to the usable records -- the
    revisions-only walk with the added/removed bookkeeping every
    derived view needs precomputed once."""
    return tuple(
        step for _, step in _log_steps(records) if step is not None
    )


def state_walk(
    records: Sequence[RevisionRecord],
) -> tuple[tuple[RevisionRecord, tuple[str, ...]], ...]:
    """Each usable record paired with the FULL hash sequence after it."""
    return tuple(
        (step.record, step.hashes) for step in revision_steps(records)
    )


def current_hashes(records: Sequence[RevisionRecord]) -> tuple[str, ...]:
    states = state_walk(records)
    return states[-1][1] if states else ()


def texts_of(records: Sequence[RevisionRecord]) -> dict[str, str]:
    """Every hash the log can still name -> its raw text, last write
    wins (a re-emitted refresh supersedes; pre-ADR-054 entries yield
    normalized text until their hash is refreshed)."""
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
        payload: dict[str, Any] = {
            "kind": entry.kind, "hash": entry.hash, "value": entry.value,
            "at": entry.at, "by": entry.by,
        }
        if entry.start >= 0:
            payload["s"] = entry.start
            payload["e"] = entry.end
        return payload
    if isinstance(entry, CommentEntry):
        payload = {
            "kind": KIND_COMMENT, "id": entry.id, "hash": entry.hash,
            "text": entry.text, "at": entry.at, "by": entry.by,
        }
        if entry.start >= 0:
            payload["s"] = entry.start
            payload["e"] = entry.end
        return payload
    if isinstance(entry, CommentStateEntry):
        return {
            "kind": KIND_COMMENT_STATE, "id": entry.id,
            "value": entry.value, "at": entry.at, "by": entry.by,
        }
    return _record_payload(entry)


def _record_payload(record: RevisionRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seq": record.seq, "at": record.at, "author": record.author_kind,
        "detail": record.author_detail, "kind": record.kind,
        "v": RECORD_VERSION,  # forensics only; the lenient parser ignores it
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
                start=int(payload.get("s", -1)),
                end=int(payload.get("e", -1)),
            )
            allowed = STATUS_VALUES if kind == MARK_STATUS else INTENT_VALUES
            if not mark.hash or mark.value not in allowed:
                return None
            if mark.start >= 0 and mark.end <= mark.start:
                return None  # a mangled range degrades, never applies odd
            return mark
        if kind == KIND_COMMENT:
            comment = CommentEntry(
                id=str(payload["id"]),
                hash=str(payload.get("hash", "")),
                text=str(payload["text"]),
                at=str(payload["at"]),
                by=str(payload["by"]),
                start=int(payload.get("s", -1)),
                end=int(payload.get("e", -1)),
            )
            if not comment.id or not comment.text:
                return None
            if comment.start >= 0 and comment.end <= comment.start:
                return None
            return comment
        if kind == KIND_COMMENT_STATE:
            transition = CommentStateEntry(
                id=str(payload["id"]),
                value=str(payload["value"]),
                at=str(payload["at"]),
                by=str(payload["by"]),
            )
            if not transition.id or (
                transition.value not in COMMENT_STATE_VALUES
            ):
                return None
            return transition
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
    "replaced its ancestor", a brand-new paragraph has none). Word-free
    blocks stay out (separators share hashes; :func:`has_words`)."""
    texts = texts_of(records)
    entries: dict[str, BlameEntry] = {}
    final: tuple[str, ...] = ()
    for step in revision_steps(records):
        for added_hash in step.added:
            entries[added_hash] = BlameEntry(
                author_kind=step.record.author_kind,
                author_detail=step.record.author_detail,
                at=step.record.at,
                seq=step.record.seq,
                ancestor=step.ancestor(added_hash, texts),
            )
        final = step.hashes
    return {
        h: entry for h, entry in entries.items()
        if h in final and has_words(texts.get(h, ""))
    }


def closest(
    text: str, candidates: AbstractSet[str], texts: Mapping[str, str]
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


def _token_cache(
    texts: Mapping[str, str],
) -> Callable[[str], list[str]]:
    """Memoized hash -> the block's :func:`block_tokens`, shared walk
    scaffolding over one :func:`texts_of` view."""
    cache: dict[str, list[str]] = {}

    def tokens(block: str) -> list[str]:
        if block not in cache:
            cache[block] = block_tokens(texts.get(block, ""))
        return cache[block]

    return tokens


def apply_mark(
    values: list[str], value: str, start: int = -1, end: int = -1
) -> tuple[bool, bool]:
    """THE mark-application kernel: write ``value`` over the clamped
    token slice (``-1, -1`` = the whole block) of a per-token vector,
    in place. Returns ``(applied, changed)``: ``applied`` -- the
    clamped slice was nonempty (the fold stamps at/by on this; an empty
    slice is a range the current text no longer has); ``changed`` -- at
    least one token actually flipped (the historian's change-only
    no-op test: it skips a mark iff ``applied and not changed``)."""
    lo, hi = (0, len(values)) if start < 0 else (
        max(0, start), min(len(values), end)
    )
    if lo >= hi:
        return False, False
    changed = any(values[i] != value for i in range(lo, hi))
    values[lo:hi] = [value] * (hi - lo)
    return True, changed


_Payload = TypeVar("_Payload")


def _inherit(
    base: Sequence[_Payload],
    old_tokens: Sequence[str],
    new_tokens: Sequence[str],
    fill: _Payload,
) -> list[_Payload]:
    """Positional per-token inheritance (WP45/46): token-equal ranges
    copy the ancestor's payload, everything else takes ``fill``. A
    length-mismatched base (a hand-mangled log) degrades to uniform."""
    if len(base) != len(old_tokens):
        return [fill] * len(new_tokens)
    matcher = difflib.SequenceMatcher(
        a=list(old_tokens), b=list(new_tokens), autojunk=False
    )
    out: list[_Payload] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(base[i1:i2])
        else:
            out.extend([fill] * (j2 - j1))
    return out


def token_states(entries: Sequence[LogEntry]) -> dict[str, TokenState]:
    """The fold (WP42, token-grained since WP46): interleaved log
    entries -> per-token status/intent for the FINAL hash sequence.

    Review state lives on TOKENS and follows them through edits by the
    same positional inheritance authorship uses: a revision's added
    blocks similarity-match its removed blocks through :func:`closest`
    (no match consumption -- lineage, not pairing); token-equal ranges
    carry their state, new tokens default (``human``/``flexible`` when
    a human typed them, ``raw_ai``/``flexible`` for the model -- so an
    AI edit voids ``approved`` exactly on the words it changed, and
    approval on untouched words survives). A mark applies to its token
    range (whole block when unranged) iff its hash is live at that fold
    position; stale marks and dead ranges fold to nothing. A hash seen
    before keeps its state when restored verbatim.
    """
    records = [e for e in entries if isinstance(e, RevisionRecord)]
    texts = texts_of(records)
    tokens = _token_cache(texts)
    statuses: dict[str, list[str]] = {}
    intents: dict[str, list[str]] = {}
    stamps: dict[str, dict[str, str]] = {}
    current: tuple[str, ...] = ()
    for entry, step in _log_steps(entries):
        if isinstance(entry, SectionMark):
            if entry.hash not in current:
                continue
            target = (
                statuses if entry.kind == MARK_STATUS else intents
            )[entry.hash]
            applied, _ = apply_mark(
                target, entry.value, entry.start, entry.end
            )
            if not applied:
                continue  # a range the current text no longer has
            stamp = stamps.setdefault(entry.hash, {})
            prefix = "status" if entry.kind == MARK_STATUS else "intent"
            stamp[f"{prefix}_at"] = entry.at
            stamp[f"{prefix}_by"] = entry.by
            continue
        if step is None:
            continue  # comments and future kinds are inert here
        default_status = (
            STATUS_HUMAN if step.record.author_kind == AUTHOR_HUMAN
            else STATUS_RAW_AI
        )
        for added in step.added:
            if added in statuses:
                continue  # restored verbatim: state rides the hash
            ancestor = step.ancestor(added, texts)
            if ancestor and ancestor in statuses:
                old_tokens = tokens(ancestor)
                statuses[added] = _inherit(
                    statuses[ancestor], old_tokens, tokens(added),
                    default_status,
                )
                intents[added] = _inherit(
                    intents[ancestor], old_tokens, tokens(added),
                    INTENT_FLEXIBLE,
                )
                stamps[added] = dict(stamps.get(ancestor, {}))
            else:
                statuses[added] = [default_status] * len(tokens(added))
                intents[added] = [INTENT_FLEXIBLE] * len(tokens(added))
                stamps[added] = {
                    "status_at": step.record.at,
                    "status_by": step.record.author_detail,
                }
        current = step.hashes
    result: dict[str, TokenState] = {}
    for block in set(current):
        status = statuses[block]
        stamp = stamps.get(block, {})
        result[block] = TokenState(
            status=tuple(status),
            intent=tuple(intents.get(block, ())),
            status_at=stamp.get("status_at", ""),
            status_by=stamp.get("status_by", ""),
            intent_at=stamp.get("intent_at", ""),
            intent_by=stamp.get("intent_by", ""),
        )
    return result


def section_states(entries: Sequence[LogEntry]) -> dict[str, SectionState]:
    """Block-level BADGES derived from the token fold (WP46): a block is
    ``approved``/``human`` only when every token agrees (mixed reads
    ``raw_ai`` -- the word-level view shows the split); intent is the
    strictest token's, so one locked word makes the block read locked."""
    derived: dict[str, SectionState] = {}
    for block, state in token_states(entries).items():
        status = STATUS_RAW_AI
        if state.status and all(s == STATUS_APPROVED for s in state.status):
            status = STATUS_APPROVED
        elif state.status and all(s == STATUS_HUMAN for s in state.status):
            status = STATUS_HUMAN
        if INTENT_LOCKED in state.intent:
            intent = INTENT_LOCKED
        elif INTENT_NEEDS_CHANGE in state.intent:
            intent = INTENT_NEEDS_CHANGE
        else:
            intent = INTENT_FLEXIBLE
        derived[block] = SectionState(
            status=status, intent=intent,
            status_at=state.status_at, status_by=state.status_by,
            intent_at=state.intent_at, intent_by=state.intent_by,
        )
    return derived


def locked_runs(
    states: Mapping[str, TokenState], texts: Mapping[str, str]
) -> dict[str, tuple[str, ...]]:
    """Per block: the contiguous LOCKED token runs' text (edge-stripped,
    over the block's raw text) -- what the guard demands survive
    verbatim and what the context block shows the model (WP46)."""
    runs: dict[str, tuple[str, ...]] = {}
    for block, state in states.items():
        if INTENT_LOCKED not in state.intent:
            continue
        tokens = block_tokens(texts.get(block, ""))
        if len(tokens) != len(state.intent):
            continue  # mangled log: no run to demand
        found: list[str] = []
        run: list[str] = []
        for token, intent in zip(tokens, state.intent, strict=True):
            if intent == INTENT_LOCKED:
                run.append(token)
            elif run:
                found.append("".join(run).strip())
                run = []
        if run:
            found.append("".join(run).strip())
        cleaned = tuple(r for r in found if r)
        if cleaned:
            runs[block] = cleaned
    return runs


def missing_locked(
    states: Mapping[str, TokenState],
    texts: Mapping[str, str],
    new_body: str,
) -> tuple[tuple[str, str], ...]:
    """LOCKED runs absent from ``new_body`` as ``(hash, run text)`` --
    an order-insensitive verbatim-presence check (moving locked text is
    fine, even into another paragraph; changing or deleting it is not),
    still no diffing (WP42, refined by WP46). Runs and body are raw
    text; both sides fold whitespace runs to single spaces before the
    substring check (ADR 010 forbids byte-exact comparison -- the store
    reflows whitespace, and reflow is not a locked-text violation)."""
    haystack = _WS_RUN.sub(" ", new_body)
    return tuple(
        (block, run)
        for block, runs in sorted(locked_runs(states, texts).items())
        for run in runs
        if _WS_RUN.sub(" ", run) not in haystack
    )


def word_token_authors(
    records: Sequence[RevisionRecord],
) -> dict[str, tuple[str, ...]]:
    """Final-state hash -> one author per token (WP45/46) -- derived,
    never stored: one forward pass over the log carrying a per-token
    author for EVERY hash ever seen (a removed-then-restored block
    keeps its authorship; identity is the hash). An added block
    inherits its ancestor's authors on token-equal ranges and takes the
    introducing revision's author elsewhere. Ancestors resolve through
    :func:`closest` WITHOUT consuming matches: a paragraph split in two
    lets BOTH halves inherit the original's words -- authorship is
    lineage, not a one-to-one pairing (``revision_diff``'s display
    pairing differs deliberately). The prose bridge merges these into
    display spans and joins them with token review states. No
    word-free filter here; callers apply :func:`has_words` where
    display rules demand it."""
    texts = texts_of(records)
    tokens = _token_cache(texts)
    token_authors: dict[str, tuple[str, ...]] = {}
    final: tuple[str, ...] = ()
    for step in revision_steps(records):
        for added in step.added:
            if added in token_authors:
                continue  # restored verbatim: authorship rides the hash
            ancestor = step.ancestor(added, texts)
            base = token_authors.get(ancestor, ()) if ancestor else ()
            old_tokens = tokens(ancestor) if base else []
            token_authors[added] = tuple(
                _inherit(base, old_tokens, tokens(added),
                         step.record.author_kind)
            )
        final = step.hashes
    return {
        block: token_authors[block]
        for block in set(final) if block in token_authors
    }


@dataclass(slots=True)
class _CommentFold:
    """One comment's mutable state inside the fold walk (internal)."""

    entry: CommentEntry
    state: str = COMMENT_OPEN
    state_at: str = ""
    state_by: str = ""
    anchor: str = ""          # last known anchor hash; "" = born detached
    attached: bool = False
    flags: list[bool] | None = None  # per anchor token; None = whole block


def _fold_comments(
    entries: Sequence[LogEntry], texts: Mapping[str, str]
) -> dict[str, _CommentFold]:
    """The comment fold walk (WP50): file-ordered comments with their
    anchors ridden forward through every revision. ``texts`` is the
    caller's :func:`texts_of` view over the walked records (compaction
    passes its dropped-era subset). Shared by :func:`comment_states`
    and compaction's hoist-with-rewrite."""
    tokens = _token_cache(texts)

    def _attach(fold: _CommentFold) -> None:
        fold.attached = True
        entry = fold.entry
        if (
            fold.flags is None and entry.start >= 0
            and fold.anchor == entry.hash
        ):
            anchor_tokens = tokens(fold.anchor)
            lo = max(0, entry.start)
            hi = min(len(anchor_tokens), entry.end)
            if lo < hi:
                fold.flags = [
                    lo <= i < hi for i in range(len(anchor_tokens))
                ]

    folds: dict[str, _CommentFold] = {}
    current: tuple[str, ...] = ()
    for entry, step in _log_steps(entries):
        if isinstance(entry, CommentEntry):
            if entry.id in folds:
                continue  # first line owns the identity
            fold = _CommentFold(entry=entry, anchor=entry.hash)
            if entry.hash and entry.hash in current:
                _attach(fold)
            folds[entry.id] = fold
            continue
        if isinstance(entry, CommentStateEntry):
            fold_or_none = folds.get(entry.id)
            if fold_or_none is None or fold_or_none.state == COMMENT_RESOLVED:
                continue  # unknown id, or resolved is terminal
            fold_or_none.state = entry.value
            fold_or_none.state_at = entry.at
            fold_or_none.state_by = entry.by
            continue
        if step is None:
            continue
        new_set = set(step.hashes)
        ancestor_of = step.ancestors(texts)
        for fold in folds.values():
            if fold.state == COMMENT_RESOLVED:
                continue
            if not fold.attached:
                if fold.anchor and fold.anchor in new_set:
                    _attach(fold)  # the commented text came back verbatim
                continue
            if fold.anchor in new_set:
                continue  # anchor still live (moves are free)
            successors = [
                h for h in step.added
                if ancestor_of.get(h) == fold.anchor
            ]
            if fold.flags is not None:
                old_tokens = tokens(fold.anchor)
                for successor in successors:
                    inherited = _inherit(
                        fold.flags, old_tokens, tokens(successor), False
                    )
                    if any(inherited):
                        fold.anchor = successor
                        fold.flags = inherited
                        break
                else:
                    if successors:
                        # the commented words themselves were deleted:
                        # fall back to the whole successor block
                        fold.anchor = successors[0]
                        fold.flags = None
                    else:
                        fold.attached = False  # detached until resolved
            elif successors:
                fold.anchor = successors[0]
            else:
                fold.attached = False
        current = step.hashes
    return folds


def _flag_bounds(flags: Sequence[bool]) -> tuple[int, int]:
    """A flag vector's bounding ``(s, e)`` token range; ``(-1, -1)``
    when nothing is flagged."""
    marked = [i for i, flag in enumerate(flags) if flag]
    if not marked:
        return -1, -1
    return marked[0], marked[-1] + 1


def comment_states(entries: Sequence[LogEntry]) -> tuple[CommentState, ...]:
    """The live comments (WP50): open and addressed, file order, each
    with its CURRENT anchor -- a comment rides edits through the same
    ``closest``/positional inheritance the review fold uses, falls back
    to its successor's whole block when the commented words are deleted,
    detaches (``hash=""``) when the block vanishes with no successor,
    and re-attaches if the hash reappears verbatim. Resolved comments
    fold away (the log keeps them until compaction)."""
    records = [e for e in entries if isinstance(e, RevisionRecord)]
    texts = texts_of(records)
    tokens = _token_cache(texts)
    states: list[CommentState] = []
    for fold in _fold_comments(entries, texts).values():
        if fold.state == COMMENT_RESOLVED:
            continue
        anchor = fold.anchor if fold.attached else ""
        start = end = -1
        if (
            anchor and fold.flags is not None
            and len(fold.flags) == len(tokens(anchor))
        ):
            start, end = _flag_bounds(fold.flags)
        states.append(CommentState(
            id=fold.entry.id, text=fold.entry.text, state=fold.state,
            hash=anchor, start=start, end=end,
            at=fold.entry.at, by=fold.entry.by,
            state_at=fold.state_at, state_by=fold.state_by,
        ))
    return tuple(states)


def rollup_base(entries: Sequence[LogEntry]) -> tuple[LogEntry, ...] | None:
    """The log minus a coalescible human tail (WP44), or None.

    Consecutive human revisions collapse into ONE pending revision: the
    historian drops the tail and re-records against this trimmed base
    (same seq -- keyframe cadence is seq-derived, so it survives). A
    pending revision coalesces only while it is the LAST log entry: a
    model revision or any section mark after it solidifies it -- a mark
    pins exactly the text that was reviewed.

    Refuses (None) when the trimmed log would still contain records but
    no usable keyframe -- the post-compaction ``truncated-marker +
    keyframe`` shape, where the human tail IS the first kept keyframe:
    re-recording over that base would regress seq behind the marker and
    could strand deltas with no keyframe ancestor.
    """
    if not entries:
        return None
    tail = entries[-1]
    if not isinstance(tail, RevisionRecord):
        return None  # a mark solidified the pending revision
    if tail.author_kind != AUTHOR_HUMAN:
        return None
    if tail.kind not in (KIND_KEYFRAME, KIND_DELTA):
        return None  # truncated marker or lenient-parse oddity
    trimmed = tuple(entries[:-1])
    records = [e for e in trimmed if isinstance(e, RevisionRecord)]
    if records and not state_walk(records):
        return None
    return trimmed


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

    Dropped-era COMMENTS (WP50) hoist differently: a live (unresolved)
    comment's anchor may have migrated during the dropped era, so its
    line is REWRITTEN -- same id/text/at/by, anchor and range replaced
    by the fold's state as of the first kept keyframe (last-known
    anchor kept when detached, so a verbatim restore in the kept era
    still re-attaches) -- plus one ``addressed`` transition line when
    set. Resolved comments drop with their era.
    """
    kept = list(entries)
    if len(render_log(kept)) <= cap:
        return tuple(entries)
    records = [e for e in entries if isinstance(e, RevisionRecord)]
    all_texts = texts_of(records)
    while len(render_log(kept)) > cap:
        next_keyframe = next(
            (i for i, e in enumerate(kept)
             if i and isinstance(e, RevisionRecord)
             and e.kind == KIND_KEYFRAME),
            None,
        )
        if next_keyframe is None:
            break  # nothing older left to shed; over-cap is the lesser evil
        kept = kept[next_keyframe:]
    dropped_all = list(entries)[:len(entries) - len(kept)]
    dropped_records = [
        e for e in dropped_all if isinstance(e, RevisionRecord)
    ]
    if not dropped_records:
        return tuple(entries)
    # seq never decreases in file order (append-only; roll-up rewrites
    # the SAME seq), so the max over everything dropped is the seq the
    # truncation marker records.
    dropped_through = max(r.seq for r in dropped_records)
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
    hoisted = [
        m for m in dropped_all
        if isinstance(m, SectionMark) and m.hash in live
    ]
    kept_comment_ids = {
        e.id for e in kept if isinstance(e, CommentEntry)
    }
    hoisted_comments: list[LogEntry] = []
    dropped_texts = texts_of([*dropped_records, first])
    for cid, fold in _fold_comments(
        (*dropped_all, first), dropped_texts
    ).items():
        if cid in kept_comment_ids or fold.state == COMMENT_RESOLVED:
            continue
        start, end = (
            _flag_bounds(fold.flags) if fold.flags is not None else (-1, -1)
        )
        hoisted_comments.append(CommentEntry(
            id=cid, hash=fold.anchor, text=fold.entry.text,
            at=fold.entry.at, by=fold.entry.by, start=start, end=end,
        ))
        if fold.state == COMMENT_ADDRESSED:
            hoisted_comments.append(CommentStateEntry(
                id=cid, value=COMMENT_ADDRESSED,
                at=fold.state_at, by=fold.state_by,
            ))
    marker = RevisionRecord(
        seq=dropped_through, at=first.at, author_kind=AUTHOR_MODEL,
        author_detail="compaction", kind=KIND_TRUNCATED,
    )
    return (marker, first, *hoisted, *hoisted_comments, *kept[1:])
