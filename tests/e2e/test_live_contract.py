"""The GraphRepository contract, run against a live Anytype space.

This is the "third subclass behind ANYTYPE_E2E=1" that
``tests/contract/test_graph_repository_contract.py`` calls for. It reuses
the exact same behavioral spec; the ``repo`` fixture (live, bootstrapped
space) is provided by ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

from graph_context.domain.models import LinkSpec, NodeDraft
from tests.contract.test_graph_repository_contract import GraphRepositoryContract


class TestAnytypeLiveRepository(GraphRepositoryContract):
    """Certifies the live adapter against the same contract as the fakes."""

    async def test_create_with_brand_new_outgoing_relation_links_in_one_call(self, repo):
        """Regression (Mary Abbott incident): creating a node with an outgoing
        link whose relation does not exist yet must succeed atomically.

        The live API rejects a not-yet-attached relation inlined in the create
        body (``400 unknown property key``); the adapter must therefore create
        the relation, POST the object, then PATCH the relation on. Previously
        this forced agents into a create-then-update workaround. ``inspired_by``
        is not in the bootstrapped vocabulary, so it is genuinely new.
        """
        target = await repo.create_node(
            NodeDraft("Organization", name="Rental Family", summary="Reference work.")
        )
        node = await repo.create_node(
            NodeDraft("Character", name="Mary Abbott", summary="Marketer."),
            links=[LinkSpec("inspired_by", other=target.id, outgoing=True)],
            create_missing_relations=True,
        )
        edges = [(e.type, e.target) for e in repo.graph.edges(node.id)]
        assert ("inspired_by", target.id) in edges
        assert repo.registry.key_for_label("inspired_by") is not None
