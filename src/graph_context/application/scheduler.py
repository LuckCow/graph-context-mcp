"""Use-case: managing and firing Scheduled Events (WP18, ADR 027).

A Scheduled Event is a ``gc_scheduled_event`` node holding a schedule (a
one-shot local datetime or a cron line -- ``domain/scheduling.py`` owns
the format), a prompt the LLM is handed when the event comes due, and
the session key of the chat the fired turn belongs to. Two producers,
one consumer:

* the ``schedule`` tool (LLM) and the Anytype UI (human) create/edit
  the nodes;
* the orchestrator's scheduler loop calls :meth:`tick` every few
  seconds and fires what is due.

Firing bookkeeping lives in ``gc_last_fired`` on the node itself, so it
survives restarts and is visible/repairable in the Anytype UI. The rules
(one-shot fires once until rescheduled; a recurring event must be ARMED
-- anchored at a first ``gc_last_fired`` stamp -- before it fires, so a
fresh "every Monday 09:00" waits for Monday; downtime catches up with
ONE fire, not one per missed occurrence) are :func:`scheduling.due_at`'s;
this service only decides which stamp to write when.

Like ``CaptureRecorder``, writes go straight to the repository (no
NodeWriter): scheduled events are infrastructure bookkeeping, exempt
from story-node invariants, and must not enter the session's recent
trail. ``now`` is injectable and NAIVE LOCAL time -- the schedule
format's convention (cron lines have no timezone).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.domain import scheduling
from graph_context.domain.models import Node, NodeDraft, NodeId
from graph_context.domain.schema import Role
from graph_context.errors import GraphContextError, NodeNotFound
from graph_context.ports.graph_repository import GraphRepository


def _local_now() -> datetime:
    # Naive local wall-clock time IS the schedule convention (cron lines
    # have no timezone); see domain/scheduling.py.
    return datetime.now()


def local_clock(timezone: str = "") -> Callable[[], datetime]:
    """The scheduler's wall clock, pinned to an IANA timezone.

    "Local time" must mean the USER's region, but the process usually
    runs in a container whose system clock is UTC -- so the composition
    root passes ``GC_TIMEZONE`` (e.g. ``America/Chicago``) through here.
    Empty falls back to the system-local clock (correct when the host's
    TZ is configured). An unknown name fails loudly HERE, at startup,
    never inside a tick.
    """
    name = timezone.strip()
    if not name:
        return _local_now
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise GraphContextError(
            f"unknown GC_TIMEZONE {name!r}; use an IANA zone name like "
            "America/Chicago or Europe/Berlin (empty = the system clock)"
        ) from None

    def now() -> datetime:
        # Naive in that zone: the schedule convention (see scheduling.py).
        return datetime.now(zone).replace(tzinfo=None)

    return now


def _stamp(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


@dataclass(frozen=True, slots=True)
class ScheduledEventView:
    """One event as the ``schedule`` tool's list action renders it."""

    node: Node
    status: str  # human/LLM-readable: next fire, spent, disabled, invalid
    prompt: str
    session_key: str


@dataclass(frozen=True, slots=True)
class DueEvent:
    """Everything the orchestrator loop needs to fire one event."""

    node_id: NodeId
    name: str
    prompt: str
    session_key: str


@dataclass(frozen=True, slots=True)
class SchedulerTick:
    """One scan's outcome: events to fire, recurring strays to arm.

    ``arm`` holds recurring events with no ``gc_last_fired`` anchor yet
    (human-created in the UI; tool-created ones are armed at creation) --
    the loop stamps them without firing.
    """

    fire: tuple[DueEvent, ...] = ()
    arm: tuple[NodeId, ...] = ()


