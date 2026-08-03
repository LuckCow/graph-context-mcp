"""NodeHistorian service (WP41, ADR 049): the recording contract.

The invariants under test: tracking is keyed off the Space Context's
tracked-types list (infra types never track); the first body-bearing
write creates the sidecar; identical bodies never record; rebuild
restores baselines without re-recording, and its catch-up compare picks
up offline edits as human revisions; the change-tick sweep records human
edits and ignores everything else.
"""

from __future__ import annotations

import pytest

from graph_context.application.node_historian import (
    HISTORY_TYPE,
    NodeHistorian,
)
from graph_context.domain import revisions
from graph_context.domain.models import Node, NodeDraft
from graph_context.domain.schema import Role
from graph_context.errors import (
    GraphContextError,
    LockedSectionsChanged,
    StaleSectionMark,
)
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)

OPENING = "The city fell quiet before the siege began, every gate barred."
MIDDLE = "Mira counted the engines twice; one was missing from the yard."
REVISED = "Rain came at dusk and the watch fires guttered along the wall."


class World:
    """One space with a tracked-types list and a helper per test need."""

    def __init__(self) -> None:
        self.repo = InMemoryGraphRepository()
        self.ticks = 0
        self.historian = NodeHistorian(self.repo, now=self._now)

    def _now(self) -> str:
        self.ticks += 1
        return f"T{self.ticks}"

    async def seed_space_context(self, tracked: str = "Chapter") -> Node:
        return await self.repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: tracked},
        ))

    async def chapter(self, *paragraphs: str, name: str = "Chapter One") -> Node:
        return await self.repo.create_node(NodeDraft(
            type="Chapter", name=name, summary="a chapter",
            body="\n\n".join(paragraphs),
        ))

    def sidecars(self) -> list[Node]:
        return [
            n for n in self.repo.graph.nodes()
            if n.role is Role.NODE_HISTORY
        ]


class TestTracking:
    async def test_tracked_types_read_off_the_space_context(self) -> None:
        world = World()
        assert world.historian.tracked_types() == ()
        await world.seed_space_context("Chapter, Character")
        assert world.historian.tracked_types() == ("Chapter", "Character")

    async def test_only_listed_types_track(self) -> None:
        world = World()
        await world.seed_space_context("Chapter")
        chapter = await world.chapter(OPENING)
        other = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        assert world.historian.is_tracked(chapter.id)
        assert not world.historian.is_tracked(other.id)

    async def test_infra_types_never_track_even_if_listed(self) -> None:
        world = World()
        await world.seed_space_context("Activity Mode")
        mode = await world.repo.create_node(NodeDraft(
            type="gc_activity_mode", name="Prose", summary="m", body="goal",
        ))
        assert not world.historian.is_tracked(mode.id)

    async def test_a_list_edit_applies_with_no_restart(self) -> None:
        world = World()
        context = await world.seed_space_context("Chapter")
        character = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        assert not world.historian.is_tracked(character.id)
        await world.repo.update_node(
            context.id,
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter, Character"},
        )
        assert world.historian.is_tracked(character.id)


