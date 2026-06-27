"""Shared fixtures.

``world`` builds a small but representative story world *through the same
code paths production uses* (NodeWriter + repository), so fixtures double
as an integration smoke test of the write side. Layout::

    Mira (Character) --participated_in--> Siege of Brakk (Event, t=10)
    Mira (Character) --located_at-------> The Undercroft (Location)
    Siege of Brakk   --located_at-------> The Undercroft
    Mira (Character) --participated_in--> Fall of Brakk (Event, t=99)   # "future"
    Mira (Character) --possesses--------> Ashbrand (Item)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from graph_context.application.node_writer import NodeWriter
from graph_context.domain.models import LinkSpec, Node, NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository


@pytest.fixture
def repository() -> InMemoryGraphRepository:
    return InMemoryGraphRepository()

@pytest.fixture
def session() -> SessionState:
    return SessionState(project="Ashfall")


@pytest.fixture
def writer(repository: InMemoryGraphRepository, session: SessionState) -> NodeWriter:
    return NodeWriter(repository, session)


@dataclass(frozen=True)
class World:
    mira: Node
    undercroft: Node
    siege: Node
    fall: Node
    ashbrand: Node


@pytest.fixture
async def world(writer: NodeWriter) -> World:
    mira = await writer.create_node(
        NodeDraft("Character", name="Mira", summary="Exiled siege engineer.")
    )
    undercroft = await writer.create_node(
        NodeDraft("Location", name="The Undercroft", summary="Vaults beneath Brakk."),
        links=[LinkSpec("located_at", other=mira.id, outgoing=False)],
    )
    siege = await writer.create_node(
        NodeDraft(
            "Event",
            name="Siege of Brakk",
            summary="The city falls after a year-long siege.",
            story_time=10,
        ),
        links=[
            LinkSpec("participated_in", other=mira.id, outgoing=False),
            LinkSpec("located_at", other=undercroft.id),
        ],
    )
    fall = await writer.create_node(
        NodeDraft(
            "Event",
            name="Fall of Brakk",
            summary="Brakk is razed; survivors scatter.",
            story_time=99,
        ),
        links=[LinkSpec("participated_in", other=mira.id, outgoing=False)],
    )
    ashbrand = await writer.create_node(
        NodeDraft("Item", name="Ashbrand", summary="A blade quenched in ash."),
        links=[LinkSpec("possesses", other=mira.id, outgoing=False)],
    )
    return World(mira=mira, undercroft=undercroft, siege=siege, fall=fall, ashbrand=ashbrand)
