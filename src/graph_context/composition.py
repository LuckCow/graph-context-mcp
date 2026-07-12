"""The shared service builder: one wiring, two composition roots (ADR 007).

Both the MCP server (``interface/server.py``) and the orchestrator's CLI
(``orchestrator/cli.py``) need the identical build:

    config -> client -> bootstrap -> repository -> hydrate -> session
    (restored via SessionStore) -> services

so it lives here exactly once. This module and the composition roots that
call it are the ONLY places allowed to import ``infrastructure`` (the
import-linter contracts name them); everything below stays wired to ports.

Environment surface (unchanged from the server's original wiring):

* ``GC_BACKEND``           -- ``anytype`` (default) or ``memory``.
* ``GC_PROJECT_NAME``      -- initial project label (cosmetic).
* ``GC_FIELD_DENYLIST``    -- ADR 012 field-reflection silences.
* ``GC_EMBEDDER``          -- ``off`` (default), ``hash`` (deterministic,
                              model-free), or ``local`` (the baked
                              sentence-transformers model, ADR 014).
* ``GC_EMBEDDER_MODEL``    -- model-name override for ``local`` (defaults
                              to the model the image bakes).
* ``GC_SEMANTIC_CACHE``    -- embedding-cache directory (default
                              ``~/.cache/graph-context``); files are
                              disposable projections.
* ``GC_TIMEZONE``          -- IANA zone (e.g. ``America/Chicago``) the
                              scheduled-event clock runs in (ADR 027).
                              Empty = the system clock; containers
                              usually sit at UTC, so set this (or TZ)
                              to make "9am" mean the user's 9am.

(``GC_STORE_LLM_INPUT`` moved to the orchestrator's root: WP7 retired
record_prose's llm_* parameters, so the knob now governs intent-node
prompt storage instead.)
* Anytype connection env is read by ``AnytypeConfig.from_env``.

The active :class:`~graph_context.interface.profiles.DomainProfile` is a
parameter, not an env read: each root resolves ``GC_PROFILE`` itself (the
MCP server must do so at import time, when tool docstrings register).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from graph_context.application.mutation_journal import MutationJournal
from graph_context.application.ranker import Ranker
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.application.session_registry import SessionRegistry
from graph_context.errors import GraphContextError
from graph_context.interface.profiles import DomainProfile
from graph_context.interface.services import (
    Services,
    build_services,
    derive_services,
)
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.mode_store import ModeStore
from graph_context.ports.semantic import Embedder, SemanticIndex

logger = logging.getLogger(__name__)

TeardownHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BuiltRuntime:
    """One process's wired backend: services, config stores, shutdown.

    ``services_for`` is the WP8 session seam: every live session is
    addressed by an explicit transport-scoped key and gets its own
    Services view (per-key SessionState + persister over the shared
    repository). ``services`` is either the ``session_key`` session's own
    view (MCP server) or an inert donor bundle (orchestrator paths) --
    see :func:`build_runtime`. ``session_labels`` is a shared mutable map
    of session key -> human label; transports fill it so a session's
    Anytype node gets a legible name.
    """

    services: Services
    mode_store: ModeStore
    teardown: list[TeardownHook]
    services_for: Callable[[str], Awaitable[Services]]
    session_labels: dict[str, str]


async def build_runtime(
    profile: DomainProfile,
    *,
    journal: MutationJournal | None = None,
    space_id: str | None = None,
    project: str | None = None,
    session_key: str | None = None,
) -> BuiltRuntime:
    """Build the full service bundle for one runtime.

    The returned teardown hooks run in reverse order on shutdown (session
    flushes before client close). The mode store reads the space's Activity
    Mode config objects (ADR 015 amendment); the memory backend's is empty,
    so profile defaults apply.

    ``space_id``/``project`` override ``ANYTYPE_SPACE_ID``/``GC_PROJECT_NAME``
    so one process can host several runtimes bound to different spaces
    (channel-bound spaces, ADR 017); left ``None``, the env applies and a
    process gets exactly one runtime, as before.

    ``session_key`` decides what ``BuiltRuntime.services`` is bound to
    (WP8): a key (the MCP server passes ``"mcp"``) binds the primary
    bundle to that registry session -- loaded, persisted, flushed at
    teardown. ``None`` (orchestrator paths) returns a DONOR bundle: a
    fresh, never-persisted session that exists only as the shared-
    component source for ``services_for`` derivations; everything real
    flows through ``services_for(key)``.
    """
    from graph_context.domain.session import SessionState

    backend = os.environ.get("GC_BACKEND", "anytype")
    default_project = project or os.environ.get("GC_PROJECT_NAME")
    teardown: list[TeardownHook] = []
    session_labels: dict[str, str] = {}

    logger.info("profile=%s (%s)", profile.name, profile.description)
    if backend == "memory":
        from graph_context.infrastructure.memory.fake_mode_store import (
            InMemoryModeStore,
        )
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )
        from graph_context.infrastructure.memory.fake_session_store import (
            InMemorySessionStore,
        )

        logger.info("backend=memory (development mode; nothing persists)")
        memory_repo = InMemoryGraphRepository(role_overrides=profile.role_overrides)
        projector, ranker = await _build_semantic(
            memory_repo, profile, space_id=None
        )
        registry = SessionRegistry(
            InMemorySessionStore(), default_project=default_project
        )
        base = build_services(
            memory_repo,
            SessionState(project=default_project),  # donor unless keyed below
            journal=journal,
            projector=projector,
            ranker=ranker,
            timezone=os.environ.get("GC_TIMEZONE", ""),
        )
        base = await _bind_primary(base, registry, session_key)
        return BuiltRuntime(
            services=base,
            mode_store=InMemoryModeStore(),
            teardown=teardown,
            services_for=_session_seam(base, registry),
            session_labels=session_labels,
        )

    from graph_context.infrastructure.anytype.client import AnytypeClient
    from graph_context.infrastructure.anytype.config import AnytypeConfig
    from graph_context.infrastructure.anytype.mode_store import AnytypeModeStore
    from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
    from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
    from graph_context.infrastructure.anytype.session_repository import (
        AnytypeSessionStore,
    )
    from graph_context.infrastructure.anytype.view_catalog import AnytypeViewCatalog

    config = AnytypeConfig.from_env(space_id)
    client = AnytypeClient(config)
    teardown.append(client.aclose)
    timeline = (profile.time_property, profile.time_format)  # ADR 015
    await ensure_schema(client, timeline=timeline)
    # GC_FIELD_DENYLIST (ADR 012): comma-separated property keys to hide
    # from field reflection, on top of the built-in system-noise denylist.
    field_denylist = frozenset(
        key.strip()
        for key in os.environ.get("GC_FIELD_DENYLIST", "").split(",")
        if key.strip()
    )
    repository = AnytypeGraphRepository(
        client,
        role_overrides=profile.role_overrides,
        field_denylist=field_denylist,
        timeline=timeline,
    )
    await repository.hydrate()
    logger.info(
        "hydrated space %s: %d nodes / %d edges",
        config.space_id,
        repository.graph.node_count(),
        repository.graph.edge_count(),
    )
    if not default_project:
        # Derived cosmetic default: the space's own name. Never blocks startup;
        # GC_PROJECT_NAME and a session's persisted set_project take precedence.
        try:
            default_project = (await client.get_space()).get("name") or None
        except GraphContextError:
            logger.warning(
                "could not read space name for the project label", exc_info=True
            )
    # Sessions are keyed (WP8, ADR 021): each key owns a SessionContext
    # meta-node; the registry lazily restores each on first use and the
    # teardown flush persists every session this process touched.
    store = AnytypeSessionStore(client, labels=session_labels)
    registry = SessionRegistry(store, default_project=default_project)
    teardown.append(registry.flush_all)  # LIFO: flushes before client aclose
    projector, ranker = await _build_semantic(
        repository, profile, space_id=config.space_id
    )
    base = build_services(
        repository,
        SessionState(project=default_project),  # donor unless keyed below
        journal=journal,
        projector=projector,
        ranker=ranker,
        # WP13 view param (ADR 018): saved Set views, compiled to
        # NodeQuery per call so desktop edits apply immediately.
        views=AnytypeViewCatalog(client),
        timezone=os.environ.get("GC_TIMEZONE", ""),
    )
    base = await _bind_primary(base, registry, session_key)
    return BuiltRuntime(
        services=base,
        mode_store=AnytypeModeStore(client),
        teardown=teardown,
        services_for=_session_seam(base, registry),
        session_labels=session_labels,
    )


async def _bind_primary(
    base: Services, registry: SessionRegistry, session_key: str | None
) -> Services:
    """Bind the primary bundle to its registry session, or leave the donor.

    A key (MCP server: ``"mcp"``) makes ``BuiltRuntime.services`` a real,
    persisted session view; ``None`` keeps the never-persisted donor whose
    only jobs are sharing components with ``services_for`` derivations and
    the orchestrator's no-factory test fallback.
    """
    if session_key is None:
        return base
    session, persister = await registry.get(session_key)
    return derive_services(base, session, persister, session_key=session_key)


def _session_seam(
    base: Services, registry: SessionRegistry
) -> Callable[[str], Awaitable[Services]]:
    async def services_for(key: str) -> Services:
        session, persister = await registry.get(key)
        return derive_services(base, session, persister, session_key=key)

    return services_for


def _make_embedder(choice: str) -> Embedder | None:
    """GC_EMBEDDER resolution; unknown values fail loudly at startup."""
    if choice in {"", "off", "0", "none"}:
        return None
    if choice == "hash":
        from graph_context.infrastructure.semantic.hashing_embedder import (
            HashingEmbedder,
        )

        return HashingEmbedder()
    if choice == "local":
        from graph_context.infrastructure.semantic.local_embedder import (
            SentenceTransformerEmbedder,
        )

        model_name = os.environ.get("GC_EMBEDDER_MODEL", "").strip()
        if model_name:
            return SentenceTransformerEmbedder(model_name)
        return SentenceTransformerEmbedder()
    raise ValueError(f"unknown GC_EMBEDDER {choice!r}; allowed: off, hash, local")


async def _build_semantic(
    repository: GraphRepository, profile: DomainProfile, *, space_id: str | None
) -> tuple[SemanticProjector | None, Ranker | None]:
    """The semantic projection + Ranker (ADRs 014/016), or Nones when
    GC_EMBEDDER=off.

    Anytype spaces get the persistent SQLite cache (one file per space +
    model); the memory backend keeps the cache in memory too -- nothing
    about it should outlive a store that itself evaporates.
    """
    embedder = _make_embedder(os.environ.get("GC_EMBEDDER", "off").strip().lower())
    if embedder is None:
        return None, None
    index: SemanticIndex
    if space_id is None:
        from graph_context.infrastructure.semantic.memory_index import (
            InMemorySemanticIndex,
        )

        index = InMemorySemanticIndex()
    else:
        from pathlib import Path

        from graph_context.infrastructure.semantic.sqlite_index import (
            SqliteSemanticIndex,
        )

        cache_dir = Path(os.environ.get(
            "GC_SEMANTIC_CACHE", str(Path.home() / ".cache" / "graph-context")
        ))
        index = SqliteSemanticIndex(
            cache_dir / f"semantic-{space_id}.sqlite", model=embedder.model_id
        )
    projector = SemanticProjector(repository, embedder, index)
    embedded = await projector.refresh()  # full pass: seed + prune
    logger.info("semantic projection ready (%d embedded at startup)", embedded)
    return projector, Ranker(repository, embedder, index, profile.ranking)


async def run_teardown(teardown: list[TeardownHook]) -> None:
    """Run hooks in reverse registration order, never letting one abort the rest."""
    for hook in reversed(teardown):
        try:
            await hook()
        except Exception:
            logger.exception("teardown hook failed")
