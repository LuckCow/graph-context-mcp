"""Fixtures for the tool-layer tests.

``services`` wraps the *same* ``repository``/``session`` the top-level
``world`` fixture builds through, so a test can request both and have the
tools operate over the populated graph.
"""

from __future__ import annotations

import pytest

from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.services import Services, build_services


@pytest.fixture
def services(repository: InMemoryGraphRepository, session: SessionState) -> Services:
    return build_services(repository, session)
