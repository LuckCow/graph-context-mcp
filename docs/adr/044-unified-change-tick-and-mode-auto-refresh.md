# ADR 044: Unified change tick + automatic mode-registry refresh

Date: 2026-07-21
Status: accepted (amends ADR 039's single-purpose rule watcher and the
ADR 015/034/035 refresh story — "startup and `/mode`" becomes "startup,
`/mode`, and within one change tick of an edit")

## Context

The space is the mode-config editing surface (ADRs 034/035): humans edit
Activity Mode objects and relink `gc_default_mode` on the Space Context
directly in Anytype. But the registry only reloaded at startup or when
someone typed `/mode` in a chat — an edit sat invisible until a human
happened to issue the command, which reads as broken in exactly the way
ADR 039 called out for rule reactions.

Meanwhile the bot had grown four sibling watchers, one of which —
`_watch_rules` — was the only *change-driven* one: its tick ran its own
cheap modified-since resync under the route lock, then diffed an
in-memory baseline. Mode refresh is a second change-driven reaction, and
more are plausible. Copying the loop/lock/error boilerplate per feature
(and paying one resync per watcher per tick) generalizes badly; a full
event-bus/subscription framework overshoots two one-line reactions and
cuts against the repo's explicit-wiring style.

Detection is cheap because the index already carries what's needed:
Activity Mode and Space Context objects hydrate as infra-role nodes
(`Role.MODE`, `Role.SPACE_CONTEXT`) with `modified_at` stamps that bump
on any edit — including body edits and the default-mode relink. Their
`gc_mode_*` payloads and goal bodies are deliberately NOT on index nodes,
so detection can ride the index while reloading still goes through the
two stores (ModeStore + SpaceContextStore, search + per-hit GETs).

## Decision

**One unified change tick per space, with an ordered listener list.**
`_watch_rules` becomes `_watch_changes` (`anytype_chat_bot.py`): each
tick, under the route lock, one modified-since resync, then each named
listener in order — per-listener try/except (GraphContextError warns,
anything else logs the traceback), so a failing listener never starves
the next and nothing takes the serve loop down. The list is assembled
bot-side in `_change_listeners`: `rules` first (reaction latency is
their 5s contract, ADR 039), then `modes`. A future on-change feature is
one listener here, not a new watcher.

**Config:** `GC_CHANGE_TICK_SECONDS` (default 5, `0`/`off` disables all
change listeners). `GC_RULE_TICK_SECONDS` — the pre-ADR-044 name for
what was then only the rule tick — is honored as a compat alias when the
new name is unset.

**Mode auto-refresh** is a public `Orchestrator.refresh_modes()`:

* **Detection:** `modes.mode_fingerprint(graph)` — the frozenset of
  `(id, modified_at)` over `Role.MODE` + `Role.SPACE_CONTEXT` nodes.
  Create/edit/archive all shift the set; the relink bumps the Space
  Context stamp. `ModeConfigWatch` holds the baseline; the first check
  seeds it and reports no change (the rule-engine discipline — nothing
  reacts to a restart, and startup seeding happens pre-hydrate anyway).
  The no-change tick is a free in-memory read.
* **Reload:** on change, the existing `_refresh_registry()` path — the
  same shared degrade logic `/mode` uses (keeps the last good registry,
  errors name the object). Degrade events are logged, never posted
  (there is no chat behind a background tick); `/mode` still surfaces
  the same text on demand.
* **At-most-once per change:** the baseline advances even when the
  reload fails. A broken edit degrades once instead of re-running the
  store loads and spamming warnings every 5s; the human's next edit
  bumps `modified_at` and retries. `/mode` remains the unconditional
  reload for anyone who wants to force it.

Nothing downstream changes: specs are looked up from the registry every
turn, so an edited mode is live on each session's next turn; a vanished
mode already degrades to the default; a default-mode change still
affects only new sessions (ADR 034).

## Consequences

* Editing a mode's properties or goal, archiving/creating a mode, or
  relinking the default in the Anytype UI is live within ~one tick (5s
  default) — no `/mode`, no restart.
* One resync per tick serves every change listener; rules keep their
  latency contract by running first.
* Disabling the tick (`off`) disables rules AND mode auto-refresh; the
  bare MCP server, memory-backend CLI, and rescan-off deployments keep
  the `/mode`-and-startup refresh story.
* A persistent config error surfaces once per edit in the log rather
  than continuously; the trade is deliberate (same as rule errors —
  `gc_rule_last_error` writes once, no retry).
* ADRs 039/040's references to `_watch_rules` / `GC_RULE_TICK_SECONDS`
  describe the pre-044 shape; this ADR amends them rather than editing
  the records.
