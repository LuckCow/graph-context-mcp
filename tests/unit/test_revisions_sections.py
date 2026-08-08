"""Section status/intent domain rules (WP42, ADR 049): the mark line
vocabulary, the section_states fold (inheritance across human edits,
void-on-AI-edit), the order-insensitive locked check, the hash-anchored
body splice, word diffs, and mark-aware compaction. Pure and fast."""

from __future__ import annotations

import pytest

from graph_context.domain import revisions
from graph_context.domain.revisions import (
    LogEntry,
    RevisionRecord,
    SectionMark,
    body_blocks,
    compact,
    edit_body,
    missing_locked,
    parse_log,
    render_log,
    section_states,
    word_diff,
)
from graph_context.errors import GraphContextError, SectionAnchorNotFound
from tests.unit.revlog import PARA_A, PARA_A_EDIT, PARA_B, PARA_C
from tests.unit.revlog import grow as _grow
from tests.unit.revlog import h as _h
from tests.unit.revlog import records as _records


def _mark(
    entries: list[LogEntry], kind: str, paragraph: str, value: str, *,
    at: str = "TM", by: str = "user",
) -> None:
    entries.append(SectionMark(
        kind=kind, hash=_h(paragraph), value=value, at=at, by=by,
    ))


class TestMarkSerialization:
    def test_marks_round_trip_interleaved(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_INTENT, PARA_A, "locked", by="h:page")
        parsed = parse_log(render_log(entries))
        assert parsed.skipped == 0
        assert parsed.entries == tuple(entries)
        assert parsed.records == tuple(_records(entries))

    def test_pre_wp42_logs_parse_with_entries_equal_to_records(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        _grow(entries, [PARA_A, PARA_B])
        parsed = parse_log(render_log(entries))
        assert parsed.entries == parsed.records == tuple(entries)

    def test_garbled_marks_are_skipped_not_fatal(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        rendered = render_log(entries)
        bad_value = '{"kind":"status","hash":"abcd1234","value":"golden","at":"T","by":"u"}'
        missing_key = '{"kind":"intent","hash":"abcd1234","at":"T","by":"u"}'
        mangled = (
            rendered.removesuffix("```")
            + f"{bad_value}\n{missing_key}\n```"
        )
        parsed = parse_log(mangled)
        assert parsed.skipped == 2
        assert parsed.entries == parsed.records


class TestSectionStatesFold:
    def test_badges_of_matches_section_states(self) -> None:
        # badges_of is the extracted badge rule (fold-free); over a
        # mixed log the two derivations must agree exactly.
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_INTENT, PARA_B, "locked")
        _grow(entries, [PARA_A_EDIT, PARA_B, PARA_C],
              author=revisions.AUTHOR_HUMAN, detail="human")
        assert revisions.badges_of(
            revisions.token_states(entries)
        ) == section_states(entries)

    def test_model_blocks_default_to_raw_ai_flexible(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        states = section_states(entries)
        assert states[_h(PARA_A)].status == "raw_ai"
        assert states[_h(PARA_A)].intent == "flexible"

    def test_a_mark_sets_its_field_and_stamps_who_when(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved",
              at="T9", by="h:page")
        state = section_states(entries)[_h(PARA_A)]
        assert state.status == "approved"
        assert (state.status_at, state.status_by) == ("T9", "h:page")
        assert state.intent == "flexible"  # untouched field keeps default

    def test_human_edit_inherits_state_token_wise(self) -> None:
        # WP46 semantics: state follows TOKENS. The one word the human
        # changed is no longer approved (the block badge drops to
        # raw_ai for the mixed block), but every untouched word keeps
        # approved+locked -- and one locked token keeps the block's
        # intent badge locked.
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_INTENT, PARA_A, "locked")
        _grow(entries, [PARA_A_EDIT, PARA_B],
              author=revisions.AUTHOR_HUMAN, detail="human")
        states = section_states(entries)
        assert _h(PARA_A) not in states  # keyed to the final sequence
        assert states[_h(PARA_A_EDIT)].status == "raw_ai"  # mixed badge
        assert states[_h(PARA_A_EDIT)].intent == "locked"
        tokens = revisions.token_states(entries)[_h(PARA_A_EDIT)]
        assert tokens.status.count("approved") == len(tokens.status) - 1
        assert "human" in tokens.status  # the edited word alone

    def test_a_brand_new_human_block_starts_human(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        _grow(entries, [PARA_A, PARA_C],
              author=revisions.AUTHOR_HUMAN, detail="human")
        assert section_states(entries)[_h(PARA_C)].status == "human"

    def test_an_ai_edit_voids_approved_but_intent_follows(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_INTENT, PARA_A, "needs_change")
        _grow(entries, [PARA_A_EDIT, PARA_B])  # model author
        state = section_states(entries)[_h(PARA_A_EDIT)]
        assert state.status == "raw_ai"
        assert state.intent == "needs_change"

    def test_minor_revisions_intent_folds_like_any_other(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_INTENT, PARA_A, "minor_revisions")
        assert section_states(entries)[_h(PARA_A)].intent == "minor_revisions"

    def test_a_louder_intent_wins_the_block_badge(self) -> None:
        # Mixed token intents badge as the loudest instruction:
        # locked > needs_change > minor_revisions > flexible.
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A),
            value="minor_revisions", at="TM", by="user", start=0, end=3,
        ))
        assert section_states(entries)[_h(PARA_A)].intent == "minor_revisions"
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A),
            value="needs_change", at="TM", by="user", start=4, end=6,
        ))
        assert section_states(entries)[_h(PARA_A)].intent == "needs_change"
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A),
            value="locked", at="TM", by="user", start=7, end=9,
        ))
        assert section_states(entries)[_h(PARA_A)].intent == "locked"

    def test_a_mark_on_a_dead_or_unknown_hash_folds_to_nothing(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _grow(entries, [PARA_B])  # PARA_A removed
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_STATUS, PARA_C, "approved")
        states = section_states(entries)
        assert set(states) == {_h(PARA_B)}
        assert states[_h(PARA_B)].status == "raw_ai"

    def test_removed_blocks_leave_the_state_map(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_INTENT, PARA_B, "locked")
        _grow(entries, [PARA_A])
        assert _h(PARA_B) not in section_states(entries)


