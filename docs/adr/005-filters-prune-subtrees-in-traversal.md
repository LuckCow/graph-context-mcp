# ADR 005: Traversal filters prune subtrees, not just results

**Status:** Accepted (backfilled)

## Context

`explore` supports node-type includes/excludes, edge-type filters, and the
`as_of` story-time cutoff. Two possible semantics: filter the *result set*
(walk everything, drop non-matching nodes from the output) or filter the
*frontier* (a node that fails the filter is not expanded, so its whole
subtree is invisible).

## Decision

Filters prune the frontier (`domain/traversal.py`): a filtered-out node is
neither reported nor traversed through. The same applies to `as_of` — an
Event after the cutoff hides everything reachable only through it.

## Consequences

- Filter semantics compose intuitively with `depth`: "explore the world as of
  T" really is the world a character could know at T, not a censored dump of
  the full graph — connections that only exist via future events stay hidden.
- A filtered node can shadow reachable content; users widen filters or raise
  `depth` to see around it. The tool docstring documents this.
- The tool layer adds one *policy* filter on top (Prose/SessionContext roles
  hidden by default — a WP2 decision, deliberately kept out of the domain).
