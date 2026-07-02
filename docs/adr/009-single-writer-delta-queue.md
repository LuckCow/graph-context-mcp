# ADR 009: Single-writer write seam with store-truth PATCH materialization

**Status:** Accepted (2026-07-02) — implements the severable core of WP8's
"single-writer delta queue"; amends WP1's "read-modify-write from index
state" concurrency stance

## Context

Anytype PATCHes multi-value relation properties by **wholesale replace**
(spike S1 / mapping A4), so every link mutation is read-modify-write:
read current targets, apply the delta, PATCH the full list. The adapter
read "current targets" from the in-memory index, and nothing serialized
overlapping writes. Two consequences:

- **Bot-vs-bot lost updates.** Two concurrent turns adding links to the
  same node each read the same stale list and the second PATCH silently
  erases the first — a user command acknowledged and then evaporated. The
  multi-user direction (WP8) makes this structural, not freak timing.
- **Human-vs-bot clobbering (Q2).** A human editing the same relation in
  the Anytype UI between syncs was overwritten by an index-derived PATCH;
  Q2 accepted this as last-write-wins with loud logging and deferred
  precise detection to Phase 4.

The suite could not even express the first failure: `MockAnytype`'s
transport was synchronous, so awaited mock requests never actually
suspended and no interleaving was possible in tests.

## Decision

1. **One writer at a time.** Every store mutation (`create_node`,
   `update_node`, `add_link`, `remove_link`) runs inside a FIFO
   `asyncio.Lock` critical section in `AnytypeGraphRepository` — enqueue is
   awaiting the lock; the "delta" is the bound method call itself. The
   settle-window retries deliberately sleep while holding the lock: a later
   write must not overtake an earlier one mid-retry.
2. **PATCH payloads are materialized from store truth, inside the critical
   section.** Relation-list writes read the source object's current
   targets via a fresh `GET` (`_current_targets`; reads are unthrottled,
   S7) instead of the index. A wholesale-replace PATCH can therefore never
   be built on state another writer — bot or human — already changed.
3. **The fresh read is the precise Q2 race detector.** When store truth
   diverges from the index view, the adapter logs a warning naming the
   node, property, and both lists — and the write builds on store truth,
   so the human's edit *survives* rather than being clobbered. This lands
   the hardening Q2 deferred, and improves composite-create rollback for
   free (it now restores what the store really held).
4. **Port contract, not adapter behavior:** *concurrent link mutations on
   one node all take effect*, certified by the contract suite against both
   implementations (the fake is synchronously atomic; the adapter uses the
   seam). Asserted against the store post-`hydrate`, where lost updates
   actually show.
5. **Mock fidelity:** `MockAnytype`'s transport now yields to the event
   loop on every request (real I/O always suspends). Without this, the
   whole class of in-process concurrency bugs is invisible to the suite.
6. **Queue depth is surfaced** (`pending_writes`) for WP8's user feedback.

## Consequences

- Lost bot-vs-bot updates are unrepresentable, not merely unlikely; the
  orchestrator's concurrent turns (WP8) can safely share one repository.
- Human-vs-bot remains last-write-wins *for scalar fields*; for relation
  lists the human edit now survives a bot link-write, and every detected
  divergence is logged precisely.
- Each relation write costs one extra GET (unthrottled; negligible), and
  writes serialize — which under the ~1 write/s live throttle they
  effectively already did. Explicit pacing (a configurable interval) and
  fairness policies are deferred to WP8 proper, where queue-depth feedback
  gives them a consumer; the client's existing 429 backoff plus
  serialization suffice today.
- The index still lags the store between syncs: a divergence discovered by
  `_current_targets` is written to the store correctly but the index only
  reconciles on the next hydrate/resync — the documented direction (index
  may lag, never lead).
