"""Comment domain rules (WP50, ADR 056): the two comment line kinds,
the comment fold (anchors riding edits, whole-block fallback, detach and
verbatim re-attach, the addressed/resolved lifecycle), rewrite-on-
compaction hoisting, and the roll-up interaction. Pure and fast."""

from __future__ import annotations

import pytest

from graph_context.domain import revisions
from graph_context.domain.revisions import (
    CommentEntry,
    CommentStateEntry,
    LogEntry,
    comment_id,
    comment_states,
    compact,
    parse_log,
    render_log,
    rollup_base,
    token_range_to_chars,
)
from graph_context.errors import GraphContextError
from tests.unit.revlog import PARA_A, PARA_A_EDIT, PARA_B, PARA_C
from tests.unit.revlog import grow as _grow
from tests.unit.revlog import h as _h
from tests.unit.revlog import records as _records


def _comment(
    entries: list[LogEntry], paragraph: str, text: str, *,
    at: str = "TC", by: str = "human:prose-page",
    start: int = -1, end: int = -1,
) -> str:
    cid = comment_id(at, by, _h(paragraph), text)
    entries.append(CommentEntry(
        id=cid, hash=_h(paragraph), text=text, at=at, by=by,
        start=start, end=end,
    ))
    return cid


class TestCommentSerialization:
    def test_comments_round_trip_interleaved(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        cid = _comment(entries, PARA_A, "why barred?", start=2, end=4)
        entries.append(CommentStateEntry(
            id=cid, value="addressed", at="T2", by="model",
        ))
        parsed = parse_log(render_log(entries))
        assert parsed.skipped == 0
        assert parsed.entries == tuple(entries)
        assert parsed.records == tuple(_records(entries))

    def test_whole_block_comment_omits_range_keys(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        _comment(entries, PARA_A, "tighten this")
        rendered = render_log(entries)
        comment_line = next(
            line for line in rendered.splitlines() if '"comment"' in line
        )
        assert '"s"' not in comment_line
        assert '"e"' not in comment_line

    def test_detached_comment_with_empty_hash_parses(self) -> None:
        entry = CommentEntry(
            id="cdeadbeef", hash="", text="orphaned", at="T", by="u",
        )
        parsed = parse_log(render_log([entry]))
        assert parsed.skipped == 0
        assert parsed.entries == (entry,)

    def test_garbled_comment_lines_are_skipped_not_fatal(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        rendered = render_log(entries)
        missing_text = '{"kind":"comment","id":"c1","hash":"ab","at":"T","by":"u"}'
        bad_state = '{"kind":"comment_state","id":"c1","value":"golden","at":"T","by":"u"}'
        bad_range = (
            '{"kind":"comment","id":"c2","hash":"ab","text":"x",'
            '"at":"T","by":"u","s":4,"e":2}'
        )
        mangled = (
            rendered.removesuffix("```")
            + f"{missing_text}\n{bad_state}\n{bad_range}\n```"
        )
        parsed = parse_log(mangled)
        assert parsed.skipped == 3
        assert parsed.entries == parsed.records


class TestCommentFold:
    def test_a_comment_anchors_to_its_live_block(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        cid = _comment(entries, PARA_B, "count them again")
        (state,) = comment_states(entries)
        assert state.id == cid
        assert state.state == "open"
        assert state.hash == _h(PARA_B)
        assert (state.start, state.end) == (-1, -1)

    def test_a_ranged_comment_rides_an_edit_to_the_successor(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        # "every gate barred." = tokens 8..11 of PARA_A, untouched by the edit
        _comment(entries, PARA_A, "gates?", start=8, end=11)
        _grow(entries, [PARA_A_EDIT, PARA_B], author=revisions.AUTHOR_HUMAN)
        (state,) = comment_states(entries)
        assert state.hash == _h(PARA_A_EDIT)
        assert (state.start, state.end) == (8, 11)

    def test_deleting_the_commented_words_falls_back_to_whole_block(
        self,
    ) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        # "began" alone -- the edit rewrites it to "started"
        _comment(entries, PARA_A, "weak verb", start=7, end=8)
        _grow(entries, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        (state,) = comment_states(entries)
        assert state.hash == _h(PARA_A_EDIT)
        assert (state.start, state.end) == (-1, -1)

    def test_a_vanished_block_detaches_the_comment_but_keeps_it(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _comment(entries, PARA_B, "count them again")
        _grow(entries, [PARA_A])
        (state,) = comment_states(entries)
        assert state.hash == ""
        assert state.state == "open"

    def test_a_restored_block_reattaches_its_detached_comment(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _comment(entries, PARA_B, "count them again", start=0, end=3)
        _grow(entries, [PARA_A])
        _grow(entries, [PARA_A, PARA_B])
        (state,) = comment_states(entries)
        assert state.hash == _h(PARA_B)
        assert (state.start, state.end) == (0, 3)

    def test_addressed_then_resolved_folds_away(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        cid = _comment(entries, PARA_A, "tighten this")
        entries.append(CommentStateEntry(
            id=cid, value="addressed", at="T2", by="model",
        ))
        (state,) = comment_states(entries)
        assert state.state == "addressed"
        assert state.state_by == "model"
        entries.append(CommentStateEntry(
            id=cid, value="resolved", at="T3", by="human:prose-page",
        ))
        assert comment_states(entries) == ()

    def test_resolved_is_terminal_for_later_transitions(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        cid = _comment(entries, PARA_A, "tighten this")
        entries.append(CommentStateEntry(
            id=cid, value="resolved", at="T2", by="u",
        ))
        entries.append(CommentStateEntry(
            id=cid, value="addressed", at="T3", by="model",
        ))
        assert comment_states(entries) == ()

    def test_a_transition_on_an_unknown_id_folds_to_nothing(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        entries.append(CommentStateEntry(
            id="cnosuch01", value="addressed", at="T2", by="model",
        ))
        assert comment_states(entries) == ()

    def test_comments_leave_review_and_authorship_folds_untouched(
        self,
    ) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _grow(entries, [PARA_A_EDIT, PARA_B], author=revisions.AUTHOR_HUMAN)
        before_tokens = revisions.token_states(entries)
        before_sections = revisions.section_states(entries)
        cid = _comment(entries, PARA_B, "count them again", start=0, end=2)
        entries.append(CommentStateEntry(
            id=cid, value="addressed", at="T2", by="model",
        ))
        assert revisions.token_states(entries) == before_tokens
        assert revisions.section_states(entries) == before_sections

    def test_comments_list_in_file_order(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        first = _comment(entries, PARA_B, "one", at="T1")
        second = _comment(entries, PARA_A, "two", at="T2")
        assert [s.id for s in comment_states(entries)] == [first, second]


class TestCommentCompaction:
    def _grow_until_compactable(
        self, entries: list[LogEntry], cap: int
    ) -> None:
        """Append revisions until compaction at ``cap`` drops an era."""
        filler = 0
        while True:
            filler += 1
            _grow(entries, [PARA_A, PARA_B, f"Filler paragraph {filler}."])
            records = _records(entries)
            keyframes = [
                r for r in records if r.kind == revisions.KIND_KEYFRAME
            ]
            if len(keyframes) >= 2 and len(render_log(entries)) > cap:
                return

    def test_a_dropped_era_live_comment_is_hoisted_with_its_anchor(
        self,
    ) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        cid = _comment(entries, PARA_B, "count them again", start=0, end=3)
        cap = 4000
        self._grow_until_compactable(entries, cap)
        compacted = compact(entries, cap)
        assert len(compacted) < len(entries)
        hoisted = [
            e for e in compacted
            if isinstance(e, CommentEntry) and e.id == cid
        ]
        assert len(hoisted) == 1
        (state,) = comment_states(compacted)
        assert state.id == cid
        assert state.hash == _h(PARA_B)
        assert (state.start, state.end) == (0, 3)

    def test_an_addressed_comment_survives_compaction_addressed(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        cid = _comment(entries, PARA_B, "count them again")
        entries.append(CommentStateEntry(
            id=cid, value="addressed", at="T2", by="model",
        ))
        cap = 4000
        self._grow_until_compactable(entries, cap)
        compacted = compact(entries, cap)
        (state,) = comment_states(compacted)
        assert state.state == "addressed"
        assert state.state_by == "model"

    def test_a_resolved_comment_drops_with_its_era(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        cid = _comment(entries, PARA_B, "count them again")
        entries.append(CommentStateEntry(
            id=cid, value="resolved", at="T2", by="u",
        ))
        cap = 4000
        self._grow_until_compactable(entries, cap)
        compacted = compact(entries, cap)
        assert not any(isinstance(e, CommentEntry) for e in compacted)
        assert comment_states(compacted) == ()

    def test_a_detached_comment_is_hoisted_and_can_still_reattach(
        self,
    ) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_C])
        cid = _comment(entries, PARA_C, "watch fires?")
        _grow(entries, [PARA_A])  # PARA_C vanishes: detached
        cap = 4000
        self._grow_until_compactable(entries, cap)
        compacted = compact(entries, cap)
        (state,) = comment_states(compacted)
        assert state.id == cid
        assert state.hash == ""
        _grow(list_entries := list(compacted), [PARA_A, PARA_B, PARA_C])
        (state,) = comment_states(list_entries)
        assert state.hash == _h(PARA_C)


class TestCommentRollup:
    def test_a_comment_line_solidifies_the_pending_human_revision(
        self,
    ) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        _grow(entries, [PARA_A_EDIT], author=revisions.AUTHOR_HUMAN)
        assert rollup_base(entries) is not None
        _comment(entries, PARA_A_EDIT, "keep this phrasing")
        assert rollup_base(entries) is None


class TestTokenRangeToChars:
    def test_round_trips_a_char_selection(self) -> None:
        text = "The cat sat on the mat."
        s, e = revisions.char_range_to_tokens(text, 4, 11)  # "cat sat"
        assert text[slice(*token_range_to_chars(text, s, e))] == "cat sat"

    def test_rejects_a_range_the_text_does_not_have(self) -> None:
        with pytest.raises(GraphContextError):
            token_range_to_chars("one two", 1, 9)


class TestCommentId:
    def test_is_stable_and_content_keyed(self) -> None:
        a = comment_id("T", "u", "hash", "text")
        assert a == comment_id("T", "u", "hash", "text")
        assert a != comment_id("T", "u", "hash", "other")
        assert a.startswith("c") and len(a) == 9
