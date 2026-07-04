"""Ranker behavior (ADR 016): the graph recruits, conditions, and explains."""

from __future__ import annotations

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationRecord
from graph_context.application.ranker import Ranker, RankingWeights
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.infrastructure.semantic.hashing_embedder import HashingEmbedder
from graph_context.infrastructure.semantic.memory_index import InMemorySemanticIndex


async def _ranker(
    repository: InMemoryGraphRepository,
    weights: RankingWeights | None = None,
) -> Ranker:
    embedder = HashingEmbedder()
    index = InMemorySemanticIndex()
    await SemanticProjector(repository, embedder, index).refresh()
    return Ranker(repository, embedder, index, weights)


async def test_semantic_seed_ranks_by_description() -> None:
    repository = InMemoryGraphRepository()
    await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer of Brakk.",
    ))
    await repository.create_node(NodeDraft(
        "Character", name="Renata", summary="Senior product executive.",
    ))
    ranker = await _ranker(repository)
    hits = await ranker.rank("the siege engineer")
    assert hits and hits[0].node.name == "Mira"
    assert any("matched the description" in e for e in hits[0].evidence)


async def test_graph_recruits_a_vocabulary_invisible_node() -> None:
    """The ADR 016 motivating case: the item's text shares nothing with the
    query, but it is linked to two strong seeds -- the graph nominates it."""
    repository = InMemoryGraphRepository()
    mira = await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer of Brakk.",
    ))
    siege = await repository.create_node(NodeDraft(
        "Event", name="Siege of Brakk", story_time=10,
        summary="The year-long siege where the engineer broke the walls.",
    ))
    ashbrand = await repository.create_node(
        NodeDraft("Item", name="Ashbrand", summary="A blade quenched in ash."),
        links=[
            LinkSpec("wielded_by", other=mira.id),
            LinkSpec("used_in", other=siege.id),
        ],
    )
    ranker = await _ranker(repository)
    hits = await ranker.rank("the siege engineer who broke the walls", limit=5)
    names = [h.node.name for h in hits]
    assert "Ashbrand" in names  # recruited: zero query-vocabulary overlap
    recruit = next(h for h in hits if h.node.name == "Ashbrand")
    assert not any("matched the description" in e for e in recruit.evidence)
    assert any("linked to" in e for e in recruit.evidence)
    assert ashbrand.id == recruit.node.id


async def test_no_seeds_means_no_hits_fail_closed() -> None:
    repository = InMemoryGraphRepository()
    await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer.",
    ))
    ranker = await _ranker(repository)
    assert await ranker.rank("quarterly financial derivatives") == []


async def test_hub_connections_conduct_less() -> None:
    """Adamic-Adar in action: the same link through a hub scores lower."""
    repository = InMemoryGraphRepository()
    seed = await repository.create_node(NodeDraft(
        "Event", name="Siege of Brakk", story_time=10,
        summary="The siege of the city walls.",
    ))
    quiet = await repository.create_node(
        NodeDraft("Item", name="Quietblade", summary="zzz"),
        links=[LinkSpec("used_in", other=seed.id)],
    )
    hub = await repository.create_node(
        NodeDraft("Item", name="Hubblade", summary="zzz"),
        links=[LinkSpec("used_in", other=seed.id)],
    )
    for i in range(12):  # make Hubblade a hub
        await repository.create_node(
            NodeDraft("Character", name=f"owner{i}", summary="zzz"),
            links=[LinkSpec("owned", other=hub.id)],
        )
    ranker = await _ranker(repository, RankingWeights(final_threshold=0.0))
    hits = {h.node.id: h.score for h in await ranker.rank("the siege", limit=20)}
    assert hits[quiet.id] > hits[hub.id]


async def test_links_mirror_conducts_less_than_named_relations() -> None:
    repository = InMemoryGraphRepository()
    seed = await repository.create_node(NodeDraft(
        "Event", name="Siege of Brakk", story_time=10,
        summary="The siege of the city walls.",
    ))
    named = await repository.create_node(
        NodeDraft("Item", name="Namedblade", summary="zzz"),
        links=[LinkSpec("used_in", other=seed.id)],
    )
    mirrored = await repository.create_node(
        NodeDraft("Item", name="Mirrorblade", summary="zzz"),
        links=[LinkSpec("links", other=seed.id)],
    )
    ranker = await _ranker(repository, RankingWeights(final_threshold=0.0))
    hits = {h.node.id: h.score for h in await ranker.rank("the siege", limit=20)}
    assert hits[named.id] > hits[mirrored.id]


