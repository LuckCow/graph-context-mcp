"""Scheduler service (WP18, ADR 027): set/list/cancel + the tick contract."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from graph_context.application.mutation_journal import MutationJournal
from graph_context.application.scheduler import Scheduler, local_clock
from graph_context.domain import scheduling
from graph_context.domain.models import NodeDraft
from graph_context.domain.schema import Role
from graph_context.errors import GraphContextError, NodeNotFound, SchemaViolation
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository


class Clock:
    """A settable now() so tests move time explicitly."""

    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment)


@pytest.fixture
def clock() -> Clock:
    return Clock("2026-07-12 16:00:00")


class TestLocalClock:
    """GC_TIMEZONE (ADR 027): 'local' means the USER's region, not the
    container's clock (which usually sits at UTC)."""

    def test_a_named_zone_yields_naive_wall_time_in_that_zone(self) -> None:
        now = local_clock("America/New_York")()
        expected = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        assert now.tzinfo is None  # the schedule convention stays naive
        assert abs((now - expected).total_seconds()) < 5

    def test_empty_falls_back_to_the_system_clock(self) -> None:
        assert abs(
            (local_clock("")() - datetime.now()).total_seconds()
        ) < 5

    def test_unknown_zone_fails_loudly_at_startup_with_guidance(self) -> None:
        with pytest.raises(GraphContextError) as err:
            local_clock("Mars/Olympus_Mons")
        message = str(err.value)
        assert "GC_TIMEZONE" in message and "America/Chicago" in message


@pytest.fixture
def journal() -> MutationJournal:
    return MutationJournal()


@pytest.fixture
def scheduler(
    repository: InMemoryGraphRepository, journal: MutationJournal, clock: Clock
) -> Scheduler:
    return Scheduler(repository, journal=journal, now=clock)


