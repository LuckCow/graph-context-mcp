"""WP7: writers report touched nodes at the source; drain dedups in order."""

from __future__ import annotations

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.application.node_writer import NodeWriter
from graph_context.application.prose_recorder import ProseRecorder
from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository


class TestJournalMechanics:
    def test_drain_returns_and_clears_deduped_in_order(self) -> None:
        journal = MutationJournal()
        journal.created("n1")
        journal.modified("n2")
        journal.modified("n1")  # created wins: first action per node
        drained = journal.drain()
        assert [(r.node_id, r.action) for r in drained] == [
            ("n1", "created"), ("n2", "modified"),
        ]
        assert journal.drain() == ()

    def test_null_journal_records_nothing(self) -> None:
        journal = NullJournal()
        journal.created("n1")
        journal.modified("n2")
        assert journal.drain() == ()


class TestWritersReport:
    async def test_create_with_incoming_link_reports_both_touches(self) -> None:
        repository = InMemoryGraphRepository()
        journal = MutationJournal()
        writer = NodeWriter(repository, SessionState(), journal)
        mira = await writer.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.")
        )
        journal.drain()  # isolate the composite create below
        event = await writer.create_node(
            NodeDraft("Event", name="Siege", summary="s", story_time=10),
            links=[LinkSpec("participated_in", other=mira.id, outgoing=False)],
        )
        drained = {(r.node_id, r.action) for r in journal.drain()}
        # Incoming link mutates Mira's relation list; outgoing links land
        # with the create and touch no other object.
        assert drained == {(event.id, "created"), (mira.id, "modified")}

    async def test_update_reports_modified(self) -> None:
        repository = InMemoryGraphRepository()
        journal = MutationJournal()
        writer = NodeWriter(repository, SessionState(), journal)
        node = await writer.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.")
        )
        journal.drain()
        await writer.update_node(node.id, summary="Leads the survivors.")
        assert {(r.node_id, r.action) for r in journal.drain()} == {
            (node.id, "modified"),
        }

    async def test_prose_capture_reports_the_artifact(self) -> None:
        repository = InMemoryGraphRepository()
        journal = MutationJournal()
        writer = NodeWriter(repository, SessionState(), journal)
        place = await writer.create_node(
            NodeDraft("Location", name="Keep", summary="s")
        )
        journal.drain()
        recorder = ProseRecorder(repository, now=lambda: "t", journal=journal)
        prose = await recorder.record(
            text="Ash drifted.", summary="s", references=[place.id]
        )
        assert {(r.node_id, r.action) for r in journal.drain()} == {
            (prose.id, "created"),
        }

    async def test_default_journal_is_null(self) -> None:
        repository = InMemoryGraphRepository()
        writer = NodeWriter(repository, SessionState())  # no journal wired
        await writer.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.")
        )  # must not raise, must not accumulate anywhere