class TestRecording:
    async def test_first_bot_revision_creates_the_sidecar(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, MIDDLE)
        recorded = await world.historian.record_bot_revision(
            chapter.id, author_detail="fable · prose · u1",
        )
        assert recorded
        (sidecar,) = world.sidecars()
        assert sidecar.type_key == HISTORY_TYPE or sidecar.type == HISTORY_TYPE
        assert sidecar.fields[revisions.FIELD_HISTORY_OF] == chapter.id
        parsed = revisions.parse_log(await world.repo.fetch_body(sidecar.id))
        (record,) = parsed.records
        assert record.kind == revisions.KIND_KEYFRAME
        assert record.author_detail == "fable · prose · u1"

    async def test_identical_body_records_nothing(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        assert await world.historian.record_bot_revision(
            chapter.id, author_detail="m",
        )
        assert not await world.historian.record_bot_revision(
            chapter.id, author_detail="m",
        )
        assert len(world.historian.history(chapter.id)) == 1

    async def test_the_sweep_records_a_human_revision(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{REVISED}",
        )
        await world.historian.sweep({chapter.id})
        records = world.historian.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_a_human_created_chapter_tracks_on_first_sweep(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.sweep({chapter.id})
        (record,) = world.historian.history(chapter.id)
        assert record.author_kind == revisions.AUTHOR_HUMAN

    async def test_an_empty_body_does_not_start_tracking(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(name="Stub")  # no paragraphs
        await world.historian.sweep({chapter.id})
        assert world.sidecars() == []

    async def test_untracked_changes_are_ignored(self) -> None:
        world = World()
        await world.seed_space_context()
        other = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        await world.historian.sweep({other.id})
        assert world.sidecars() == []

    async def test_blame_reads_from_the_live_baseline(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}",
        )
        await world.historian.sweep({chapter.id})
        by_hash = world.historian.blame(chapter.id)
        opening = revisions.block_hash(revisions.normalize_block(OPENING))
        middle = revisions.block_hash(revisions.normalize_block(MIDDLE))
        assert by_hash[opening].author_kind == revisions.AUTHOR_MODEL
        assert by_hash[middle].author_kind == revisions.AUTHOR_HUMAN


class TestRebuild:
    async def test_rebuild_restores_without_re_recording(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        assert fresh.tracked_ids() == frozenset({chapter.id})
        assert len(fresh.history(chapter.id)) == 1  # clean replay: no echo

    async def test_rebuild_catches_up_an_offline_edit_as_human(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(chapter.id, body=REVISED)  # bot was down
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        records = fresh.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_a_mangled_sidecar_degrades_never_bricks(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        (sidecar,) = world.sidecars()
        await world.repo.update_node(sidecar.id, body="a human wiped this")
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        # The log restarts from the current body -- seq 1, keyframe.
        (record,) = fresh.history(chapter.id)
        assert record.kind == revisions.KIND_KEYFRAME
        assert record.author_kind == revisions.AUTHOR_HUMAN


def _h(text: str) -> str:
    return revisions.block_hash(revisions.normalize_block(text))


async def _tracked_chapter() -> tuple[World, Node]:
    world = World()
    await world.seed_space_context()
    chapter = await world.chapter(OPENING, MIDDLE)
    await world.historian.record_bot_revision(chapter.id, author_detail="m")
    return world, chapter


class TestSectionMarks:
    async def test_a_mark_lands_in_the_sidecar_and_survives_rebuild(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(OPENING),
            value="approved", by="user",
        )
        assert state.status == "approved"
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        assert fresh.section_states(chapter.id)[_h(OPENING)].status == "approved"

    async def test_re_marking_the_same_value_writes_nothing(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        again = await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        assert again.intent == "locked"
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_a_stale_hash_is_rejected_with_reload(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(StaleSectionMark, match="reload"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(REVISED),
                value="approved", by="user",
            )

    async def test_bad_kinds_and_values_are_prompt_errors(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(GraphContextError, match="allowed"):
            await world.historian.record_mark(
                chapter.id, kind="mood", block_hash=_h(OPENING),
                value="approved", by="user",
            )
        with pytest.raises(GraphContextError, match="allowed"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="golden", by="user",
            )

    async def test_word_free_separator_blocks_cannot_carry_marks(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, "* * *", MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        with pytest.raises(GraphContextError, match="no words"):
            await world.historian.record_mark(
                chapter.id, kind="intent", block_hash=_h("* * *"),
                value="locked", by="user",
            )

    async def test_short_prose_is_markable(self) -> None:
        """ADR 054: the old MIN_BLAME_CHARS floor made short dialogue
        unmarkable; only word-free blocks stay out now."""
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, "No.", MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        state = await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h("No."),
            value="locked", by="user",
        )
        assert state.intent == "locked"

    async def test_a_never_recorded_node_is_a_prompt_error(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        with pytest.raises(GraphContextError, match="no revision history"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="approved", by="user",
            )


class TestSectionGuard:
    async def test_dropping_a_locked_section_raises_with_excerpt(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        with pytest.raises(LockedSectionsChanged) as exc:
            world.historian.check_body_update(chapter.id, MIDDLE)
        assert "city fell quiet" in str(exc.value)
        assert "unlock" in str(exc.value)

    async def test_a_moved_locked_section_passes(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        world.historian.check_body_update(
            chapter.id, "\n\n".join([MIDDLE, REVISED, OPENING])
        )

    async def test_untracked_nodes_pass_freely(self) -> None:
        world = World()
        world.historian.check_body_update("no-such-node", "anything")


class TestRollup:
    """WP44: consecutive human revisions coalesce into one pending
    revision; a model revision or any mark solidifies it."""

    async def _chapter_world(self) -> tuple[World, Node]:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        return world, chapter

    async def test_consecutive_human_revisions_coalesce(self) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}\n\n{REVISED}"
        )
        await world.historian.sweep({chapter.id})
        records = world.historian.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]
        assert records[-1].seq == 2  # replaced in place, not appended
        assert records[-1].at == "T3"  # latest tick wins
        assert _h(REVISED) in revisions.current_hashes(records)

    async def test_a_model_revision_solidifies_the_pending_human_one(
        self,
    ) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}\n\n{REVISED}"
        )
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(chapter.id, body=OPENING)
        await world.historian.sweep({chapter.id})
        kinds = [r.author_kind for r in world.historian.history(chapter.id)]
        assert kinds == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_a_mark_solidifies_the_pending_human_revision(self) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(MIDDLE),
            value="approved", by="user",
        )
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}\n\n{REVISED}"
        )
        await world.historian.sweep({chapter.id})
        records = world.historian.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
            revisions.AUTHOR_HUMAN,
        ]
        state = world.historian.section_states(chapter.id)[_h(MIDDLE)]
        assert state.status == "approved"  # the mark survived the edits

    async def test_idle_ticks_never_rewrite_the_log(self) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        await world.historian.sweep({chapter.id})  # nothing changed
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_reverting_to_base_removes_the_pending_revision(
        self,
    ) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        await world.repo.update_node(chapter.id, body=OPENING)  # full undo
        await world.historian.sweep({chapter.id})
        records = world.historian.history(chapter.id)
        assert [r.author_kind for r in records] == [revisions.AUTHOR_MODEL]
        # And the on-disk log agrees after a rebuild.
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        assert len(fresh.history(chapter.id)) == 1

    async def test_keyframe_cadence_survives_coalescing(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        # March the log to seq 19 with alternating model bodies.
        for i in range(19):
            await world.repo.update_node(
                chapter.id, body=f"{OPENING}\n\nFiller paragraph {i} words."
            )
            await world.historian.record_bot_revision(
                chapter.id, author_detail="m",
            )
        assert world.historian.history(chapter.id)[-1].seq == 19
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})  # human seq 20 -> keyframe
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{REVISED}")
        await world.historian.sweep({chapter.id})  # coalesces, still seq 20
        tail = world.historian.history(chapter.id)[-1]
        assert (tail.seq, tail.kind) == (20, revisions.KIND_KEYFRAME)

    async def test_coalescing_re_carries_tail_only_texts(self) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}\n\n{REVISED}"
        )
        await world.historian.sweep({chapter.id})
        texts = revisions.texts_of(world.historian.history(chapter.id))
        assert texts[_h(MIDDLE)]  # first seen in the dropped tail, re-carried
        assert texts[_h(REVISED)]

    async def test_rebuild_then_human_edits_keep_coalescing(self) -> None:
        world, chapter = await self._chapter_world()
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}\n\n{REVISED}"
        )
        await fresh.sweep({chapter.id})
        records = fresh.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_the_sole_first_revision_coalesces(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.sweep({chapter.id})  # human seq-1 keyframe
        await world.repo.update_node(chapter.id, body=f"{OPENING}\n\n{MIDDLE}")
        await world.historian.sweep({chapter.id})
        (record,) = world.historian.history(chapter.id)
        assert (record.seq, record.kind) == (1, revisions.KIND_KEYFRAME)
        assert record.author_kind == revisions.AUTHOR_HUMAN


class TestSpanMarks:
    """WP46: token-ranged marks and the verbatim-run locked guard."""

    async def _tracked(self) -> tuple[World, Node, list[str]]:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        tokens = revisions.block_tokens(revisions.normalize_block(OPENING))
        return world, chapter, tokens

    async def test_a_ranged_mark_sets_only_its_tokens(self) -> None:
        world, chapter, tokens = await self._tracked()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user", start=0, end=3,
        )
        state = world.historian.token_states(chapter.id)[_h(OPENING)]
        assert state.intent[:3] == ("locked",) * 3
        assert set(state.intent[3:]) == {"flexible"}
        badge = world.historian.section_states(chapter.id)[_h(OPENING)]
        assert badge.intent == "locked"  # one locked token flags the block

    async def test_one_sided_and_out_of_range_marks_are_prompts(self) -> None:
        world, chapter, tokens = await self._tracked()
        with pytest.raises(GraphContextError, match="BOTH start and end"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="approved", by="user", start=2,
            )
        with pytest.raises(GraphContextError, match=f"0..{len(tokens)}"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="approved", by="user", start=0, end=len(tokens) + 9,
            )

    async def test_re_marking_an_already_set_range_writes_nothing(self) -> None:
        world, chapter, _ = await self._tracked()
        await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(OPENING),
            value="approved", by="user", start=1, end=4,
        )
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(OPENING),
            value="approved", by="user", start=2, end=3,  # inside the range
        )
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_the_model_may_edit_around_a_locked_run(self) -> None:
        world, chapter, tokens = await self._tracked()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user", start=0, end=4,
        )
        run = "".join(tokens[:4]).strip()
        # Rewriting the REST of the paragraph keeps the run verbatim.
        world.historian.check_body_update(
            chapter.id, f"{run} and then everything changed.\n\n{MIDDLE}"
        )
        # Touching the run itself is refused, naming its text.
        with pytest.raises(LockedSectionsChanged) as exc:
            world.historian.check_body_update(chapter.id, MIDDLE)
        assert run in str(exc.value)

    async def test_marks_survive_via_rebuild(self) -> None:
        world, chapter, _ = await self._tracked()
        await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(OPENING),
            value="approved", by="user", start=0, end=2,
        )
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        state = fresh.token_states(chapter.id)[_h(OPENING)]
        assert state.status[:2] == ("approved", "approved")