async def test_capture_coreference_ties_candidates_together() -> None:
    repository = InMemoryGraphRepository()
    mira = await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer.",
    ))
    keep = await repository.create_node(NodeDraft(
        "Location", name="The Undercroft", summary="zzz vaults zzz",
    ))
    recorder = CaptureRecorder(repository, now=lambda: "t")
    await recorder.record(
        text="Scene text", summary="s", references=[mira.id, keep.id],
        title="The vaults fall",
    )
    ranker = await _ranker(repository)
    hits = await ranker.rank("the siege engineer", limit=5)
    undercroft = next((h for h in hits if h.node.id == keep.id), None)
    assert undercroft is not None  # recruited through the capture connector
    assert any("co-referenced with Mira" in e for e in undercroft.evidence)


async def test_intent_cotouch_ties_candidates_together() -> None:
    repository = InMemoryGraphRepository()
    mira = await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer.",
    ))
    task = await repository.create_node(NodeDraft(
        "Location", name="Gatehouse", summary="zzz",
    ))
    recorder = IntentRecorder(repository, now=lambda: "T0")
    await recorder.record_turn(
        prompt="rework the gate defenses",
        mutations=[MutationRecord(mira.id, "modified"),
                   MutationRecord(task.id, "modified")],
    )
    ranker = await _ranker(repository)
    hits = await ranker.rank("the siege engineer", limit=5)
    gatehouse = next((h for h in hits if h.node.id == task.id), None)
    assert gatehouse is not None
    assert any("worked on together with Mira" in e for e in gatehouse.evidence)


async def test_recency_weight_boosts_fresher_nodes() -> None:
    from dataclasses import replace as dc_replace

    repository = InMemoryGraphRepository()
    old = await repository.create_node(NodeDraft(
        "Character", name="Old Task", summary="deploy the build",
    ))
    new = await repository.create_node(NodeDraft(
        "Character", name="New Task", summary="deploy the build",
    ))
    # Stamp modified_at by hand (the memory backend has no store clock).
    graph = repository.graph
    graph.upsert_node(dc_replace(graph.node(old.id), modified_at="2026-01-01"))
    graph.upsert_node(dc_replace(graph.node(new.id), modified_at="2026-07-04"))
    weighted = await _ranker(repository, RankingWeights(recency=0.5))
    hits = await weighted.rank("deploy the build", limit=2)
    assert hits[0].node.id == new.id
    assert any("recently modified" in e for e in hits[0].evidence)
    unweighted = await _ranker(repository)  # fiction default: recency = 0
    scores = {h.node.id: h.score for h in await unweighted.rank("deploy the build")}
    assert scores[old.id] == scores[new.id]


async def test_edge_label_conditioning_changes_ranking() -> None:
    """The SAME candidates rank differently when the query speaks a
    relation's language -- labels are embedded with the same embedder."""
    repository = InMemoryGraphRepository()
    seed = await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="The siege engineer.",
    ))
    home = await repository.create_node(
        NodeDraft("Location", name="Homeplace", summary="zzz"),
        links=[LinkSpec("located_at", other=seed.id, outgoing=False)],
    )
    friend = await repository.create_node(
        NodeDraft("Character", name="Friendface", summary="zzz"),
        links=[LinkSpec("knows", other=seed.id, outgoing=False)],
    )
    ranker = await _ranker(repository, RankingWeights(final_threshold=0.0))
    located_query = {h.node.id: h.score for h in await ranker.rank(
        "where is the siege engineer located", limit=10)}
    knows_query = {h.node.id: h.score for h in await ranker.rank(
        "who knows the siege engineer", limit=10)}
    # The located query favors the located_at edge; the knows query flips it.
    assert located_query[home.id] > located_query[friend.id]
    assert knows_query[friend.id] > knows_query[home.id]
