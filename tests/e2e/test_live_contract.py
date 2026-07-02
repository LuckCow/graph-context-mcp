"""The GraphRepository contract, run against a live Anytype space.

This is the "third subclass behind ANYTYPE_E2E=1" that
``tests/contract/test_graph_repository_contract.py`` calls for. It reuses
the exact same behavioral spec; the ``repo`` fixture (live, bootstrapped
space) is provided by ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.anytype.client import AnytypeClient
from tests.contract.test_graph_repository_contract import GraphRepositoryContract


async def test_get_space_returns_a_name(live_config) -> None:
    """Pins the /v1/spaces/{id} envelope the project-label default reads."""
    client = AnytypeClient(live_config)
    try:
        space = await client.get_space()
    finally:
        await client.aclose()
    assert space.get("name")


async def test_a7_body_editing_field_name_mismatch(live_config) -> None:
    """Pins A7 (ADR 010) against the real server, raw client level.

    Create takes ``body``; update takes ``markdown`` (wholesale replace)
    while a ``body`` key in PATCH is silently ignored; neither the hydrate
    sweep nor search ever returns ``markdown``. The contract subclass below
    certifies the same semantics through the repository -- this test exists
    so a server-side change to the raw quirk is caught by name.

    Note the live server normalizes markdown on store (S6), so assertions
    use ``strip()``, never byte equality.
    """
    client = AnytypeClient(live_config)
    try:
        created = await client.create_object(
            {"name": "A7 pin", "type_key": "page", "body": "v1 original"}
        )
        object_id = created["id"]
        await client.update_object(object_id, {"body": "clobber attempt"})
        assert (await client.get_object(object_id))["markdown"].strip() == "v1 original"
        await client.update_object(object_id, {"markdown": "v2 via markdown"})
        assert (await client.get_object(object_id))["markdown"].strip() == "v2 via markdown"
        async for obj in client.list_objects():
            assert not obj.get("markdown")
    finally:
        await client.aclose()


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