class TestRawTextMigration:
    """ADR 054: a pre-upgrade sidecar stores NORMALIZED block texts; the
    first post-upgrade record (rebuild's catch-up included) re-emits the
    live raw text on the same hashes -- no wipe, no version gate."""

    HEADING = "## The Gate\nIt stayed barred through the siege."

    async def _v1_world(self) -> tuple[World, Node]:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(self.HEADING, MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        # Rewrite the sidecar as a v1 log: same records, normalized texts.
        sidecar = world.sidecars()[0]
        parsed = revisions.parse_log(await world.repo.fetch_body(sidecar.id))
        v1_entries = tuple(
            revisions.RevisionRecord(
                seq=e.seq, at=e.at, author_kind=e.author_kind,
                author_detail=e.author_detail, kind=e.kind, hashes=e.hashes,
                ops=e.ops,
                new_blocks={
                    h: revisions.normalize_block(t)
                    for h, t in e.new_blocks.items()
                },
            ) if isinstance(e, revisions.RevisionRecord) else e
            for e in parsed.entries
        )
        await world.repo.update_node(
            sidecar.id, body=revisions.render_log(v1_entries)
        )
        return world, chapter

    async def test_rebuild_refreshes_normalized_texts_to_raw(self) -> None:
        world, chapter = await self._v1_world()
        await world.historian.rebuild()
        texts = revisions.texts_of(world.historian.history(chapter.id))
        assert self.HEADING in texts.values()  # raw again, marker intact

    async def test_the_refresh_is_hash_stable_and_once(self) -> None:
        world, chapter = await self._v1_world()
        await world.historian.rebuild()
        hashes_after = revisions.current_hashes(
            world.historian.history(chapter.id)
        )
        body = await world.repo.fetch_body(chapter.id)
        assert hashes_after == tuple(
            h for h, _ in revisions.body_blocks(body)
        )
        # A second catch-up finds nothing stale: no new record.
        before = len(world.historian.history(chapter.id))
        assert not await world.historian.record_external_revision(chapter.id)
        assert len(world.historian.history(chapter.id)) == before


class TestComments:
    async def test_a_comment_lands_in_the_sidecar_and_survives_rebuild(
        self,
    ) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(MIDDLE), text="count them again",
            by="human:prose-page",
        )
        assert state.state == revisions.COMMENT_OPEN
        assert state.hash == _h(MIDDLE)
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        (reloaded,) = fresh.comments(chapter.id)
        assert reloaded.id == state.id
        assert reloaded.text == "count them again"

    async def test_a_ranged_comment_keeps_its_token_range(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="gates?",
            by="u", start=8, end=11,
        )
        assert (state.start, state.end) == (8, 11)

    async def test_comment_writes_fire_the_on_record_hook(self) -> None:
        world, chapter = await _tracked_chapter()
        seen: list[str] = []
        world.historian.on_record = seen.append
        await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="x", by="u",
        )
        assert seen == [chapter.id]

    async def test_a_stale_hash_is_rejected_with_reload(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(StaleSectionMark, match="reload"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h(REVISED), text="x", by="u",
            )

    async def test_empty_capped_and_word_free_comments_are_rejected(
        self,
    ) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, "* * *", MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        with pytest.raises(GraphContextError, match="needs text"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h(OPENING), text="   ", by="u",
            )
        with pytest.raises(GraphContextError, match="cap"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h(OPENING),
                text="x" * (revisions.COMMENT_TEXT_CAP + 1), by="u",
            )
        with pytest.raises(GraphContextError, match="no words"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h("* * *"), text="here?", by="u",
            )

    async def test_a_bad_range_is_rejected_by_token_count(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(GraphContextError, match="BOTH"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h(OPENING), text="x", by="u",
                start=1,
            )
        with pytest.raises(GraphContextError, match="tokens"):
            await world.historian.record_comment(
                chapter.id, block_hash=_h(OPENING), text="x", by="u",
                start=0, end=999,
            )

    async def test_an_identical_same_second_comment_writes_nothing(
        self,
    ) -> None:
        world, chapter = await _tracked_chapter()
        world.historian = NodeHistorian(world.repo, now=lambda: "TX")
        await world.historian.rebuild()
        first = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="dup", by="u",
        )
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        again = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="dup", by="u",
        )
        assert again.id == first.id
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_addressed_then_resolved_then_gone(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="fix the tense", by="u",
        )
        addressed = await world.historian.set_comment_state(
            chapter.id, comment_id=state.id,
            value=revisions.COMMENT_ADDRESSED, by="model",
        )
        assert addressed is not None
        assert addressed.state == revisions.COMMENT_ADDRESSED
        resolved = await world.historian.set_comment_state(
            chapter.id, comment_id=state.id,
            value=revisions.COMMENT_RESOLVED, by="human:prose-page",
        )
        assert resolved is None
        assert world.historian.comments(chapter.id) == ()

    async def test_direct_resolve_without_addressing_works(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="drop this", by="u",
        )
        assert await world.historian.set_comment_state(
            chapter.id, comment_id=state.id,
            value=revisions.COMMENT_RESOLVED, by="u",
        ) is None

    async def test_addressing_twice_writes_nothing(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="x", by="u",
        )
        await world.historian.set_comment_state(
            chapter.id, comment_id=state.id,
            value=revisions.COMMENT_ADDRESSED, by="model",
        )
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        again = await world.historian.set_comment_state(
            chapter.id, comment_id=state.id,
            value=revisions.COMMENT_ADDRESSED, by="model",
        )
        assert again is not None
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_an_unknown_id_error_lists_the_live_ids(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_comment(
            chapter.id, block_hash=_h(OPENING), text="x", by="u",
        )
        with pytest.raises(GraphContextError, match=state.id):
            await world.historian.set_comment_state(
                chapter.id, comment_id="cnosuch01",
                value=revisions.COMMENT_ADDRESSED, by="model",
            )

    async def test_a_comment_solidifies_the_pending_human_rollup(
        self,
    ) -> None:
        world, chapter = await _tracked_chapter()
        await world.repo.update_node(
            chapter.id, body="\n\n".join([OPENING, MIDDLE, REVISED])
        )
        assert await world.historian.record_external_revision(chapter.id)
        assert revisions.rollup_base(
            world.historian.entries(chapter.id)
        ) is not None
        await world.historian.record_comment(
            chapter.id, block_hash=_h(REVISED), text="nice", by="u",
        )
        assert revisions.rollup_base(
            world.historian.entries(chapter.id)
        ) is None
