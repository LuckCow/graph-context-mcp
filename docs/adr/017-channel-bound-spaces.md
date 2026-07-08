# ADR 017: Channel-bound spaces (one Discord channel, one Anytype space)

Date: 2026-07-06
Status: accepted

## Context

Until now one process meant one space: `ANYTYPE_SPACE_ID` was read once,
`composition.build_runtime` built one client/repository/session/services
bundle, and every Discord channel in the `GC_DISCORD_CHANNELS` allowlist
shared it. A user running several worlds (a fiction space, a fieldwork
space) had to run several bots. ADR 015 had also deferred per-space
*profile* selection because the profile drives `ensure_schema` before any
repository exists — a bootstrapping problem.

## Decision

**Each Discord channel may be bound to its own Anytype space, declared
statically at startup in a `GC_CHANNELS_FILE` TOML file:**

```toml
[channels.1523551542123298896]
space_id   = "bafyre..."       # required
profile    = "fiction"         # optional; defaults to GC_PROFILE
project    = "Ashfall"         # optional cosmetic label
modes_file = "ashfall.toml"    # optional; overrides GC_MODES_FILE
```

Mechanically, the bot's composition root multiplies the *existing*
runtime assembly rather than threading channel ids through the layers:
`bootstrap.build_channel_runtimes` calls the same per-runtime wiring the
CLI uses once **per binding**, so each channel gets its own client,
repository/GraphIndex, `SessionState` (persisted to that space's own
SessionContext node), mutation journal, mode store, and teardown hooks.
The LLM driver and turn log are shared — both are per-turn stateless.
The transport routes `channel_id -> ChannelRoute` (runtime + its own
turn lock); everything below the composition roots is unchanged, and the
stdio MCP server stays single-space (`GC_PROFILE` + `ANYTYPE_SPACE_ID`).

Consequences of the static file:

- **Per-space profile selection lands here** (ADR 015's deferral): the
  binding names the profile *before* that space's `ensure_schema` runs,
  dissolving the bootstrapping problem for the orchestrator path.
- **Per-channel modes**: a binding's `modes_file` replaces the global
  `GC_MODES_FILE` for that runtime; precedence within a runtime is
  unchanged (profile defaults < file < in-space Activity Mode objects,
  which are naturally per-space already).
- **One channel per space** is enforced at parse time: a space holds
  exactly one SessionContext meta-node, so two runtimes on one space
  would clobber each other's focus/recent snapshot (LWW). WP8's keyed
  multi-session store is what lifts this, not a second channel.
- **Startup is sequential and fail-fast.** Spaces hydrate one after
  another (concurrent `ensure_schema` bursts would only trip the live
  server's ~1 write/s throttle into retry backoff), and any space that
  fails to auth/hydrate stops the whole bot with the channel and space
  named — a half-alive bot silently ignoring a channel is the worse
  failure mode.
- **Turns serialize per route, not process-wide.** Different spaces have
  disjoint repositories, sessions, journals, and write queues, so their
  turns may interleave; channels sharing the one legacy runtime share
  one route and still serialize.

**Backwards compatibility:** `GC_CHANNELS_FILE` unset means exactly the
old behavior — the `GC_DISCORD_CHANNELS` allowlist over one
env-configured runtime. Setting both is ambiguous and fails loudly.

## Alternatives considered

- **A `channel`/`space` parameter on the tools or a `switch_space`
  action.** Rejected: it drags a transport concern through every layer
  and makes the graph a moving target mid-session; the runtime bundle is
  already the natural unit of isolation.
- **Dynamic (lazy) binding, spaces attached at first message.** Rejected
  for v1: config errors would surface mid-conversation instead of at
  startup, against the repo's fail-loudly-at-boot ethos.
- **One bot process per space.** Works today, but N processes for N
  worlds multiplies tokens, deploys, and Discord app registrations for
  no architectural gain.

## Consequences

- Startup time and memory grow linearly with bindings (one hydrate +
  optional embedder refresh per space). Fine at the intended scale
  (a handful of channels); per-channel hydration is logged.
- The live server's write throttle is global while each runtime's
  single-writer queue paces independently; concurrent turns in different
  channels can see extra 429 retries, which the client already absorbs
  with backoff.
- The semantic cache was already keyed per space
  (`semantic-<space_id>.sqlite`), so multiple runtimes coexist without
  collisions.

## Amendment (2026-07-07, ADR 019)

The Anytype in-space chat transport binds SPACES directly
(`spaces.toml`, table key = space id) rather than channels — see ADR 019.
The one-runtime-per-space invariant now spans both files: a space bound
in `channels.toml` must not also appear in `spaces.toml` (operator
invariant, stated in both file headers; not machine-checkable across
processes).