class TestSet:
    async def test_one_shot_creates_an_infra_node_with_the_stored_fields(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        journal: MutationJournal,
    ) -> None:
        node, next_at = await scheduler.set(
            "tax reminder", "2027-04-08T09:00",
            "Remind Nick taxes are due April 15.", "anytype:chat-1",
        )
        stored = repository.graph.node(node.id)
        assert stored.role is Role.SCHEDULED
        assert stored.fields[scheduling.FIELD_SCHEDULE] == "2027-04-08T09:00"
        assert stored.fields[scheduling.FIELD_PROMPT].startswith("Remind Nick")
        assert stored.fields[scheduling.FIELD_STATUS] == scheduling.STATUS_PENDING
        assert stored.fields[scheduling.FIELD_SESSION_KEY] == "anytype:chat-1"
        assert scheduling.FIELD_LAST_FIRED not in stored.fields
        assert next_at == datetime.fromisoformat("2027-04-08T09:00")
        assert stored.summary  # creation invariant: bookkeeping still explains itself
        assert [(r.node_id, r.action) for r in journal.drain()] == [
            (node.id, "created")
        ]

    async def test_recurring_is_armed_at_creation(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        node, next_at = await scheduler.set(
            "standup", "0 9 * * 1", "Post the standup summary.", "anytype:chat-1",
        )
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_LAST_FIRED] == "2026-07-12 16:00:00"
        # 2026-07-12 is a Sunday; the first fire is Monday 09:00, not now.
        assert next_at == datetime.fromisoformat("2026-07-13T09:00")
        assert scheduler.tick().fire == ()

    async def test_a_past_one_shot_is_rejected_with_the_current_time(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(GraphContextError) as err:
            await scheduler.set("late", "2026-01-01T09:00", "p", "")
        message = str(err.value)
        assert "past" in message and "2026-07-12 16:00:00" in message

    async def test_neither_prompt_nor_message_is_rejected_teaching_both(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(GraphContextError) as err:
            await scheduler.set("nameless", "2027-01-01T09:00", "   ", "")
        message = str(err.value)
        assert "'prompt'" in message and "'message'" in message
        assert "verbatim" in message and "neither" in message

    async def test_prompt_and_message_together_are_rejected(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(GraphContextError, match="both -- pick one"):
            await scheduler.set(
                "greedy", "2027-01-01T09:00", "think about it", "",
                message="Taxes are due.",
            )

    async def test_message_only_stores_the_message_and_no_prompt_or_mode(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        node, _ = await scheduler.set(
            "tax note", "2027-04-08T09:00", "", "anytype:chat-1",
            message="Taxes are due April 15.",
        )
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_MESSAGE] == "Taxes are due April 15."
        assert scheduling.FIELD_PROMPT not in stored.fields
        assert scheduling.FIELD_MODE not in stored.fields

    async def test_mode_with_message_is_rejected(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(GraphContextError, match="no LLM turn"):
            await scheduler.set(
                "confused", "2027-01-01T09:00", "", "",
                message="Taxes are due.", mode="research",
            )

    async def test_document_type_with_message_is_rejected(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(GraphContextError, match="nothing writes a document"):
            await scheduler.set(
                "confused", "2027-01-01T09:00", "", "",
                message="Taxes are due.", document_type="Report",
            )

    async def test_a_document_type_is_stored_unchecked_on_prompt_events(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        # Lenient text like mode (ADR 057): no type vocabulary at set
        # time even where modes have one -- a typo surfaces at fire time
        # as a create_node error the model self-corrects.
        scheduler.mode_names = lambda: ["assistant", "research"]
        node, _ = await scheduler.set(
            "newsletter", "0 9 * * 5", "Compile the weekly digest.", "",
            mode="research", document_type="Totally Unknown Type",
        )
        stored = repository.graph.node(node.id)
        assert (
            stored.fields[scheduling.FIELD_DOCUMENT_TYPE]
            == "Totally Unknown Type"
        )

    async def test_unknown_mode_is_rejected_when_the_vocabulary_is_wired(
        self, scheduler: Scheduler,
    ) -> None:
        scheduler.mode_names = lambda: ["assistant", "research"]
        with pytest.raises(GraphContextError) as err:
            await scheduler.set(
                "digest", "0 9 * * 1", "Compile the digest.", "", mode="nope",
            )
        message = str(err.value)
        assert "assistant, research" in message
        assert "omit 'mode'" in message  # the default-mode escape hatch

    async def test_a_known_mode_is_stored(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        scheduler.mode_names = lambda: ["assistant", "research"]
        node, _ = await scheduler.set(
            "digest", "0 9 * * 1", "Compile the digest.", "", mode="research",
        )
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_MODE] == "research"

    async def test_mode_is_unchecked_without_the_vocabulary(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        # The bare MCP server never wires mode_names (ADR 055): set
        # stores the name and the fire-time degrade covers typos.
        assert scheduler.mode_names is None
        node, _ = await scheduler.set(
            "digest", "0 9 * * 1", "Compile.", "", mode="anything",
        )
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_MODE] == "anything"

    async def test_bad_schedule_error_reaches_the_caller(
        self, scheduler: Scheduler,
    ) -> None:
        with pytest.raises(SchemaViolation, match="cron"):
            await scheduler.set("bad", "whenever", "p", "")


class TestTick:
    async def test_due_one_shot_fires_with_prompt_and_session_key(
        self, scheduler: Scheduler, clock: Clock,
    ) -> None:
        node, _ = await scheduler.set(
            "tax reminder", "2027-04-08T09:00", "Remind Nick.", "anytype:chat-1",
        )
        assert scheduler.tick().fire == ()  # not yet
        clock.advance_to("2027-04-10 12:00:00")  # two days late: still fires once
        due = scheduler.tick().fire
        assert [d.node_id for d in due] == [node.id]
        assert due[0].prompt == "Remind Nick."
        assert due[0].session_key == "anytype:chat-1"

    async def test_mark_fired_spends_the_one_shot_and_keeps_other_fields(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        node, _ = await scheduler.set(
            "tax reminder", "2027-04-08T09:00", "Remind Nick.", "anytype:chat-1",
        )
        clock.advance_to("2027-04-08 09:01:00")
        await scheduler.mark_fired(node.id)
        assert scheduler.tick().fire == ()
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_PROMPT] == "Remind Nick."
        assert stored.fields[scheduling.FIELD_LAST_FIRED] == "2027-04-08 09:01:00"
        # A fired one-shot completes; a fired recurring event stays Pending
        # (covered in test_recurring_fires_once_per_occurrence).
        assert stored.fields[scheduling.FIELD_STATUS] == scheduling.STATUS_COMPLETED

    async def test_recurring_fires_once_per_occurrence(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        node, _ = await scheduler.set("daily", "0 9 * * *", "Check in.", "")
        clock.advance_to("2026-07-13 09:00:00")
        assert [d.node_id for d in scheduler.tick().fire] == [node.id]
        await scheduler.mark_fired(node.id)
        assert scheduler.tick().fire == ()  # spent until tomorrow 09:00
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_STATUS] == scheduling.STATUS_PENDING
        clock.advance_to("2026-07-14 09:00:30")
        assert [d.node_id for d in scheduler.tick().fire] == [node.id]

    async def test_a_ui_created_recurring_event_is_armed_not_fired(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        # A human creates the object in Anytype: no gc_last_fired anchor.
        node = await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="weekly", summary="s",
            fields={
                scheduling.FIELD_SCHEDULE: "0 9 * * 1",
                scheduling.FIELD_PROMPT: "Review the backlog.",
            },
        ))
        tick = scheduler.tick()
        assert tick.fire == () and tick.arm == (node.id,)
        await scheduler.arm(node.id)
        assert scheduler.tick().arm == ()
        clock.advance_to("2026-07-13 09:00:00")  # the next Monday
        assert [d.node_id for d in scheduler.tick().fire] == [node.id]

    async def test_disabled_and_invalid_schedules_never_fire(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        for schedule in ("off", "", "not a schedule"):
            await repository.create_node(NodeDraft(
                type=scheduling.SCHEDULED_TYPE_KEY, name=f"n-{schedule!r}",
                summary="s",
                fields={scheduling.FIELD_SCHEDULE: schedule,
                        scheduling.FIELD_PROMPT: "p"},
            ))
        clock.advance_to("2030-01-01 00:00:00")
        tick = scheduler.tick()
        assert tick.fire == () and tick.arm == ()

    async def test_a_message_event_fires_verbatim_with_no_prompt_or_mode(
        self, scheduler: Scheduler, clock: Clock,
    ) -> None:
        node, _ = await scheduler.set(
            "tax note", "2027-04-08T09:00", "", "anytype:chat-1",
            message="Taxes are due April 15.",
        )
        clock.advance_to("2027-04-08 09:00:00")
        due = scheduler.tick().fire
        assert [d.node_id for d in due] == [node.id]
        assert due[0].message == "Taxes are due April 15."
        assert due[0].prompt == "" and due[0].mode == ""
        assert due[0].session_key == "anytype:chat-1"

    async def test_a_prompt_event_carries_its_mode(
        self, scheduler: Scheduler, clock: Clock,
    ) -> None:
        await scheduler.set(
            "digest", "2027-04-08T09:00", "Compile the digest.", "",
            mode="research",
        )
        clock.advance_to("2027-04-08 09:00:00")
        due = scheduler.tick().fire
        assert due[0].mode == "research" and due[0].message == ""

    async def test_a_prompt_event_carries_its_document_type(
        self, scheduler: Scheduler, clock: Clock,
    ) -> None:
        await scheduler.set(
            "newsletter", "2027-04-09T09:00", "Compile the digest.", "",
            document_type="Report",
        )
        clock.advance_to("2027-04-09 09:00:00")
        due = scheduler.tick().fire
        assert due[0].document_type == "Report"

    async def test_the_message_wins_when_a_human_stored_both(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        # Only reachable from the Anytype UI -- set() enforces one-of.
        # Firing the human's exact words beats an inert reminder (ADR 055).
        await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="doubled", summary="s",
            fields={
                scheduling.FIELD_SCHEDULE: "2026-07-12T17:00",
                scheduling.FIELD_PROMPT: "Think hard about taxes.",
                scheduling.FIELD_MESSAGE: "Taxes are due April 15.",
                scheduling.FIELD_MODE: "research",
                scheduling.FIELD_DOCUMENT_TYPE: "Report",
            },
        ))
        clock.advance_to("2026-07-12 17:00:00")
        due = scheduler.tick().fire
        assert due[0].message == "Taxes are due April 15."
        assert due[0].prompt == "" and due[0].mode == ""
        assert due[0].document_type == ""  # no turn runs, no document

    async def test_a_fired_event_with_no_prompt_falls_back_to_its_name(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        node = await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="water the plants",
            summary="s",
            fields={scheduling.FIELD_SCHEDULE: "2026-07-12T17:00"},
        ))
        clock.advance_to("2026-07-12 17:00:00")
        assert scheduler.tick().fire[0].prompt == "water the plants"
        assert node.fields.get(scheduling.FIELD_SESSION_KEY) is None


class TestCancelAndList:
    async def test_cancel_marks_cancelled_and_keeps_the_schedule(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        journal: MutationJournal, clock: Clock,
    ) -> None:
        node, _ = await scheduler.set(
            "tax reminder", "2027-04-08T09:00", "Remind Nick.", "",
        )
        journal.drain()
        cancelled = await scheduler.cancel("Tax Reminder")  # case-insensitive
        assert cancelled.id == node.id
        stored = repository.graph.node(node.id)
        assert stored.fields[scheduling.FIELD_STATUS] == "Cancelled"
        # The schedule survives, so a human can re-enable in the UI by
        # flipping the status back to Pending.
        assert stored.fields[scheduling.FIELD_SCHEDULE] == "2027-04-08T09:00"
        assert stored.fields[scheduling.FIELD_PROMPT] == "Remind Nick."
        clock.advance_to("2030-01-01 00:00:00")
        assert scheduler.tick().fire == ()
        assert [(r.node_id, r.action) for r in journal.drain()] == [
            (node.id, "modified")
        ]

    async def test_flipping_the_status_back_to_pending_reenables(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        node, _ = await scheduler.set(
            "tax reminder", "2027-04-08T09:00", "Remind Nick.", "",
        )
        await scheduler.cancel(node.id)
        clock.advance_to("2027-04-08 09:00:00")
        assert scheduler.tick().fire == ()
        # The human's re-enable gesture in the Anytype UI.
        stored = repository.graph.node(node.id)
        await repository.update_node(node.id, fields={
            **dict(stored.fields),
            scheduling.FIELD_STATUS: scheduling.STATUS_PENDING,
        })
        assert [d.node_id for d in scheduler.tick().fire] == [node.id]

    async def test_cancel_rejects_a_non_scheduled_node(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
    ) -> None:
        mira = await repository.create_node(
            NodeDraft("Character", name="Mira", summary="s")
        )
        with pytest.raises(GraphContextError, match="not a Scheduled Event"):
            await scheduler.cancel(mira.id)

    async def test_cancel_miss_and_ambiguity_are_actionable(
        self, scheduler: Scheduler,
    ) -> None:
        await scheduler.set("twin", "2027-01-01T09:00", "a", "")
        await scheduler.set("twin", "2027-01-02T09:00", "b", "")
        with pytest.raises(NodeNotFound):
            await scheduler.cancel("no such event")
        with pytest.raises(GraphContextError, match="2 scheduled events"):
            await scheduler.cancel("twin")

    async def test_events_render_every_status(
        self, scheduler: Scheduler, repository: InMemoryGraphRepository,
        clock: Clock,
    ) -> None:
        await scheduler.set("future", "2027-04-08T09:00", "p", "")
        await scheduler.set("weekly", "0 9 * * 1", "p", "")
        spent, _ = await scheduler.set("soon", "2026-07-12T17:00", "p", "")
        clock.advance_to("2026-07-12 17:00:00")
        await scheduler.mark_fired(spent.id)
        cancelled, _ = await scheduler.set("gone", "2027-05-01T09:00", "p", "")
        await scheduler.cancel(cancelled.id)
        await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="broken", summary="s",
            fields={scheduling.FIELD_SCHEDULE: "not a schedule",
                    scheduling.FIELD_PROMPT: "p"},
        ))
        status = {v.node.name: v.status for v in scheduler.events()}
        assert status["future"].startswith("once at 2027-04-08 09:00; next")
        assert "next 2026-07-13 09:00" in status["weekly"]
        # A fired one-shot is marked Completed by mark_fired.
        assert status["soon"] == "completed (once at 2026-07-12 17:00)"
        assert status["gone"] == "cancelled (once at 2027-05-01 09:00)"
        assert status["broken"].startswith("invalid:")

    async def test_list_views_carry_the_document_type(
        self, scheduler: Scheduler,
    ) -> None:
        await scheduler.set(
            "newsletter", "0 9 * * 5", "Compile.", "",
            document_type="Report",
        )
        await scheduler.set("plain", "0 9 * * 1", "p", "")
        docs = {v.node.name: v.document_type for v in scheduler.events()}
        assert docs == {"newsletter": "Report", "plain": ""}