class TestMissingLocked:
    def _locked_a(
        self,
    ) -> tuple[dict[str, revisions.TokenState], dict[str, str]]:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_INTENT, PARA_A, "locked")
        records = [e for e in entries if isinstance(e, RevisionRecord)]
        return revisions.token_states(entries), revisions.texts_of(records)

    def test_a_moved_locked_block_passes(self) -> None:
        states, texts = self._locked_a()
        moved = "\n\n".join([PARA_B, PARA_C, PARA_A])
        assert missing_locked(states, texts, moved) == ()

    def test_a_dropped_locked_block_is_reported(self) -> None:
        states, texts = self._locked_a()
        missing = missing_locked(states, texts, PARA_B)
        assert missing == ((_h(PARA_A), revisions.normalize_block(PARA_A)),)

    def test_an_edited_locked_block_is_reported(self) -> None:
        # Editing the locked text breaks verbatim presence -- no diffing.
        states, texts = self._locked_a()
        body = "\n\n".join([PARA_A_EDIT, PARA_B])
        assert missing_locked(states, texts, body) != ()

    def test_locked_text_merged_into_a_bigger_paragraph_passes(self) -> None:
        # WP46 refinement: LOCKED means the TEXT survives verbatim --
        # embedding it in a larger paragraph is legal.
        states, texts = self._locked_a()
        merged = f"{PARA_A} {PARA_C}\n\n{PARA_B}"
        assert missing_locked(states, texts, merged) == ()

    def test_unlocked_blocks_never_report(self) -> None:
        states, texts = self._locked_a()
        del states[_h(PARA_A)]
        assert missing_locked(states, texts, "") == ()

    def test_a_partial_lock_demands_only_its_run(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        tokens = revisions.block_tokens(revisions.normalize_block(PARA_A))
        # Lock "the siege began" (tokens 6..9 of PARA_A).
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A), value="locked",
            at="TM", by="user", start=6, end=9,
        ))
        states = revisions.token_states(entries)
        records = [e for e in entries if isinstance(e, RevisionRecord)]
        texts = revisions.texts_of(records)
        run = "".join(tokens[6:9]).strip()
        assert revisions.locked_runs(states, texts) == {_h(PARA_A): (run,)}
        # A rewrite that keeps the run verbatim passes...
        assert missing_locked(
            states, texts, f"Now rewritten, but {run} still stands."
        ) == ()
        # ...and one that loses it is reported with the run text.
        assert missing_locked(states, texts, PARA_B) == ((_h(PARA_A), run),)


