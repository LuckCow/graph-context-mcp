# ADR 004: The summary-staleness rule lives in NodeWriter

**Status:** Accepted (backfilled)

## Context

Every node carries a required one-line `summary` that exploration renders by
default; a summary that silently rots is worse than none. The lifecycle rule
is: any update that changes a node without supplying a fresh summary flags
`summary_stale`. That rule has to live in exactly one place — candidates were
the domain model, the repository implementations, or the use-case layer.

## Decision

The rule is enforced solely in `application/node_writer.py::NodeWriter`.
`Node.summary_stale` is plain data; repositories persist whatever they are
told (`update_node(summary_stale=...)`); the tool layer only *reports* the
flag (and `explore only_stale=true` filters on it).

## Consequences

- One place to change the policy (e.g. exempting cosmetic field edits), and
  both repository implementations stay policy-free — the contract suite
  certifies storage behavior, not workflow rules.
- Any future write path that bypasses NodeWriter would silently skip the
  rule; new composite writes must go through the use-case layer (the
  ProseRecorder, which never updates existing nodes, is the only other
  writer).
- Clearing staleness is an explicit act: supply a fresh `summary`. The
  documented sweep is `explore(only_stale=true)` → regenerate → `update_node`
  (no dedicated refresh tool; WORK_PACKAGES WP3 settled that).
