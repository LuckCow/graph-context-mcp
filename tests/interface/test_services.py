"""Services derivation (WP42): the late-bound historian must reach every
lazily-derived session writer as its locked-section guard -- the pin for
bootstrap's ``built.services.historian = historian`` gesture."""

from __future__ import annotations

import pytest

from graph_context.application.node_historian import NodeHistorian
from graph_context.domain import revisions
from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.errors import LockedSectionsChanged
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)
from graph_context.interface.services import build_services, derive_services

OPENING = "The city fell quiet before the siege began, every gate barred."


async def test_late_bound_historian_guards_derived_writers() -> None:
    repository = InMemoryGraphRepository()
    base = build_services(repository, SessionState())
    await repository.create_node(NodeDraft(
        type="gc_space_context", name="Space Context", summary="cfg",
        fields={revisions.FIELD_TRACKED_TYPES: "Chapter"},
    ))
    chapter = await repository.create_node(NodeDraft(
        type="Chapter", name="Chapter One", summary="ch", body=OPENING,
    ))
    historian = NodeHistorian(repository)
    await historian.record_bot_revision(chapter.id, author_detail="m")
    await historian.record_mark(
        chapter.id, kind="intent",
        block_hash=revisions.block_hash(revisions.normalize_block(OPENING)),
        value="locked", by="user",
    )
    base.historian = historian  # bootstrap's post-construction late-bind
    derived = derive_services(
        base, SessionState(), None, session_key="anytype:c1"
    )
    assert derived.historian is historian
    with pytest.raises(LockedSectionsChanged):
        await derived.writer.update_node(
            chapter.id, description="Something else entirely."
        )