class TestEditBody:
    BODY = f"{PARA_A}\n\n{PARA_B}\n\n{PARA_C}"

    def test_replace_swaps_one_block_and_keeps_the_rest_verbatim(self) -> None:
        odd = "Kept   spacing  block, with internal   runs preserved."
        body = f"{odd}\n\n{PARA_B}"
        new = edit_body(body, action="replace", anchor=_h(PARA_B),
                        text="A fresh paragraph rides in.")
        assert odd in new
        assert PARA_B not in new
        assert "A fresh paragraph rides in." in new

    def test_insert_after_places_text_behind_the_anchor(self) -> None:
        new = edit_body(self.BODY, action="insert_after",
                        anchor=_h(PARA_A), text=PARA_A_EDIT)
        hashes = [h for h, _ in body_blocks(new)]
        assert hashes == [_h(PARA_A), _h(PARA_A_EDIT), _h(PARA_B), _h(PARA_C)]

    def test_insert_after_top_prepends(self) -> None:
        new = edit_body(self.BODY, action="insert_after", anchor="top",
                        text=PARA_A_EDIT)
        assert [h for h, _ in body_blocks(new)][0] == _h(PARA_A_EDIT)

    def test_delete_removes_exactly_one_block(self) -> None:
        new = edit_body(self.BODY, action="delete", anchor=_h(PARA_B))
        assert [h for h, _ in body_blocks(new)] == [_h(PARA_A), _h(PARA_C)]

    def test_a_unique_prefix_resolves_like_git(self) -> None:
        new = edit_body(self.BODY, action="delete",
                        anchor="§" + _h(PARA_B)[:6])
        assert _h(PARA_B) not in [h for h, _ in body_blocks(new)]

    def test_an_unknown_anchor_lists_the_current_sections(self) -> None:
        with pytest.raises(SectionAnchorNotFound) as exc:
            edit_body(self.BODY, action="replace", anchor="feedbeefcafe",
                      text="x")
        message = str(exc.value)
        assert _h(PARA_A) in message
        assert PARA_B.split()[0] in message  # first-line excerpts shown

    def test_a_short_anchor_is_rejected_not_guessed(self) -> None:
        with pytest.raises(SectionAnchorNotFound):
            edit_body(self.BODY, action="delete", anchor=_h(PARA_A)[:2])

    def test_an_unknown_action_is_a_prompt_error(self) -> None:
        with pytest.raises(GraphContextError, match="insert_after"):
            edit_body(self.BODY, action="append", anchor="top", text="x")


class TestWordDiff:
    def test_spans_reconstruct_both_sides(self) -> None:
        spans = word_diff(PARA_A, PARA_A_EDIT)
        old = "".join(t for k, t in spans if k in ("eq", "del"))
        new = "".join(t for k, t in spans if k in ("eq", "add"))
        assert old == PARA_A
        assert new == PARA_A_EDIT
        assert any(k == "del" for k, _ in spans)
        assert any(k == "add" for k, _ in spans)

    def test_identical_texts_are_one_eq_span(self) -> None:
        assert word_diff(PARA_A, PARA_A) == (("eq", PARA_A),)


