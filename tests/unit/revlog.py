"""Shared builders for the revision-log unit suites (test_revisions*).

The fixture paragraphs and grow-a-log helpers below were re-declared
verbatim in each suite; they live here once now. Fixtures that
deliberately differ between suites (e.g. the authorship suite's own
one-word edit of PARA_A) stay file-local, and so do the suite-specific
builders (marks, comments).
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_context.domain import revisions
from graph_context.domain.revisions import (
    LogEntry,
    RevisionRecord,
    block_hash,
    body_blocks,
    current_hashes,
    next_record,
    normalize_block,
)

PARA_A = "The city fell quiet before the siege began, every gate barred."
PARA_B = "Mira counted the engines twice; one was missing from the yard."
PARA_C = "Rain came at dusk and the watch fires guttered along the wall."
# A human's light touch on PARA_A -- similar enough for lineage.
PARA_A_EDIT = "The city fell quiet before the siege started, every gate barred."


def h(paragraph: str) -> str:
    return block_hash(normalize_block(paragraph))


def pairs(*paragraphs: str) -> tuple[tuple[str, str], ...]:
    return body_blocks("\n\n".join(paragraphs))


def records(entries: Sequence[LogEntry]) -> list[RevisionRecord]:
    return [e for e in entries if isinstance(e, RevisionRecord)]


def grow(
    entries: list[LogEntry] | list[RevisionRecord],
    body_paragraphs: list[str], *,
    author: str = revisions.AUTHOR_MODEL, detail: str = "m", at: str = "T",
) -> None:
    """Append the record for a new body state to the log (test-side
    convenience mirroring the historian's bookkeeping). Works on a
    records-only log too -- marks and comments are filtered out."""
    recs = records(entries)
    prev = current_hashes(recs)
    entries.append(next_record(
        prev, recs[-1].seq if recs else 0, pairs(*body_paragraphs),
        at=at, author_kind=author, author_detail=detail,
        known_texts=revisions.texts_of(recs),
    ))
