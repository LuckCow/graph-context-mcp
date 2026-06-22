"""The GraphRepository contract, run against a live Anytype space.

This is the "third subclass behind ANYTYPE_E2E=1" that
``tests/contract/test_graph_repository_contract.py`` calls for. It reuses
the exact same behavioral spec; the ``repo`` fixture (live, bootstrapped
space) is provided by ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

from tests.contract.test_graph_repository_contract import GraphRepositoryContract


class TestAnytypeLiveRepository(GraphRepositoryContract):
    """Certifies the live adapter against the same contract as the fakes."""
