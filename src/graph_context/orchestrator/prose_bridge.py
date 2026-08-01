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
import re
import threading
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, cast

from graph_context.application.node_historian import (
    MarkRequest,
    NodeHistorian,
)
from graph_context.domain import revisions
from graph_context.errors import GraphContextError, StaleSectionMark
from graph_context.ports.graph_repository import GraphRepository

CALL_TIMEOUT_SECONDS = 15.0

MARK_AUTHOR = "human:prose-page"

# The author_detail user leg as transports record it (`anytype:<object
# id>`); display resolves the id to the member object's name.
_ANYTYPE_ID = re.compile(r"anytype:(\S+)")


@dataclass(frozen=True, slots=True)
class ProseSpace:
    """One registered space: its loop handle plus the callables the
    server routes speak. The callables close over the space's historian,
    repository, and route lock -- the server never sees those."""

    space_id: str
    label: str
    loop: asyncio.AbstractEventLoop
    list_nodes: Callable[[], Awaitable[list[dict[str, Any]]]]
    revision_diff: Callable[[str, int], Awaitable[dict[str, Any]]]
    # WP48 (ADR 054): the document-level editor wire.
    # (node) -> full raw body + absolute-offset segments/spans
    doc_view: Callable[[str], Awaitable[dict[str, Any]]]
    # (node, base, body) -- whole-document save; base is the doc
    # payload's concurrency token, mismatch -> 409
    save_body: Callable[[str, str, str], Awaitable[dict[str, Any]]]
    # (node, base, marks) -- batch status/intent marks, one sidecar write
    set_marks: Callable[
        [str, str, list[dict[str, Any]]], Awaitable[dict[str, Any]]
    ]


