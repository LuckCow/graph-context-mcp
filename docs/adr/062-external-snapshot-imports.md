# ADR 062: External snapshots are projected into the graph, and the graph is what rules watch

Date: 2026-09-06
Status: accepted (builds on ADR 039/040 for rules, ADR 041 for type
minting, ADR 044 for the change tick, ADR 060 for host config)

## Context

The RuneScape test space now has a RuneLite plugin writing the
character's state as JSON exports — `character`, `quests`, `diaries`,
`combat_achievements`, `equipment`, `inventory` — one file per subject,
rewritten whenever the game changes something (`reason:
stat_changed`, `item_container_changed`, …). The ask: make that
information available to the bot, and have "each variable in the JSON
compared, and if the difference triggers any scripts, run them with the
updated information."

The second half of that sentence already exists. The Automation Rule
engine (ADR 039) diffs watched properties tick over tick and fires a
`run script` action (ADR 040) with `before`/`after` and a snapshot of
the whole graph. What did not exist was any way for the game's state to
be a *property on a node*.

Two designs were on the table:

1. **Diff the JSON directly.** A second transition detector over the
   export files, with rules addressing JSON paths.
2. **Project the JSON into nodes** and let the existing engine diff
   those.

## Decision

**Option 2. The exports are upstream of the graph the way Anytype UI
edits are: an importer reconciles native nodes against them, and
everything downstream — rules, scripts, the model's `query`/`explore`,
the Anytype UI — is unchanged.** Option 1 would put the one rule the
codebase has for "detect a transition" in a second place, give rules a
second target vocabulary (`json path` beside `type · property`), and
leave the model unable to answer "what am I wearing?".

### The game is the ground truth

The importer plans against the **live index**, not against the previous
export: every tick it computes the creates and change-only updates that
make the graph match the files. A value a human edited in Anytype is
therefore written back on the next tick.

Considered and rejected: source-vs-source diffing, where a human edit
sticks until the game moves that leaf. It is a coherent model and gives
Anytype-side edits real standing — but the user's answer was that the
game is the truth and human-owned facts belong on other nodes. The
simpler rule also removes a failure mode: with graph-vs-source
reconciling in the same tick as the rule diff (below), a human edit is
reverted *before* the engine can see it, so there is no double fire and
no phantom transition. Under source-vs-source the edit would have fired
once, honestly, and that was not wanted.

Nothing is deleted. A node the export stopped listing takes its type's
`absent_fields` (an inventory holding goes to quantity 0 — the zero
transition IS the signal) or is left alone when the type declares none.
The port has no archive and the graph keeps history.

### Ordering: imports are the change tick's first listener

`_change_listeners` (ADR 044) gains `imports` AHEAD of `rules`. One tick
is now: resync → reconcile against the exports → rule diff → mode
refresh → history. Consequences, all load-bearing:

* A rule fires on the game's value in the same 5 s tick the file changed.
* Scripts see every leaf the same export changed, because the importer
  writes ALL differences before the engine reads any.
* A human edit to an imported node is clobbered before the diff, so the
  engine never observes it (pinned in the wiring test).
* The bootstrap's seed pass runs before the engine's first tick, whose
  baseline is `None` → everything created is baselined silently. Nothing
  fires on boot, matching a restart with no importer.

