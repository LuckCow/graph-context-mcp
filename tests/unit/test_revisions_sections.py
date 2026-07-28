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
    block_hash,
    body_blocks,
    compact,
    current_hashes,
    edit_body,
    hash_sequence,
    missing_locked,
    next_record,
    normalize_block,
    parse_log,
    render_log,
    section_states,
    word_diff,
)
from graph_context.errors import GraphContextError, SectionAnchorNotFound

PARA_A = "The city fell quiet before the siege began, every gate barred."
PARA_B = "Mira counted the engines twice; one was missing from the yard."
PARA_C = "Rain came at dusk and the watch fires guttered along the wall."
# A human's light touch on PARA_A -- similar enough for lineage.
PARA_A_EDIT = "The city fell quiet before the siege started, every gate barred."


def _h(paragraph: str) -> str:
    return block_hash(normalize_block(paragraph))


def _pairs(*paragraphs: str) -> tuple[tuple[str, str], ...]:
    return hash_sequence("\n\n".join(paragraphs))


def _records(entries: list[LogEntry]) -> list[RevisionRecord]:
    return [e for e in entries if isinstance(e, RevisionRecord)]


def _grow(
    entries: list[LogEntry], body_paragraphs: list[str], *,
    author: str = revisions.AUTHOR_MODEL, detail: str = "m", at: str = "T",
) -> None:
    records = _records(entries)
    prev = current_hashes(records)
    known = frozenset(revisions.texts_of(records))
    entries.append(next_record(
        prev, records[-1].seq if records else 0, _pairs(*body_paragraphs),
        at=at, author_kind=author, author_detail=detail, known_hashes=known,
    ))


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

    def test_human_edit_inherits_status_and_intent_via_similarity(self) -> None:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_STATUS, PARA_A, "approved")
        _mark(entries, revisions.MARK_INTENT, PARA_A, "locked")
        _grow(entries, [PARA_A_EDIT, PARA_B],
              author=revisions.AUTHOR_HUMAN, detail="human")
        states = section_states(entries)
        assert _h(PARA_A) not in states  # keyed to the final sequence
        assert states[_h(PARA_A_EDIT)].status == "approved"
        assert states[_h(PARA_A_EDIT)].intent == "locked"

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
    def _locked_a(self) -> dict[str, revisions.SectionState]:
        entries: list[LogEntry] = []
        _grow(entries, [PARA_A, PARA_B])
        _mark(entries, revisions.MARK_INTENT, PARA_A, "locked")
        return section_states(entries)

    def test_a_moved_locked_block_passes(self) -> None:
        states = self._locked_a()
        moved = "\n\n".join([PARA_B, PARA_C, PARA_A])
        assert missing_locked(states, moved) == ()

    def test_a_dropped_locked_block_is_reported(self) -> None:
        states = self._locked_a()
        assert missing_locked(states, PARA_B) == (_h(PARA_A),)

    def test_an_edited_locked_block_is_reported(self) -> None:
        # Editing changes the hash -- presence check fails, no diffing.
        states = self._locked_a()
        body = "\n\n".join([PARA_A_EDIT, PARA_B])
        assert missing_locked(states, body) == (_h(PARA_A),)

    def test_unlocked_blocks_never_report(self) -> None:
        states = self._locked_a()
        del states[_h(PARA_A)]
        assert missing_locked(states, "") == ()


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
