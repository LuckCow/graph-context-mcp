"""Revision-history domain rules (WP41, ADR 049): segmentation,
normalized block identity, the keyframe+delta log, derived blame, and
compaction. Pure and fast -- every store quirk the normalization must
absorb (A9 heading flatten, A13 fence-tag drop, whitespace drift) is
exercised as text shapes, no adapter in sight."""

from __future__ import annotations

from graph_context.domain import revisions
from graph_context.domain.revisions import (
    RevisionRecord,
    blame,
    block_hash,
    compact,
    current_hashes,
    hash_sequence,
    next_record,
    normalize_block,
    parse_log,
    parse_tracked_types,
    render_log,
    split_blocks,
)

PARA_A = "The city fell quiet before the siege began, every gate barred."
PARA_B = "Mira counted the engines twice; one was missing from the yard."
PARA_C = "Rain came at dusk and the watch fires guttered along the wall."


def _pairs(*paragraphs: str) -> tuple[tuple[str, str], ...]:
    return hash_sequence("\n\n".join(paragraphs))


def _grow(
    log: list[RevisionRecord], body_paragraphs: list[str], *,
    author: str = revisions.AUTHOR_MODEL, detail: str = "m", at: str = "T",
) -> None:
    """Append the record for a new body state to ``log`` (test-side
    convenience mirroring the historian's bookkeeping)."""
    prev = current_hashes(log)
    known = frozenset(revisions.texts_of(log))
    log.append(next_record(
        prev, log[-1].seq if log else 0, _pairs(*body_paragraphs),
        at=at, author_kind=author, author_detail=detail, known_hashes=known,
    ))


class TestSegmentation:
    def test_blank_lines_split_blocks(self) -> None:
        assert split_blocks(f"{PARA_A}\n\n{PARA_B}") == (PARA_A, PARA_B)

    def test_fenced_blocks_stay_whole_across_blank_lines(self) -> None:
        body = f"{PARA_A}\n\n```\nline one\n\nline two\n```\n\n{PARA_B}"
        blocks = split_blocks(body)
        assert len(blocks) == 3
        assert blocks[1] == "```\nline one\n\nline two\n```"

    def test_heading_flatten_does_not_change_identity(self) -> None:
        """Quirk A9: the store flattens a first-line heading; the block
        must hash the same before and after."""
        assert normalize_block("## The Siege") == normalize_block("The Siege")

    def test_fence_info_strings_do_not_change_identity(self) -> None:
        """Quirk A13: the store drops fence language tags."""
        assert (normalize_block("```python\nx = 1\n```")
                == normalize_block("```\nx = 1\n```"))

    def test_whitespace_drift_does_not_change_identity(self) -> None:
        assert (normalize_block("a  spaced\tline  ")
                == normalize_block("a spaced line"))

    def test_reordering_keeps_every_hash(self) -> None:
        forward = {h for h, _ in _pairs(PARA_A, PARA_B, PARA_C)}
        backward = {h for h, _ in _pairs(PARA_C, PARA_B, PARA_A)}
        assert forward == backward


