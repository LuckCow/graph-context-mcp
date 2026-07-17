# ADR 034: The default mode lives in the space, on a Space Context object

Date: 2026-07-17
Status: accepted (supersedes the default-mode half of ADR 031; amended
by ADR 035: with profile modes retired, the no-link fallback is the
alphabetically first loaded mode, and the starter-mode seeder links the
default automatically)

## Context

ADR 031 (WP21) made the mode NEW chats start in a `spaces.toml` per-space
key, explicitly rejecting an in-space settings object as "new machinery
for one scalar". That call put mode *selection* on the deployment
operator while mode *content* (the Activity Mode objects, ADR 015) lives
with the space's human — which in practice is the same person wanting
one thing: open Anytype, decide how the assistant behaves here. Changing
the starting mode should not require editing a TOML file on the server
and restarting the bot when every other mode edit applies live via the
`/mode` reload.

A per-mode "default" checkbox on the Activity Mode objects themselves
was considered and rejected: checking one cannot uncheck the others (the
server has no clean radio-group gesture over independently-edited
objects), so two ticked boxes would be a standing ambiguity rather than
a transient mistake.

## Decision

**A `gc_space_context` singleton — display name "Space Context" — is
the space's settings object.** Bootstrap mints the type (infra role
`SpaceContext`, joining `INFRA_ROLES`) and seeds one object whose body
explains itself, the same explainer pattern as ADR 015/027. Its one
field today is `gc_default_mode`, an **`objects`-format relation
linking the Activity Mode object new chats start in** — the human picks
the mode *object* from a picker, no name to mistype, and single-select
falls out of link arity (the loader rejects >1 target loudly). Empty
link, or no Space Context object at all: the profile's built-in default
applies, as before WP21.

**Resolution is by object id, at registry load.** ModeStore payloads
now carry the mode object's `id`; the new `SpaceContextStore` port
(fake + Anytype adapter, same contract posture as ModeStore) returns
the singleton's payloads verbatim and the loader
(`modes.load_registry`) owns all validation with errors naming the
object: two Space Context objects (keep exactly one), a multi-target
link (link exactly one), a dangling link (archived / deleted / not an
Activity Mode). Consequently the default can only ever be an in-space
mode — deliberate: the space's settings point at the space's own config
objects, never at names that exist only in code or a TOML file. Startup
fails loudly on a broken link; the `/mode` refresh degrades, exactly
like broken mode objects (ADR 015). Because the reload closure re-reads
the store each time, relinking the default applies on the next `/mode`
without a restart. Sessions with a persisted mode keep it — the
pipeline consults `registry.default` only when nothing was persisted,
unchanged.

**`spaces.toml` loses `default_mode`.** The binding file keeps doing
what only it can do (which spaces to serve, profile, chat pins); a
leftover `default_mode` key fails startup with a migration pointer to
the Space Context object, not a generic unknown-key error.

**The link is server config, not story structure.** `gc_default_mode`
joins `SYSTEM_RELATION_DENYLIST`, so `to_edges` never reflects it as a
graph edge and it never surfaces as reusable edge vocabulary; the
Space Context node itself is infra-hidden from traversal like every
other bookkeeping role.

## Consequences

- The whole mode story is now in-space: define modes as Activity Mode
  objects, pick the default by linking one on Space Context, all edits
  applied by the next `/mode` — no server files, no restarts.
- One more seeded object and type per space; bootstrap's mint path,
  retrofit path, and the mock pin it in the contract suite; a live E2E
  round-trips the link against the real server.
- A default naming a profile/TOML-only mode is no longer expressible.
  The escape hatch is one in-space Activity Mode object (which is also
  where such a mode belongs if a human is expected to manage it).
- Deleting the settings object is safe (new chats revert to the profile
  default); duplicating it is a loud startup error, so the singleton
  rule needs no enforcement machinery on the write side.