class Scheduler:
    """Scheduled-event reads and writes over the shared repository."""

    def __init__(
        self,
        repository: GraphRepository,
        journal: MutationJournal | None = None,
        now: Callable[[], datetime] = _local_now,
    ) -> None:
        self._repository = repository
        self._journal = journal or NullJournal()
        self._now = now

    def now(self) -> datetime:
        """The scheduler's local wall-clock reading, for response echoes
        (the LLM doing 'a week before April 15' math needs today's date)."""
        return self._now()

    async def set(
        self, name: str, schedule_text: str, prompt: str, session_key: str
    ) -> tuple[Node, datetime | None]:
        """Create a Scheduled Event; returns the node and its next fire time.

        A one-shot in the past is rejected with the current server time so
        the caller (an LLM doing date math) can self-correct. A recurring
        event is armed immediately -- anchored at creation -- so its first
        fire is the next occurrence from now.
        """
        if not name.strip():
            raise GraphContextError("a scheduled event needs a non-empty 'name'")
        if not prompt.strip():
            raise GraphContextError(
                "a scheduled event needs a non-empty 'prompt' -- the "
                "instructions you will be given when it fires (e.g. "
                "'Remind Nick that taxes are due April 15.')"
            )
        schedule = scheduling.parse_schedule(schedule_text)
        now = self._now()
        if isinstance(schedule, scheduling.OneShot) and schedule.at <= now:
            raise GraphContextError(
                f"schedule {schedule_text!r} is in the past; the server's "
                f"local time is {_stamp(now)} -- resend with a future time"
            )
        fields = {
            scheduling.FIELD_SCHEDULE: schedule_text.strip(),
            scheduling.FIELD_PROMPT: prompt.strip(),
            scheduling.FIELD_STATUS: scheduling.STATUS_PENDING,
            scheduling.FIELD_SESSION_KEY: session_key,
        }
        if isinstance(schedule, scheduling.Cron):
            fields[scheduling.FIELD_LAST_FIRED] = _stamp(now)  # armed at birth
        node = await self._repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY,
            name=name.strip(),
            summary=f"fires {schedule.describe()}",
            fields=fields,
            icon="⏰",
        ))
        self._journal.created(node.id)
        return node, scheduling.next_fire(schedule, after=now)

    async def cancel(self, identifier: str) -> Node:
        """Mark an event Cancelled. The node AND its schedule stay intact
        -- a human re-enables it by flipping the status back to Pending
        in Anytype (which is why cancel must not clobber the schedule)."""
        node = self._find(identifier)
        await self._write_fields(
            node, {scheduling.FIELD_STATUS: scheduling.STATUS_CANCELLED}
        )
        self._journal.modified(node.id)
        return self._repository.graph.node(node.id)

    def events(self) -> list[ScheduledEventView]:
        """Every Scheduled Event with a rendered status, name-sorted."""
        now = self._now()
        views = [
            ScheduledEventView(
                node=node,
                status=self._status(node, now),
                prompt=node.fields.get(scheduling.FIELD_PROMPT, ""),
                session_key=node.fields.get(scheduling.FIELD_SESSION_KEY, ""),
            )
            for node in self._scheduled_nodes()
        ]
        return sorted(views, key=lambda v: (v.node.name.casefold(), v.node.id))

    def tick(self) -> SchedulerTick:
        """One scan: what to fire now, what to arm. Pure read -- the loop
        writes via :meth:`arm` / :meth:`mark_fired` so a failed write can
        retry next tick."""
        now = self._now()
        fire: list[DueEvent] = []
        arm: list[NodeId] = []
        for node in self._scheduled_nodes():
            if not scheduling.is_active(
                node.fields.get(scheduling.FIELD_STATUS, "")
            ):
                continue  # Completed/Cancelled: inert until re-Pending'd
            raw = node.fields.get(scheduling.FIELD_SCHEDULE, "")
            if scheduling.is_disabled(raw):
                continue
            try:
                schedule = scheduling.parse_schedule(raw)
            except GraphContextError:
                continue  # visible as "invalid" in events(); never fires
            last_fired = self._last_fired(node)
            if isinstance(schedule, scheduling.Cron) and last_fired is None:
                arm.append(node.id)
                continue
            due = scheduling.due_at(schedule, last_fired)
            if due is not None and due <= now:
                fire.append(DueEvent(
                    node_id=node.id,
                    name=node.name,
                    prompt=node.fields.get(scheduling.FIELD_PROMPT, "").strip()
                    or node.name,
                    session_key=node.fields.get(
                        scheduling.FIELD_SESSION_KEY, ""
                    ).strip(),
                ))
        return SchedulerTick(fire=tuple(fire), arm=tuple(arm))

    async def arm(self, node_id: NodeId) -> None:
        """Anchor a recurring stray at now, without firing (see tick)."""
        await self.mark_fired(node_id)

    async def mark_fired(self, node_id: NodeId) -> None:
        """Stamp ``gc_last_fired`` = now and settle the status: a fired
        one-shot is Completed, a recurring event stays Pending (this also
        reconciles the empty status of a UI-created event). The loop
        calls this BEFORE the fired turn runs (at-most-once: a crashing
        turn must not re-fire every tick; its error still reaches the
        chat through the turn's reply surface)."""
        node = self._repository.graph.node(node_id)
        one_shot = False
        try:
            schedule = scheduling.parse_schedule(
                node.fields.get(scheduling.FIELD_SCHEDULE, "")
            )
            one_shot = isinstance(schedule, scheduling.OneShot)
        except GraphContextError:
            pass  # unparseable mid-edit: stamp the fire, stay Pending
        await self._write_fields(node, {
            scheduling.FIELD_LAST_FIRED: _stamp(self._now()),
            scheduling.FIELD_STATUS: (
                scheduling.STATUS_COMPLETED if one_shot
                else scheduling.STATUS_PENDING
            ),
        })

    # -- internals -------------------------------------------------------

    def _scheduled_nodes(self) -> list[Node]:
        return [
            node for node in self._repository.graph.nodes()
            if node.role is Role.SCHEDULED
        ]

    def _find(self, identifier: str) -> Node:
        """Resolve an id or an exact name AMONG scheduled events.

        The graph's shared name resolution deliberately excludes infra
        roles, so the schedule tool does its own: raw id first, then an
        exact (case-insensitive) name match; ambiguity lists candidates.
        """
        wanted = identifier.strip()
        if not wanted:
            raise GraphContextError(
                "pass 'node_id': a scheduled event's id or exact name "
                "(action='list' shows both)"
            )
        graph = self._repository.graph
        if graph.has_node(wanted):
            node = graph.node(wanted)
            if node.role is not Role.SCHEDULED:
                raise GraphContextError(
                    f"{node.name!r} ({node.type}) is not a Scheduled Event"
                )
            return node
        matches = [
            node for node in self._scheduled_nodes()
            if node.name.strip().casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NodeNotFound(identifier)
        listing = "; ".join(f"{n.name} (id={n.id})" for n in matches)
        raise GraphContextError(
            f"{identifier!r} names {len(matches)} scheduled events: "
            f"{listing}. Retry with an exact id."
        )

    async def _write_fields(self, node: Node, changes: dict[str, str]) -> None:
        # The full merged map, not a delta: the in-memory backend replaces
        # ``fields`` wholesale on update, and a delta would drop the rest.
        merged = {**dict(node.fields), **changes}
        await self._repository.update_node(node.id, fields=merged)

    def _last_fired(self, node: Node) -> datetime | None:
        raw = node.fields.get(scheduling.FIELD_LAST_FIRED, "").strip()
        if not raw:
            return None
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None  # unreadable stamp: treat as never fired (re-arm)
        if moment.tzinfo is not None:
            moment = moment.replace(tzinfo=None)  # lenient human edit
        return moment

    def _status(self, node: Node, now: datetime) -> str:
        raw = node.fields.get(scheduling.FIELD_SCHEDULE, "")
        stored = node.fields.get(scheduling.FIELD_STATUS, "").strip()
        if not scheduling.is_active(stored):
            # Completed/Cancelled: show the lifecycle word (and what the
            # schedule was, when readable) -- re-enabled via Pending.
            try:
                described = f" ({scheduling.parse_schedule(raw).describe()})"
            except GraphContextError:
                described = ""
            return f"{stored.lower()}{described}"
        if scheduling.is_disabled(raw):
            return "disabled (no schedule)"
        try:
            schedule = scheduling.parse_schedule(raw)
        except GraphContextError as err:
            return f"invalid: {err}"
        last_fired = self._last_fired(node)
        if isinstance(schedule, scheduling.Cron) and last_fired is None:
            # An unarmed stray (UI-created); the loop arms it within a tick.
            upcoming = scheduling.next_fire(schedule, after=now)
            suffix = f"; next {_stamp(upcoming)}" if upcoming else "; never fires"
            return f"{schedule.describe()}{suffix}"
        due = scheduling.due_at(schedule, last_fired)
        if due is None:
            fired = f" (fired {_stamp(last_fired)})" if last_fired else ""
            return f"spent -- {schedule.describe()}{fired}"
        return f"{schedule.describe()}; next {_stamp(due)}"
