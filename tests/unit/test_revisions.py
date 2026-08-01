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
    body_blocks,
    compact,
    current_hashes,
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
    return body_blocks("\n\n".join(paragraphs))


def _grow(
    log: list[RevisionRecord], body_paragraphs: list[str], *,
    author: str = revisions.AUTHOR_MODEL, detail: str = "m", at: str = "T",
) -> None:
    """Append the record for a new body state to ``log`` (test-side
    convenience mirroring the historian's bookkeeping)."""
    prev = current_hashes(log)
    log.append(next_record(
        prev, log[-1].seq if log else 0, _pairs(*body_paragraphs),
        at=at, author_kind=author, author_detail=detail,
        known_texts=revisions.texts_of(log),
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


class TestRollupBase:
    """WP44: which logs allow coalescing a pending human tail."""

    def _human_tail(self) -> list[revisions.LogEntry]:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A, PARA_B], author=revisions.AUTHOR_HUMAN)
        return list(log)

    def test_a_human_tail_yields_the_trimmed_log(self) -> None:
        entries = self._human_tail()
        assert revisions.rollup_base(entries) == tuple(entries[:-1])

    def test_a_model_tail_does_not_roll_up(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A, PARA_B])  # model author
        assert revisions.rollup_base(log) is None

    def test_a_mark_after_the_tail_solidifies_it(self) -> None:
        entries = self._human_tail()
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_STATUS,
            hash=block_hash(normalize_block(PARA_B)),
            value="approved", at="T", by="user",
        ))
        assert revisions.rollup_base(entries) is None

    def test_a_truncated_tail_never_rolls_up(self) -> None:
        marker = RevisionRecord(
            seq=5, at="T", author_kind=revisions.AUTHOR_HUMAN,
            author_detail="human", kind=revisions.KIND_TRUNCATED,
        )
        assert revisions.rollup_base([marker]) is None

    def test_an_empty_log_does_not_roll_up(self) -> None:
        assert revisions.rollup_base([]) is None

    def test_the_sole_first_keyframe_rolls_up_to_an_empty_log(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A], author=revisions.AUTHOR_HUMAN)
        assert revisions.rollup_base(log) == ()

    def test_a_marker_then_keyframe_log_refuses_to_roll_up(self) -> None:
        # Post-compaction shape: the human tail IS the first kept
        # keyframe; re-recording over the bare marker would regress seq.
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A], author=revisions.AUTHOR_HUMAN)
        keyframe = log[0]
        marker = RevisionRecord(
            seq=19, at="T", author_kind=revisions.AUTHOR_MODEL,
            author_detail="compaction", kind=revisions.KIND_TRUNCATED,
        )
        assert revisions.rollup_base([marker, keyframe]) is None


class TestRawIndexing:
    """ADR 054: identity hashes stay normalized, stored texts and every
    text-indexed artifact go raw."""

    def test_new_blocks_store_raw_text(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, ["## A Heading\nwith a body line."])
        assert list(log[0].new_blocks.values()) == [
            "## A Heading\nwith a body line."
        ]

    def test_a_stored_text_drift_re_emits_on_a_hash_equal_body(self) -> None:
        """One mechanism refreshes store rewrites AND migrates v1 logs:
        a hash-equal pair whose stored text differs re-emits raw."""
        log: list[RevisionRecord] = []
        heading = "## Tournament Morning\nThe yard filled before dawn."
        _grow(log, [heading])
        # Simulate a pre-ADR-054 log: stored text is the normalization.
        v1 = RevisionRecord(
            seq=1, at="T", author_kind=revisions.AUTHOR_MODEL,
            author_detail="m", kind=revisions.KIND_KEYFRAME,
            hashes=log[0].hashes,
            new_blocks={h: normalize_block(t)
                        for h, t in log[0].new_blocks.items()},
        )
        refresh = revisions.next_record(
            v1.hashes, 1, _pairs(heading),
            at="T2", author_kind=revisions.AUTHOR_HUMAN, author_detail="h",
            known_texts=revisions.texts_of([v1]),
        )
        assert refresh.new_blocks == {log[0].hashes[0]: heading}
        assert revisions.texts_of([v1, refresh])[log[0].hashes[0]] == heading

    def test_an_unchanged_body_emits_no_blocks(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        _grow(log, [PARA_A])
        assert log[1].new_blocks == {}

    def test_records_carry_the_version_tag(self) -> None:
        log: list[RevisionRecord] = []
        _grow(log, [PARA_A])
        assert '"v":2' in render_log(log)


class TestHasWords:
    def test_separators_have_no_words(self) -> None:
        assert not revisions.has_words("* * *")
        assert not revisions.has_words("---")
        assert not revisions.has_words("")

    def test_short_prose_has_words(self) -> None:
        assert revisions.has_words("No.")
        assert revisions.has_words("She ran.")


class TestBlockOffsets:
    def test_offsets_slice_the_raw_body(self) -> None:
        body = f"# Title\n\n{PARA_A}\n\n```python\ncode\n\nmore\n```\n\n{PARA_B}"
        offsets = revisions.block_offsets(body)
        assert [body[s:e] for _, s, e in offsets] == [
            "# Title", PARA_A, "```python\ncode\n\nmore\n```", PARA_B,
        ]
        assert [h for h, _, _ in offsets] == [
            h for h, _ in revisions.body_blocks(body)
        ]

    def test_duplicate_paragraphs_repeat_their_hash(self) -> None:
        body = f"{PARA_A}\n\n{PARA_A}"
        offsets = revisions.block_offsets(body)
        assert len(offsets) == 2
        assert offsets[0][0] == offsets[1][0]
        assert offsets[0][1:] != offsets[1][1:]

    def test_trailing_and_leading_blank_lines_are_skipped(self) -> None:
        body = f"\n\n{PARA_A}\n\n\n"
        offsets = revisions.block_offsets(body)
        assert len(offsets) == 1
        _, s, e = offsets[0]
        assert body[s:e] == PARA_A


class TestCharRangeToTokens:
    def test_a_range_maps_to_the_tokens_it_touches(self) -> None:
        text = "one two three"
        # "two" spans chars 4..7 -> token 1
        assert revisions.char_range_to_tokens(text, 4, 7) == (1, 2)
        assert revisions.char_range_to_tokens(text, 0, len(text)) == (0, 3)

    def test_partial_overlap_counts(self) -> None:
        text = "one two three"
        assert revisions.char_range_to_tokens(text, 2, 5) == (0, 2)

    def test_an_empty_or_out_of_bounds_range_is_a_prompt_error(self) -> None:
        import pytest

        from graph_context.errors import GraphContextError

        with pytest.raises(GraphContextError, match="empty"):
            revisions.char_range_to_tokens("one two", 3, 3)
        with pytest.raises(GraphContextError, match="outside"):
            revisions.char_range_to_tokens("one two", 40, 50)
