"""The GraphRepository contract, run against a live Anytype space.

This is the "third subclass behind ANYTYPE_E2E=1" that
``tests/contract/test_graph_repository_contract.py`` calls for. It reuses
the exact same behavioral spec; the ``repo`` fixture (live, bootstrapped
space) is provided by ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import pytest

from graph_context.domain import schema
from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.anytype import mapping
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

    async def test_connections_footer_round_trips_live(self, repo):
        """ADR 013 against the real server: link writes render the footer
        (deep links + heading survive store normalization), fetch_body
        strips it, and removing the last outgoing edge removes it."""
        from graph_context.infrastructure.anytype.mapping import CONNECTIONS_HEADING

        place = await repo.create_node(
            NodeDraft("Location", name="Footer Keep", summary="s")
        )
        mira = await repo.create_node(
            NodeDraft("Character", name="Footer Pin", summary="s",
                      body="Body text stays intact."),
            links=[LinkSpec("located_at", other=place.id)],
        )
        raw = (await repo._client.get_object(mira.id)).get("markdown", "")
        assert CONNECTIONS_HEADING in raw
        assert f"anytype://object?objectId={place.id}" in raw
        assert (await repo.fetch_body(mira.id)).strip() == "Body text stays intact."
        edge = next(iter(repo.graph.edges(mira.id)))
        await repo.remove_link(edge)
        raw = (await repo._client.get_object(mira.id)).get("markdown", "")
        assert CONNECTIONS_HEADING not in raw
        assert (await repo.fetch_body(mira.id)).strip() == "Body text stays intact."

    async def test_native_select_field_round_trips_live(self, repo):
        """ADR 012 against the real server: a `fields` key matching a select
        property resolves-or-creates the tag, writes the property, and
        reflects back as the option's display name -- while system
        timestamps stay filtered out of fields."""
        client = repo._client  # E2E-only reach-in; the port has no property API
        if repo.registry.field_property("E2E Mood") is None:
            # NOTE: the live server slugifies the requested key its own way
            # (e2e_mood came back as e_2_e_mood), so the test addresses the
            # property by DISPLAY NAME throughout -- which is also the
            # friendlier path to exercise live.
            await client.create_property(
                {"key": "e2e_mood", "name": "E2E Mood", "format": "select"}
            )
            await repo.resync()  # refresh the registry snapshot
        key = repo.registry.field_property("E2E Mood").key
        node = await repo.create_node(
            NodeDraft("Character", name="Field Pin", summary="s",
                      fields={"E2E Mood": "Wistful", "extra": "blob-bound"}),
        )
        assert node.fields[key] == "Wistful"          # tag auto-created
        assert node.fields["extra"] == "blob-bound"   # residual -> blob
        assert "created_date" not in node.fields      # noise filter, live
        updated = await repo.update_node(node.id, fields={"E2E Mood": "wistful"})
        assert updated.fields[key] == "Wistful"       # option REUSED by name

    async def test_template_applied_on_create_live(self, repo):
        """Templates can't be minted via the API, so this exercises whatever
        UI-authored template GC-E2E happens to own and skips otherwise. It
        certifies the repository applies the type's first template on create:
        the template's scaffold body appears, and any reflectable default
        property value the template carries lands on the new node."""
        client = repo._client  # E2E-only reach-in; the port has no templates API
        target = None
        for type_key in repo.registry.types_by_key:
            type_id = repo.registry.type_id_for(type_key)
            if not type_id or repo.role_for(type_key) in schema.INFRA_ROLES:
                continue
            templates = [t async for t in client.list_templates(type_id)]
            if templates:
                target = (type_key, templates[0]["id"])
                break
        if target is None:
            pytest.skip("GC-E2E has no UI-authored template to exercise")
        type_key, template_id = target
        tpl = await client.get_object(template_id)
        tpl_body = mapping.body_of(tpl).strip()

        node = await repo.create_node(
            NodeDraft(repo.registry.type_name(type_key), name="Template Pin", summary="s")
        )
        try:
            if tpl_body:  # the repo applied the template's scaffold body
                assert tpl_body[:24] in await repo.fetch_body(node.id)
            # Every reflectable default the template carries must have landed.
            for entry in tpl.get("properties", []):
                key, fmt = entry.get("key", ""), entry.get("format", "")
                if not repo.registry.reflects_field(key, fmt):
                    continue
                raw = entry.get(mapping._VALUE_FIELD.get(fmt, ""))
                if fmt == "checkbox" and not raw:
                    continue
                expected = mapping.field_value(fmt, raw)
                if expected:
                    assert node.fields.get(key) == expected
        finally:
            await client.archive_object(node.id)

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