The importer's own writes go straight to the repository — no
`NodeWriter` (no summary-staleness rule, no focus touch, no locked-section
guard: the values are the game's facts, not authored prose) and no
journal (an import is not a turn; the rule engine's tick writes are
unjournalled for the same reason). Types are minted through the ADR 041
port methods, `create_type` once and `add_type_properties` on later
starts, so a new plugin version's property retrofits without a
migration. A same-name property with a conflicting format is a startup
error naming it — a misconfigured space, not something to paper over.

### The mapping: many small nodes of few types

A rule watches ONE property on ONE type, so "each variable" means one
scalar property per watchable leaf on a node the human would name in a
rule:

| Type | Nodes | Properties | Note |
|---|---|---|---|
| Character | 1 | World, Game state, Members, Total level, Total experience, Last export, Export reason, Plugin version | `Last export` is the "on any export" hook — the only node every export touches |
| Skill | 24 | Level, Boosted level, Experience | |
| Quest | 211 | Quest id, Status (select), Complete (checkbox) | select for `changed`, checkbox for `changed to true` |
| Achievement Diary | 48 | Region, Tier (select), Complete, Tasks done | named `Ardougne Hard` |
| Combat Achievement Tier | 6 | Complete, Tasks completed, Tasks total | |
| Equipment Slot | 11 | Slot, Item, Item id, Quantity | slot-keyed: every worn slot always exists, so "Weapon changed" can be authored before anything is equipped |
| Item Holding | per item name | Item id, Quantity, Stacks | summed across stacks and ids (four nest ids share one name); zeroed on leaving the bag, never deleted |

Combat achievements are tier nodes, not the 637 tasks: the sandbox's
script snapshot is capped at 2 000 objects, and a third of it for a
feature no rule asks for yet is the wrong default. Per-task nodes are a
follow-up mapper, not a switch. Total: ~315 nodes on the real export set.

Names come from the game verbatim; only the Character carries the
export's own metadata, so every export changes exactly one node beyond
what the game changed.

### Where it lives

* `domain/imports.py` — the desired-state vocabulary (`ImportType`,
  `ImportNode`, `ImportFormat`) and the pure planner. Values compare in
  read-back form: absent checkbox == `false`, display names resolve to
  store keys through `field_catalog`, and both candidates go through
  `domain.fields.lookup` — the one read rule, lifted out of
  `RuleEngine._field_of` so the planner and the engine cannot disagree on
  what a value *is*.
* `domain/runescape.py` — the RuneLite mapper. Pure; a misshapen
  document raises `ImportDocumentError` naming its kind and the importer
  skips that one document.
* `ports/import_source.py` + `infrastructure/imports/filesystem.py` —
  `read()` returns the CURRENT decoded documents; the directory adapter
  re-decodes only files whose `(mtime_ns, size)` moved, keeps the last
  good decode across a torn mid-write read (warned once per bad version),
  and is loud only when the directory itself is missing or gone.
* `application/snapshot_importer.py` — `SnapshotImporter.ensure_types`
  and `run_tick`; one failed write is logged and the pass continues (the
  next tick's plan retries it).
* Config: `import_dir` in the git-ignored `spaces.toml` (ADR 060 — which
  host's game files a space mirrors is a deployment fact), or
  `GC_IMPORT_DIR` on the single-runtime paths (CLI, memory backend).
  The bare MCP server builds nothing: it has no tick.

## Consequences

* The write volume is bounded by what the game changes: one XP leaf per
  `stat_changed`, a few holdings per pickup, one `Last export` per file.
  The startup seed is ~315 creates against the unthrottled sidecar.
* The reconcile compare is in-memory every tick (~315 nodes × a few
  fields); only differences cost API calls.
* Item Holding nodes accumulate: every item that ever entered the
  inventory keeps a zero-quantity node. Accepted for the test space; an
  archive port method could retire long-zero holdings later.
* Imported nodes are ordinary non-infra nodes: the model can write them
  through `update_node` and the game will overwrite it next tick. The
  Space Setup mode's goal is the place to say so if it becomes a problem.

### Accepted limitations

* One format. The mapper seam (`ImportFormat`) exists but the binding
  key names a directory, not a format; a second source is a second
  mapper plus a format key on the binding.
* No links. `explore` from the Character reaches nothing; `query
  type="Skill"` is the listing surface. A Character-hub relation set is
  the obvious follow-up and was left out to keep the seed to one write
  per node.
* The importer's `modified_at` bumps land in the next tick's resync as
  `changed` ids — harmless for the `history` listener (imported types are
  never tracked) and suppressed as self-writes by the adapter.
