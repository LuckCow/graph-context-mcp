"""The connections footer (ADR 013): rendered for humans, invisible to the LLM.

Raw store state is asserted via ``mock.object(id)["markdown"]`` (what was
actually written); the LLM view via ``fetch_body`` (always clean).
"""

from __future__ import annotations

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.anytype.mapping import CONNECTIONS_HEADING
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository

MIRA = NodeDraft("Character", name="Mira", summary="Engineer.",
                 body="Reads stone like script.")


async def test_create_with_links_renders_footer_and_fetch_body_is_clean(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    place = await repo.create_node(NodeDraft("Location", name="The Undercroft",
                                             summary="Vaults."))
    mira = await repo.create_node(
        MIRA, links=[LinkSpec("located_at", other=place.id)],
    )
    raw = mock.object(mira.id)["markdown"]
    assert CONNECTIONS_HEADING in raw
    assert f"- located_at → [The Undercroft](anytype://object?objectId={place.id}" in raw
    assert f"&spaceId={mock.space_id})" in raw
    assert raw.startswith("Reads stone like script.")  # server owns only the footer
    assert "Engineer." not in raw  # A8: the summary prefix never round-trips
    assert await repo.fetch_body(mira.id) == "Reads stone like script."


async def test_add_and_remove_link_maintain_the_footer(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    mira = await repo.create_node(MIRA)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    edge = await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
    assert "- knows → [Orla]" in mock.object(mira.id)["markdown"]
    await repo.remove_link(edge)
    raw = mock.object(mira.id)["markdown"]
    # Last outgoing edge gone -> the whole footer disappears.
    assert CONNECTIONS_HEADING not in raw
    assert raw.strip() == "Reads stone like script."


async def test_footer_lists_only_outgoing_edges(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    mira = await repo.create_node(MIRA)
    event = await repo.create_node(
        NodeDraft("Event", name="Siege", summary="s", story_time=10,
                  body="The walls held for a year."),
        links=[LinkSpec("participated_in", other=mira.id, outgoing=False)],
    )
    # The edge runs Mira -> Siege: footer on Mira (the source), not the Siege.
    assert "- participated_in → [Siege]" in mock.object(mira.id)["markdown"]
    assert CONNECTIONS_HEADING not in mock.object(event.id)["markdown"]
    # Both bodies stay clean for the LLM.
    assert await repo.fetch_body(mira.id) == "Reads stone like script."
    assert await repo.fetch_body(event.id) == "The walls held for a year."


async def test_update_description_re_renders_around_new_text(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    mira = await repo.create_node(MIRA)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
    await repo.update_node(mira.id, body="Leads the survivors now.")
    raw = mock.object(mira.id)["markdown"]
    assert raw.startswith("Leads the survivors now.")
    assert "- knows → [Orla]" in raw
    assert await repo.fetch_body(mira.id) == "Leads the survivors now."


async def test_human_body_edit_above_the_footer_survives_link_writes(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """The server owns ONLY below the delimiter (ADR 013)."""
    mira = await repo.create_node(MIRA)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
    # Human rewrites the description above the footer, in the Anytype editor.
    footer = mock.object(mira.id)["markdown"].split("\n", 1)[1]
    mock.edit_object_directly(
        mira.id, markdown=f"Her hands remember every wall.\n{footer}"
    )
    place = await repo.create_node(NodeDraft("Location", name="Keep", summary="s"))
    await repo.add_link(mira.id, LinkSpec("located_at", other=place.id))
    raw = mock.object(mira.id)["markdown"]
    assert raw.startswith("Her hands remember every wall.")
    assert "- knows → [Orla]" in raw and "- located_at → [Keep]" in raw
    assert await repo.fetch_body(mira.id) == "Her hands remember every wall."


async def test_prose_nodes_never_get_a_footer(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """Infra-role bodies are write-once by policy; references edges are
    outgoing but must not trigger a body rewrite."""
    place = await repo.create_node(NodeDraft("Location", name="Keep", summary="s"))
    prose = await repo.create_node(
        NodeDraft("gc_prose", name="Scene", summary="s", body="Ash drifted."),
        links=[LinkSpec("references", other=place.id)],
    )
    assert CONNECTIONS_HEADING not in mock.object(prose.id)["markdown"]
    assert await repo.fetch_body(prose.id) == "Ash drifted."


SCAFFOLD = "## Details\n---"


async def test_scaffolded_template_type_never_gets_a_footer(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """A template body ("property header") would be destroyed by any
    markdown write -- A7 wholesale-replaces blocks and A9 flattens the
    scaffold's first-line heading -- so types whose template carries a body
    are footer-suppressed. Regression: the template-clobber bug
    (2026-07-11, "Buy paint that matches stairs")."""
    mock.seed_template("character", body=SCAFFOLD)
    place = await repo.create_node(NodeDraft("Location", name="Keep", summary="s"))
    mira = await repo.create_node(
        MIRA, links=[LinkSpec("located_at", other=place.id)],
    )
    raw = mock.object(mira.id)["markdown"]
    assert CONNECTIONS_HEADING not in raw
    assert raw.startswith("## Details")  # scaffold intact, heading unflattened
    # Only the body write is suppressed; the relation itself still landed.
    assert [e.target for e in repo.graph.edges(mira.id)] == [place.id]


async def test_add_link_on_scaffolded_type_leaves_the_body_untouched(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    mock.seed_template("character", body=SCAFFOLD)
    mira = await repo.create_node(MIRA)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    before = mock.object(mira.id)["markdown"]
    await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
    assert mock.object(mira.id)["markdown"] == before


async def test_incoming_link_leaves_scaffolded_source_body_untouched(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """The create-with-incoming-link path rewrites the SOURCE node's body;
    a scaffolded source must be left alone too."""
    mock.seed_template("character", body=SCAFFOLD)
    mira = await repo.create_node(MIRA)
    before = mock.object(mira.id)["markdown"]
    await repo.create_node(
        NodeDraft("Event", name="Siege", summary="s", story_time=10),
        links=[LinkSpec("participated_in", other=mira.id, outgoing=False)],
    )
    assert mock.object(mira.id)["markdown"] == before


async def test_template_without_body_scaffold_keeps_the_footer(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """Property-only templates (defaults, no body) stay footer-eligible."""
    mock.seed_template("character", body="")
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    mira = await repo.create_node(MIRA, links=[LinkSpec("knows", other=orla.id)])
    assert CONNECTIONS_HEADING in mock.object(mira.id)["markdown"]


async def test_update_body_on_scaffolded_type_omits_the_footer(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """An explicit body write replaces the scaffold by intent, but no footer
    is appended -- link writes would not maintain it on this type, and a
    stale footer is worse than none."""
    mock.seed_template("character", body=SCAFFOLD)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    mira = await repo.create_node(MIRA, links=[LinkSpec("knows", other=orla.id)])
    await repo.update_node(mira.id, body="Leads the survivors now.")
    assert mock.object(mira.id)["markdown"] == "Leads the survivors now."


async def test_unchanged_footer_is_not_rewritten(
    repo: AnytypeGraphRepository, mock: MockAnytype
) -> None:
    """Every skipped rewrite is a mention-pill spared (WP10c caveat): a
    relation write whose footer content is already correct must not carry
    a markdown payload. Exercised via a DIFFERENT relation on the same
    node whose footer line set ends up unchanged... simplest observable:
    update_node without body leaves markdown untouched byte-for-byte."""
    mira = await repo.create_node(MIRA)
    orla = await repo.create_node(NodeDraft("Character", name="Orla", summary="Ally."))
    await repo.add_link(mira.id, LinkSpec("knows", other=orla.id))
    before = mock.object(mira.id)["markdown"]
    await repo.update_node(mira.id, summary="Fresh summary.")
    assert mock.object(mira.id)["markdown"] == before