class TestLog:
    def test_first_record_is_a_keyframe(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        assert log[0].kind == revisions.KIND_KEYFRAME
        assert log[0].seq == 1
        assert len(log[0].new_blocks) == 2

    def test_deltas_reconstruct_the_current_state(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_A, PARA_C, PARA_B])   # insert
        _grow(log, [PARA_C, PARA_B])           # delete
        assert log[1].kind == revisions.KIND_DELTA
        assert current_hashes(log) == tuple(h for h, _ in _pairs(PARA_C, PARA_B))

    def test_new_blocks_carry_only_first_seen_text(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_B, PARA_A])  # reorder: nothing new
        assert set(log[1].new_blocks.values()) == {normalize_block(PARA_B)}
        assert log[2].new_blocks == {}

    def test_keyframes_recur_on_the_interval(self) -> None:
        log: list[RevisionRecord] = []
        for i in range(revisions.KEYFRAME_INTERVAL + 2):
            _grow(log, [PARA_A, f"Revision paragraph number {i}, growing."])
        kinds = [r.kind for r in log]
        assert kinds[0] == revisions.KIND_KEYFRAME
        assert kinds[revisions.KEYFRAME_INTERVAL - 1] == revisions.KIND_KEYFRAME
        assert set(kinds[1:revisions.KEYFRAME_INTERVAL - 1]) == {
            revisions.KIND_DELTA
        }

    def test_render_parse_round_trip(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_A, PARA_C])
        parsed = parse_log(render_log(log))
        assert parsed.skipped == 0
        assert list(parsed.records) == log

    def test_round_trip_survives_the_store_normalization_shapes(self) -> None:
        """The rendered log itself is a body: no fence info string to
        lose (A13) and no heading-shaped first line to flatten (A9)."""
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        rendered = render_log(log)
        assert not rendered.startswith("#")
        fence_lines = [
            line for line in rendered.splitlines()
            if line.startswith("```")
        ]
        assert fence_lines == ["```", "```"]

    def test_a_mangled_line_is_skipped_not_fatal(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_A, PARA_C])
        lines = render_log(log).splitlines()
        lines[3] = "{ human broke this line"   # inside the fence
        parsed = parse_log("\n".join(lines))
        assert parsed.skipped == 1
        assert len(parsed.records) == 1

    def test_text_outside_the_fence_is_ignored(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        body = render_log(log) + "\n\na human note under the log"
        assert parse_log(body).skipped == 0


class TestBlame:
    def test_blocks_blame_their_introducing_author(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A], detail="fable · prose · u1")
        _grow(log, [PARA_A, PARA_B], author=revisions.AUTHOR_HUMAN,
              detail="human")
        by_hash = blame(log)
        a_hash = block_hash(normalize_block(PARA_A))
        b_hash = block_hash(normalize_block(PARA_B))
        assert by_hash[a_hash].author_kind == revisions.AUTHOR_MODEL
        assert by_hash[a_hash].author_detail == "fable · prose · u1"
        assert by_hash[b_hash].author_kind == revisions.AUTHOR_HUMAN

    def test_moving_a_block_keeps_its_blame(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_B, PARA_A], author=revisions.AUTHOR_HUMAN)
        for entry in blame(log).values():
            assert entry.author_kind == revisions.AUTHOR_MODEL

    def test_an_edited_block_blames_the_editor_with_lineage(self) -> None:
        edited = PARA_A.replace("every gate barred", "the gates left open")
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [edited, PARA_B], author=revisions.AUTHOR_HUMAN)
        entry = blame(log)[block_hash(normalize_block(edited))]
        assert entry.author_kind == revisions.AUTHOR_HUMAN
        assert entry.ancestor == block_hash(normalize_block(PARA_A))

    def test_a_brand_new_block_has_no_ancestor(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A, PARA_C])
        assert blame(log)[block_hash(normalize_block(PARA_C))].ancestor == ""

    def test_separators_stay_out_of_blame(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, "***", PARA_B])
        assert block_hash(normalize_block("***")) not in blame(log)

    def test_removed_blocks_leave_the_blame_map(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A, PARA_B])
        _grow(log, [PARA_B])
        assert block_hash(normalize_block(PARA_A)) not in blame(log)


class TestCompaction:
    def _churned(self, rounds: int) -> list[RevisionRecord]:
        log: list[RevisionRecord] = []
        for i in range(rounds):
            filler = (
                f"Filler paragraph {i}: " + "words fill the siege log " * 40
            )
            _grow(log, [PARA_A, filler], at=f"T{i}")
        return log

    def test_under_cap_is_untouched(self) -> None:
        log = self._churned(5)
        assert compact(log) == tuple(log)

    def test_over_cap_drops_oldest_and_marks_truncation(self) -> None:
        log = self._churned(90)
        cap = len(render_log(log)) // 2
        compacted = compact(log, cap)
        assert len(render_log(list(compacted))) <= cap
        assert compacted[0].kind == revisions.KIND_TRUNCATED
        assert compacted[1].kind == revisions.KIND_KEYFRAME
        assert current_hashes(compacted) == current_hashes(log)

    def test_surviving_blocks_keep_blameable_text(self) -> None:
        """A block introduced in the dropped era but still current must
        keep its text (re-carried onto the first kept keyframe) so
        blame -- and Phase 3's status maps -- stay complete."""
        log = self._churned(90)
        compacted = compact(log, len(render_log(log)) // 2)
        a_hash = block_hash(normalize_block(PARA_A))
        assert a_hash in blame(compacted)
        assert revisions.texts_of(compacted)[a_hash] == normalize_block(PARA_A)


class TestTrackedTypes:
    def test_separators_and_dedupe(self) -> None:
        raw = "Chapter, Character\nchapter; Location,,"
        assert parse_tracked_types(raw) == ("Chapter", "Character", "Location")

    def test_empty_input_tracks_nothing(self) -> None:
        assert parse_tracked_types("") == ()
        assert parse_tracked_types("  ,\n ") == ()