class ProseBridge:
    """Thread-safe registry of live spaces + the one cross-thread call.

    Also the live-update ledger (WP48): every historian sidecar write
    bumps a per-(space, node) counter via :meth:`bump` (called on the
    space's loop through the historian's ``on_record`` hook), and the
    server's ``/api/prose/events`` SSE thread polls
    :meth:`versions_for` -- an open page learns of bot edits within one
    poll tick without any cross-thread coroutine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spaces: dict[str, ProseSpace] = {}
        self._versions: dict[tuple[str, str], int] = {}

    def register(self, space: ProseSpace) -> None:
        with self._lock:
            self._spaces[space.space_id] = space

    def get(self, space_id: str) -> ProseSpace | None:
        with self._lock:
            return self._spaces.get(space_id)

    def spaces(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(s.space_id, s.label) for s in self._spaces.values()]

    def bump(self, space_id: str, node_id: str) -> None:
        with self._lock:
            key = (space_id, node_id)
            self._versions[key] = self._versions.get(key, 0) + 1

    def versions_for(self, space_id: str) -> dict[str, int]:
        with self._lock:
            return {
                node: version
                for (sid, node), version in self._versions.items()
                if sid == space_id
            }

    def version_of(self, space_id: str, node_id: str) -> int:
        with self._lock:
            return self._versions.get((space_id, node_id), 0)

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
    """Build a space's callables and register it. Must run ON the
    space's serving loop -- the captured ``get_running_loop`` is what the
    HTTP thread schedules onto. Also wires the historian's ``on_record``
    hook to the bridge's version ledger (the SSE live-update signal)."""
    loop = asyncio.get_running_loop()

    def _name_of(node_id: str) -> str:
        graph = repository.graph
        return graph.node(node_id).name if graph.has_node(node_id) else node_id

    def _display_detail(detail: str) -> str:
        """An author_detail string for DISPLAY: any ``anytype:<id>``
        user leg resolves to that object's name when the graph knows it
        (space members hydrate as first-class nodes, quirk A10/S11) --
        pre-WP48 revisions recorded the raw participant id, and the
        stored log is history, so the readable name is a read-time
        derivation like blame itself. Unknown ids stay verbatim."""
        def _swap(match: re.Match[str]) -> str:
            name = _name_of(match.group(1))
            return name if name != match.group(1) else match.group(0)
        return _ANYTYPE_ID.sub(_swap, detail)

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
                "last_author": (
                    _display_detail(last.author_detail) if last else ""
                ),
            })
        rows.sort(key=lambda r: str(r["last_at"]), reverse=True)
        return rows

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
                "detail": _display_detail(record.author_detail),
                "pairs": pairs,
            }
        raise GraphContextError(f"no revision {seq} of {node_id!r}")

    # -- the document-level wire (WP48, ADR 054) -----------------------

    def _body_token(body: str) -> str:
        """The doc view's concurrency token: a digest of the body's
        hash SEQUENCE, so a save 409s exactly when the document's
        identity-bearing content changed since the page loaded (a
        whitespace-only store reflow does not invalidate the page)."""
        return revisions.block_hash(
            "\n".join(h for h, _ in revisions.hash_sequence(body))
        )

    async def _doc_payload(node_id: str) -> dict[str, Any]:
        body = await repository.fetch_body(node_id)
        usable = _usable(node_id)
        blame = historian.blame(node_id)
        badges = historian.section_states(node_id)
        tokens_state = historian.token_states(node_id)
        authors = revisions.word_token_authors(usable)
        segments: list[dict[str, Any]] = []
        spans: list[list[Any]] = []
        for block_hash, start, end in revisions.block_offsets(body):
            text = body[start:end]
            badge = badges.get(block_hash)
            entry = blame.get(block_hash)
            segments.append({
                "hash": block_hash,
                "start": start,
                "end": end,
                "status": badge.status if badge else revisions.STATUS_RAW_AI,
                "intent": (
                    badge.intent if badge else revisions.INTENT_FLEXIBLE
                ),
                # None = not recorded yet (an edit between change
                # ticks) or a word-free separator -- neutral display.
                "blame": {
                    "author": entry.author_kind,
                    "detail": _display_detail(entry.author_detail),
                    "at": entry.at,
                    "seq": entry.seq,
                } if entry else None,
            })
            block_authors = authors.get(block_hash)
            state = tokens_state.get(block_hash)
            tokens = revisions.block_tokens(text)
            if (
                block_authors is None
                or not revisions.has_words(text)
                or len(block_authors) != len(tokens)
            ):
                continue  # neutral: no spans over this block
            statuses: Sequence[str] = (
                state.status if state and len(state.status) == len(tokens)
                else (revisions.STATUS_RAW_AI,) * len(tokens)
            )
            intents: Sequence[str] = (
                state.intent if state and len(state.intent) == len(tokens)
                else (revisions.INTENT_FLEXIBLE,) * len(tokens)
            )
            cursor = start
            for author, status, intent, token in zip(
                block_authors, statuses, intents, tokens, strict=True
            ):
                token_end = cursor + len(token)
                if (
                    spans
                    and spans[-1][1] == cursor
                    and spans[-1][2:] == [author, status, intent]
                ):
                    spans[-1][1] = token_end
                else:
                    spans.append(
                        [cursor, token_end, author, status, intent]
                    )
                cursor = token_end
        timeline = []
        previous: tuple[str, ...] = ()
        for record, hashes in revisions.state_walk(usable):
            timeline.append({
                "seq": record.seq,
                "at": record.at,
                "author": record.author_kind,
                "detail": _display_detail(record.author_detail),
                "added": len(set(hashes) - set(previous)),
                "removed": len(set(previous) - set(hashes)),
            })
            previous = hashes
        return {
            "id": node_id,
            "name": _name_of(node_id),
            "version": bridge.version_of(space_id, node_id),
            "base": _body_token(body),
            "body": body,
            "segments": segments,
            "spans": spans,
            "revisions": timeline,
        }

    async def doc_view(node_id: str) -> dict[str, Any]:
        if node_id not in historian.tracked_ids():
            raise GraphContextError(f"no tracked node {node_id!r}")
        return await _doc_payload(node_id)

    async def save_body(node_id: str, base: str, body: str) -> dict[str, Any]:
        """Whole-document save (WP48): the page IS the editor, so the
        save carries the full body. Base-token mismatch -> 409 (the
        node changed under the page; the client reconciles). Writes
        through the repository like a human's Anytype-UI edit --
        deliberately NOT through NodeWriter: the locked guard binds the
        MODEL; the human who locks text is the authority to change it.
        Records immediately (rides WP44 roll-up, so an autosave storm
        coalesces into one pending human revision)."""
        if node_id not in historian.tracked_ids():
            raise GraphContextError(f"no tracked node {node_id!r}")
        async with route_lock:
            current = await repository.fetch_body(node_id)
            if _body_token(current) != base:
                raise StaleSectionMark(
                    f"{_name_of(node_id)!r} changed since this view "
                    "loaded; reload and merge your edits."
                )
            await repository.update_node(node_id, body=body)
            await historian.record_external_revision(
                node_id, detail=MARK_AUTHOR
            )
        return await _doc_payload(node_id)

    async def set_marks(
        node_id: str, base: str, marks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Batch status/intent marks (WP48): one POST per selection
        gesture, one route-lock hold, one sidecar rewrite. Optional
        ``start_char``/``end_char`` are offsets over the block's raw
        text; the domain's ``char_range_to_tokens`` is the only place
        they become token indices."""
        async with route_lock:
            current = await repository.fetch_body(node_id)
            if _body_token(current) != base:
                raise StaleSectionMark(
                    f"{_name_of(node_id)!r} changed since this view "
                    "loaded; reload and re-apply the marks."
                )
            by_hash = {
                h: current[s:e]
                for h, s, e in revisions.block_offsets(current)
            }
            requests = []
            for mark in marks:
                block_hash = str(mark["hash"])
                start = end = None
                if mark.get("start_char") is not None:
                    text = by_hash.get(block_hash)
                    if text is None:
                        raise StaleSectionMark(
                            f"section {block_hash} is not in the current "
                            f"version of {_name_of(node_id)!r}; reload."
                        )
                    start, end = revisions.char_range_to_tokens(
                        text,
                        int(mark["start_char"]),
                        int(mark["end_char"]),
                    )
                requests.append(MarkRequest(
                    kind=str(mark["kind"]),
                    block_hash=block_hash,
                    value=str(mark["value"]),
                    start=start,
                    end=end,
                ))
            await historian.record_marks(
                node_id, requests=requests, by=MARK_AUTHOR
            )
        return await _doc_payload(node_id)

    # WP48: every historian write (turn boundary, change tick, page
    # save, marks) bumps the live-update ledger the SSE route polls.
    historian.on_record = lambda node_id: bridge.bump(space_id, node_id)

    bridge.register(ProseSpace(
        space_id=space_id,
        label=label,
        loop=loop,
        list_nodes=list_nodes,
        revision_diff=revision_diff,
        doc_view=doc_view,
        save_body=save_body,
        set_marks=set_marks,
    ))
