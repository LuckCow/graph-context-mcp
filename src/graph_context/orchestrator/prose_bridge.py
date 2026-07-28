"""The prose review page's thread/loop seam (WP43, ADR 049).

The inspection server runs in a plain daemon thread; historian baselines
and the repository index are mutated on the bot's asyncio loop. This
module is the ONLY door between them: every read and write the prose
page makes is a coroutine scheduled onto the owning space's loop via
``asyncio.run_coroutine_threadsafe`` -- the HTTP thread never touches
shared state directly (even reads would race the loop's mutations), and
mark writes additionally take the space's route lock so they never
interleave with a turn's own recording.

Late binding by construction: ``serve`` hands ``create_server`` an EMPTY
registry before the bots bootstrap; the chat bot registers each space
once its runtime (and historian) exists. A standalone inspect server has
no bridge and renders the page's empty state.

Composition rules: this module may import application/ports/domain but
never infrastructure (import-linter); the composition roots inject the
repository and historian handles.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

from graph_context.application.node_historian import NodeHistorian
from graph_context.domain import revisions
from graph_context.errors import GraphContextError
from graph_context.ports.graph_repository import GraphRepository

CALL_TIMEOUT_SECONDS = 15.0

MARK_AUTHOR = "human:prose-page"


@dataclass(frozen=True, slots=True)
class ProseSpace:
    """One registered space: its loop handle plus the four callables the
    server routes speak. The callables close over the space's historian,
    repository, and route lock -- the server never sees those."""

    space_id: str
    label: str
    loop: asyncio.AbstractEventLoop
    list_nodes: Callable[[], Awaitable[list[dict[str, Any]]]]
    node_view: Callable[[str], Awaitable[dict[str, Any]]]
    revision_diff: Callable[[str, int], Awaitable[dict[str, Any]]]
    set_mark: Callable[[str, str, str, str], Awaitable[dict[str, Any]]]


class ProseBridge:
    """Thread-safe registry of live spaces + the one cross-thread call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spaces: dict[str, ProseSpace] = {}

    def register(self, space: ProseSpace) -> None:
        with self._lock:
            self._spaces[space.space_id] = space

    def get(self, space_id: str) -> ProseSpace | None:
        with self._lock:
            return self._spaces.get(space_id)

    def spaces(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(s.space_id, s.label) for s in self._spaces.values()]

    def call(
        self,
        space_id: str,
        name: str,
        /,
        *args: Any,
        timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> Any:
        """Run one space callable ON ITS LOOP and wait for the result.

        Raises ``KeyError`` for an unknown space, ``TimeoutError`` when
        the loop is too busy (the server maps it to 504), and re-raises
        whatever the coroutine raised (stale marks -> 409, etc.)."""
        space = self.get(space_id)
        if space is None:
            raise KeyError(space_id)
        fn: Callable[..., Awaitable[Any]] = getattr(space, name)
        coro = cast("Coroutine[Any, Any, Any]", fn(*args))
        future: Any = asyncio.run_coroutine_threadsafe(coro, space.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise


def register_space(
    bridge: ProseBridge,
    *,
    space_id: str,
    label: str,
    historian: NodeHistorian,
    repository: GraphRepository,
    route_lock: asyncio.Lock,
) -> None:
    """Build a space's four callables and register it. Must run ON the
    space's serving loop -- the captured ``get_running_loop`` is what the
    HTTP thread schedules onto."""
    loop = asyncio.get_running_loop()

    def _name_of(node_id: str) -> str:
        graph = repository.graph
        return graph.node(node_id).name if graph.has_node(node_id) else node_id

    def _usable(node_id: str) -> list[revisions.RevisionRecord]:
        return [
            r for r in historian.history(node_id)
            if r.kind != revisions.KIND_TRUNCATED
        ]

    async def list_nodes() -> list[dict[str, Any]]:
        rows = []
        for node_id in historian.tracked_ids():
            usable = _usable(node_id)
            last = usable[-1] if usable else None
            rows.append({
                "id": node_id,
                "name": _name_of(node_id),
                "revisions": len(usable),
                "last_at": last.at if last else "",
                "last_author": last.author_detail if last else "",
            })
        rows.sort(key=lambda r: str(r["last_at"]), reverse=True)
        return rows

    async def node_view(node_id: str) -> dict[str, Any]:
        if node_id not in historian.tracked_ids():
            raise GraphContextError(f"no tracked node {node_id!r}")
        body = await repository.fetch_body(node_id)
        blame = historian.blame(node_id)
        states = historian.section_states(node_id)
        blocks = []
        for block_hash, raw in revisions.body_blocks(body):
            state = states.get(block_hash)
            entry = blame.get(block_hash)
            blocks.append({
                "hash": block_hash,
                "text": raw,
                "status": state.status if state else revisions.STATUS_RAW_AI,
                "intent": state.intent if state else revisions.INTENT_FLEXIBLE,
                # None = the log has not recorded this block yet (a human
                # edit between change ticks) or it is below the blame
                # floor -- the page renders it neutrally, never errors.
                "blame": {
                    "author": entry.author_kind,
                    "detail": entry.author_detail,
                    "at": entry.at,
                    "seq": entry.seq,
                } if entry else None,
            })
        timeline = []
        previous: tuple[str, ...] = ()
        for record, hashes in revisions.state_walk(_usable(node_id)):
            timeline.append({
                "seq": record.seq,
                "at": record.at,
                "author": record.author_kind,
                "detail": record.author_detail,
                "added": len(set(hashes) - set(previous)),
                "removed": len(set(previous) - set(hashes)),
            })
            previous = hashes
        return {
            "id": node_id,
            "name": _name_of(node_id),
            "blocks": blocks,
            "revisions": timeline,
        }

    async def revision_diff(node_id: str, seq: int) -> dict[str, Any]:
        records = _usable(node_id)
        texts = revisions.texts_of(records)
        previous: tuple[str, ...] = ()
        for record, hashes in revisions.state_walk(records):
            if record.seq != seq:
                previous = hashes
                continue
            added = [h for h in hashes if h not in set(previous)]
            removed = {h for h in previous if h not in set(hashes)}
            pairs = []
            for new_hash in added:
                text = texts.get(new_hash, "")
                ancestor = revisions.closest(text, removed, texts)
                old = texts.get(ancestor, "") if ancestor else ""
                pairs.append({
                    "old_hash": ancestor,
                    "new_hash": new_hash,
                    "spans": [
                        [kind, span]
                        for kind, span in revisions.word_diff(old, text)
                    ],
                })
                removed.discard(ancestor)
            for old_hash in sorted(removed):  # deletions with no successor
                pairs.append({
                    "old_hash": old_hash,
                    "new_hash": "",
                    "spans": [["del", texts.get(old_hash, "")]],
                })
            return {
                "seq": record.seq,
                "at": record.at,
                "author": record.author_kind,
                "detail": record.author_detail,
                "pairs": pairs,
            }
        raise GraphContextError(f"no revision {seq} of {node_id!r}")

    async def set_mark(
        node_id: str, block_hash: str, kind: str, value: str
    ) -> dict[str, Any]:
        # Under the route lock: a mark append (fold -> render -> sidecar
        # write -> baseline swap) must never interleave with a turn.
        async with route_lock:
            state = await historian.record_mark(
                node_id, kind=kind, block_hash=block_hash,
                value=value, by=MARK_AUTHOR,
            )
        return {
            "hash": block_hash,
            "status": state.status,
            "intent": state.intent,
        }

    bridge.register(ProseSpace(
        space_id=space_id,
        label=label,
        loop=loop,
        list_nodes=list_nodes,
        node_view=node_view,
        revision_diff=revision_diff,
        set_mark=set_mark,
    ))