class TestMarkCompaction:
    def _churned(self, rounds: int) -> list[LogEntry]:
        entries: list[LogEntry] = []
        for i in range(rounds):
            filler = (
                f"Filler paragraph {i}: " + "words fill the siege log " * 40
            )
            _grow(entries, [PARA_A, filler], at=f"T{i}")
        return entries

    def test_live_marks_survive_compaction_fold_equivalent(self) -> None:
        entries = self._churned(90)
        # Marks set early (dropped era) on PARA_A, which stays current.
        entries.insert(2, SectionMark(
            kind=revisions.MARK_STATUS, hash=_h(PARA_A),
            value="approved", at="T1", by="user",
        ))
        entries.insert(3, SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A),
            value="locked", at="T1", by="user",
        ))
        before = section_states(entries)
        cap = len(render_log(entries)) // 2
        compacted = compact(entries, cap)
        assert len(render_log(list(compacted))) <= cap
        after = section_states(compacted)
        a = _h(PARA_A)
        assert (after[a].status, after[a].intent) == ("approved", "locked")
        assert (before[a].status, before[a].intent) == ("approved", "locked")

    def test_marks_on_dead_blocks_drop_with_their_era(self) -> None:
        entries = self._churned(90)
        dead_filler = "Filler paragraph 0: " + "words fill the siege log " * 40
        entries.insert(1, SectionMark(
            kind=revisions.MARK_STATUS, hash=_h(dead_filler),
            value="approved", at="T0", by="user",
        ))
        compacted = compact(entries, len(render_log(entries)) // 2)
        assert not any(
            isinstance(e, SectionMark) and e.hash == _h(dead_filler)
            for e in compacted
        )


class TestSpanMarks:
    """WP46: marks narrowed to a token range; state follows tokens."""

    def _tokens(self, text: str) -> list[str]:
        return revisions.block_tokens(revisions.normalize_block(text))

    def test_a_span_mark_round_trips(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_STATUS, hash=_h(PARA_A), value="approved",
            at="TM", by="user", start=2, end=5,
        ))
        parsed = parse_log(render_log(entries))
        assert parsed.skipped == 0
        assert parsed.entries == tuple(entries)

    def test_a_mangled_range_is_skipped_not_fatal(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        rendered = render_log(entries)
        bad = (
            '{"kind":"status","hash":"' + _h(PARA_A) + '",'
            '"value":"approved","at":"T","by":"u","s":5,"e":2}'
        )
        parsed = parse_log(rendered.removesuffix("```") + bad + "\n```")
        assert parsed.skipped == 1

    def test_a_span_approval_sets_only_its_tokens(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_STATUS, hash=_h(PARA_A), value="approved",
            at="TM", by="user", start=0, end=3,
        ))
        state = revisions.token_states(entries)[_h(PARA_A)]
        n = len(self._tokens(PARA_A))
        assert state.status == ("approved",) * 3 + ("raw_ai",) * (n - 3)

    def test_span_state_follows_tokens_across_an_edit(self) -> None:
        # Approve the first three words, then a model edit changes ONE
        # later word: the approved words survive; the changed word is
        # raw_ai. The void is token-local (WP46 refinement of WP42).
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_STATUS, hash=_h(PARA_A), value="approved",
            at="TM", by="user", start=0, end=3,
        ))
        _grow(entries, [PARA_A_EDIT])  # model author; "began" -> "started"
        state = revisions.token_states(entries)[_h(PARA_A_EDIT)]
        assert state.status[:3] == ("approved",) * 3
        assert "raw_ai" in state.status[3:]

    def test_an_out_of_range_span_clamps_to_the_current_text(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A])
        n = len(self._tokens(PARA_A))
        entries.append(revisions.SectionMark(
            kind=revisions.MARK_INTENT, hash=_h(PARA_A), value="locked",
            at="TM", by="user", start=n - 2, end=n + 50,
        ))
        state = revisions.token_states(entries)[_h(PARA_A)]
        assert state.intent[-2:] == ("locked", "locked")
        assert set(state.intent[:-2]) == {"flexible"}


class TestLockedRawIndexing:
    """ADR 054: locked runs are raw substrings; the presence check folds
    whitespace on both sides (store reflow is not a violation)."""

    def _locked_heading(self) -> tuple[dict, dict]:
        heading = "## The Gate\nIt stayed barred through the siege."
        entries: list[LogEntry] = []
        _grow(entries, [heading])
        _mark(entries, revisions.MARK_INTENT, heading, "locked")
        records = _records(entries)
        return revisions.token_states(entries), revisions.texts_of(records)

    def test_a_locked_run_keeps_its_heading_marker(self) -> None:
        states, texts = self._locked_heading()
        runs = revisions.locked_runs(states, texts)
        assert runs[_h("## The Gate\nIt stayed barred through the siege.")] \
            == ("## The Gate\nIt stayed barred through the siege.",)

    def test_reflowed_whitespace_is_not_a_violation(self) -> None:
        states, texts = self._locked_heading()
        reflowed = "## The Gate\nIt stayed barred\nthrough   the siege."
        assert missing_locked(states, texts, reflowed) == ()

    def test_changed_locked_words_still_report(self) -> None:
        states, texts = self._locked_heading()
        edited = "## The Gate\nIt stayed open through the siege."
        assert len(missing_locked(states, texts, edited)) == 1
