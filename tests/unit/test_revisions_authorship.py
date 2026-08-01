"""Derived word-level authorship (WP45): who typed each word of every
current block, walked off the revision log -- display derivation only,
nothing stored. Pure and fast, like the other revision suites."""

from __future__ import annotations

from graph_context.domain import revisions
from graph_context.domain.revisions import (
    RevisionRecord,
    block_hash,
    body_blocks,
    current_hashes,
    next_record,
    normalize_block,
    word_authorship,
)

PARA_A = "The city fell quiet before the siege began, every gate barred."
PARA_B = "Mira counted the engines twice; one was missing from the yard."
# A light human touch on PARA_A: one word changes.
PARA_A_EDIT = "The city fell silent before the siege began, every gate barred."
# The model's later pass over the human-touched sentence.
PARA_A_MODEL = "The city fell silent before the long siege began, every gate barred."


def _h(text: str) -> str:
    return block_hash(normalize_block(text))


def _pairs(*paragraphs: str) -> tuple[tuple[str, str], ...]:
    return body_blocks("\n\n".join(paragraphs))


def _grow(
    log: list[RevisionRecord], paragraphs: list[str], *,
    author: str = revisions.AUTHOR_MODEL, at: str = "T",
) -> None:
    prev = current_hashes(log)
    log.append(next_record(
        prev, log[-1].seq if log else 0, _pairs(*paragraphs),
        at=at, author_kind=author, author_detail=author,
        known_texts=revisions.texts_of(log),
    ))


def _text_of(spans: tuple[tuple[str, str], ...]) -> str:
    return "".join(text for _, text in spans)


def _authors_of(spans: tuple[tuple[str, str], ...]) -> set[str]:
    return {author for author, _ in spans}


class TestWordAuthorship:
    def test_a_fresh_block_is_wholly_its_author(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        spans = word_authorship(log)[_h(PARA_A)]
        assert _authors_of(spans) == {revisions.AUTHOR_MODEL}
        assert _text_of(spans) == normalize_block(PARA_A)

    def test_a_human_edit_marks_only_the_changed_words(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        spans = word_authorship(log)[_h(PARA_A_EDIT)]
        human = "".join(t for a, t in spans if a == revisions.AUTHOR_HUMAN)
        assert human.strip() == "silent"
        assert _text_of(spans) == normalize_block(PARA_A_EDIT)

    def test_a_model_edit_over_human_text_keeps_the_human_words(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        _grow(log, [PARA_A_MODEL])
        spans = word_authorship(log)[_h(PARA_A_MODEL)]
        human = "".join(t for a, t in spans if a == revisions.AUTHOR_HUMAN)
        model = "".join(t for a, t in spans if a == revisions.AUTHOR_MODEL)
        assert "silent" in human  # survived the model's pass
        assert "long" in model    # the model's own insertion
        assert _text_of(spans) == normalize_block(PARA_A_MODEL)

    def test_spans_concatenate_and_adjacent_authors_merge(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_A_EDIT, PARA_B], author=revisions.AUTHOR_HUMAN)
        for spans in word_authorship(log).values():
            authors = [a for a, _ in spans]
            assert all(x != y for x, y in zip(authors, authors[1:], strict=False))

    def test_a_split_paragraph_keeps_both_halves_ancestry(self) -> None:
        first = "The city fell quiet before the siege began that year."
        second = "Every gate stood barred and the walls were watched all night."
        combined = f"{first} {second}"
        log: list[RevisionRecord] = []
        _grow(log, [combined])
        # The human splits ONE model paragraph into two blocks.
        _grow(log, [first, second], author=revisions.AUTHOR_HUMAN)
        spans = word_authorship(log)
        # Both halves inherit the model's words (no greedy consumption).
        assert revisions.AUTHOR_MODEL in _authors_of(spans[_h(first)])
        assert revisions.AUTHOR_MODEL in _authors_of(spans[_h(second)])

    def test_no_ancestor_means_uniform_authorship(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A, PARA_B], author=revisions.AUTHOR_HUMAN)
        assert _authors_of(word_authorship(log)[_h(PARA_B)]) == {
            revisions.AUTHOR_HUMAN,
        }

    def test_short_blocks_stay_out(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, "* * *"])
        assert _h("* * *") not in word_authorship(log)

    def test_truncated_history_degrades_to_the_keyframe_author(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        for i in range(90):
            filler = f"Filler paragraph {i}: " + "words fill the siege log " * 40
            _grow(log, [PARA_A_EDIT, filler])
        compacted = revisions.compact(log, len(revisions.render_log(log)) // 3)
        records = [
            e for e in compacted if isinstance(e, RevisionRecord)
        ]
        spans = word_authorship(records)[_h(PARA_A_EDIT)]
        # Pre-truncation lineage is gone: the block reads as wholly the
        # first kept keyframe's author. No crash is the contract.
        assert len(_authors_of(spans)) == 1

    def test_a_removed_then_restored_block_keeps_its_authorship(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        _grow(log, [PARA_B])                    # the edited block removed
        _grow(log, [PARA_A_EDIT, PARA_B])       # model restores it verbatim
        spans = word_authorship(log)[_h(PARA_A_EDIT)]
        human = "".join(t for a, t in spans if a == revisions.AUTHOR_HUMAN)
        assert "silent" in human  # authorship rode the hash, not the walk
