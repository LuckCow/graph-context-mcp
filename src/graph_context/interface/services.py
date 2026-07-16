"""The :class:`Services` bundle: everything a tool call needs (WP2/WP8).

Built once per runtime in the composition root (``build_services``) and
re-derived per chat session (``derive_services``, ADR 021) -- sessions
are cheap views over one shared repository, never runtimes of their own.
Kept SDK-free like the tool bodies; only the composition root sees MCP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.application.explorer import Explorer
from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.application.node_reader import NodeReader
from graph_context.application.node_writer import NodeWriter
from graph_context.application.querier import Querier
from graph_context.application.ranker import Ranker
from graph_context.application.scheduler import Scheduler, local_clock
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.application.session_persister import SessionPersister
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.view_catalog import ViewCatalog


@dataclass(frozen=True, slots=True)
class OutboundFile:
    """One file the model queued for delivery this turn (WP23)."""

    name: str
    content: str


@dataclass(slots=True)
class Services:
    """Everything a tool call needs, built once in the composition root."""

    repository: GraphRepository
    session: SessionState
    writer: NodeWriter
    reader: NodeReader
    explorer: Explorer
    querier: Querier
    capture: CaptureRecorder
    scheduler: Scheduler
    persister: SessionPersister | None = None  # wired in server lifespan
    # WP18 (ADR 027): the transport-scoped session key this view serves
    # ("mcp", "anytype:<chat_id>", ...). The schedule tool stamps it onto
    # scheduled events so a fired event's turn lands in the right chat.
    # "" (tests, bare construction) means "no specific chat".
    session_key: str = ""
    # WP7: the orchestrator passes a real MutationJournal and drains it per
    # turn; the MCP server keeps the NullJournal (no turn boundary).
    journal: MutationJournal = field(default_factory=NullJournal)
    # WP11 (ADR 014): None when GC_EMBEDDER=off -- the semantic layer
    # degrades away and tools fall back to name search alone.
    projector: SemanticProjector | None = None
    ranker: Ranker | None = None
    # WP23 (ADR 032): files the model queued with the send_file tool this
    # turn. TURN-scoped: the pipeline clears it as a turn starts and
    # drains it into file reply events after the last decision; the
    # transport turns those into real chat uploads (or a fenced fallback).
    outbox: list[OutboundFile] = field(default_factory=list)


def build_services(
    repository: GraphRepository,
    session: SessionState,
    persister: SessionPersister | None = None,
    *,
    journal: MutationJournal | None = None,
    projector: SemanticProjector | None = None,
    ranker: Ranker | None = None,
    views: ViewCatalog | None = None,
    session_key: str = "",
    timezone: str = "",
) -> Services:
    journal = journal or NullJournal()
    return Services(
        repository=repository,
        session=session,
        writer=NodeWriter(repository, session, journal),
        reader=NodeReader(repository, session),
        explorer=Explorer(repository, session),
        querier=Querier(repository, views),
        capture=CaptureRecorder(repository, journal=journal),
        # GC_TIMEZONE (ADR 027): schedules mean the USER's wall clock,
        # not the container's (usually UTC); resolved loudly at startup.
        scheduler=Scheduler(repository, journal=journal, now=local_clock(timezone)),
        persister=persister,
        journal=journal,
        projector=projector,
        ranker=ranker,
        session_key=session_key,
    )


def derive_services(
    base: Services,
    session: SessionState,
    persister: SessionPersister | None,
    session_key: str = "",
) -> Services:
    """A per-session view of one runtime (WP8): rebind the three
    session-bound services, share everything expensive by reference.

    Repository (and its GraphIndex), querier, capture, scheduler, journal,
    projector, and ranker stay THE runtime's instances -- sessions are
    views over one space, not runtimes of their own. Cheap: three thin
    wrappers, no I/O.
    """
    return Services(
        repository=base.repository,
        session=session,
        writer=NodeWriter(base.repository, session, base.journal),
        reader=NodeReader(base.repository, session),
        explorer=Explorer(base.repository, session),
        querier=base.querier,
        capture=base.capture,
        scheduler=base.scheduler,
        persister=persister,
        journal=base.journal,
        projector=base.projector,
        ranker=base.ranker,
        session_key=session_key,
    )
