# Automation Rules

Automation Rules are reactive automations the user configures at
runtime, from Anytype, with no code changes and no deployment step.
Where a Scheduled Event (`docs/adr/027-*`) fires on *time*, a rule
fires on *state*: "when a Task's Done box is checked, stamp its
Completion date," "these objects share one Default checkbox — checking
one should uncheck the others," "recompute this pick when I ask."

Design decisions live in `docs/adr/039-reactive-rule-engine.md`
(engine + built-in actions), `docs/adr/040-sandboxed-script-action.md`
(the `run script` action + the `automation` tool), and
`docs/adr/058-manual-rule-runs.md` (running a rule on demand). This
doc is the practical companion: how the pieces fit together, and
example setups to copy.

## The mental model

A rule is an ordinary Anytype object of type **Automation Rule**. It
watches ONE scalar property on ONE object type, and when that
property's value **transitions** in a way the rule's condition
describes, it runs ONE action. That's the whole shape — no chaining,
no multi-step workflows, no cross-rule triggers by design (see
[No cascades](#no-cascades-ever) below).

A rule fires only on a change the engine *observes while it is
running*. Nothing retroactive: a box ticked while the bot was down is
absorbed silently on the next resync, not replayed as a fresh
transition.

## Anatomy of a rule

Every rule is backed by ten properties on the `gc_rule` type (display
names below are what you see in the Anytype editor):

| Property | Purpose |
|---|---|
| Rule target type | Which object type to watch, e.g. `Task`. |
| Rule watch property | The property whose *change* triggers the rule, e.g. `Done`. Optional only for a `Manual` rule. |
| Rule condition | `Changed to true`, `Changed to false`, `Changed`, or `Manual`. |
| Rule action | `Set property to now`, `Set property value`, `Uncheck others of type`, or `Run script`. |
| Rule action property | The property the action writes (implied for `Uncheck others of type`; unused by `Run script`). |
| Rule action value | The literal value `Set property value` writes. |
| Rule status | `Active` / `Paused` / `Error` — see [Status and self-healing](#status-and-self-healing). |
| Rule run now | Tick to run the rule once, right now, regardless of condition. See [Manual runs](#manual-runs). |
| Rule last fired | Bookkeeping timestamp, engine-owned. |
| Rule last error | The most recent validation/action failure, engine-owned. |

A rule with neither a target type nor a watch property is an
**unconfigured template** — skipped silently, never an error. The
seeded "Example Automation Rule" object (created once when the type is
first minted) is exactly this: an explainer with empty config, plus
the recipes below written into its body.

For `Run script`, the rule's Python source lives in the object's
**body** as a fenced ` ```python ` block, not in a property (multi-line
code in a text field is miserable to edit, and the body is where
humans already write code with syntax styling).

## How the engine decides something changed

Anytype's local API has no push notification for object changes, so
the engine polls: each space's bot runs a **change tick** (default
every 5s, `GC_CHANGE_TICK_SECONDS`, `off` disables; the rule engine's
own knob `GC_RULE_TICK_SECONDS` is honored as a compatibility alias).
Each tick:

1. Resyncs the space's graph index from Anytype (a modified-since
   search — cheap against the unthrottled headless sidecar).
2. Diffs the new index against an **in-memory baseline** of
   `{object id → {watched property → last-seen value}}`, restricted to
   the (type, property) pairs any enabled rule watches.
3. Fires every rule whose condition the diff satisfies.
4. **Rebaselines from the post-action index** — including the engine's
   own writes.

That last step is the load-bearing one:

- **Nothing fires on restart or resync replay.** The first tick after
  a restart only records the baseline; a transition made while the
  engine was down is absorbed, not replayed.
- **An engine write can never trigger a rule.** Rebaselining after
  every action means the engine's own writes are folded in before the
  next diff runs — so rules never cascade, by construction, even
  against themselves.
- **At-most-once per (rule, object, transition).** The baseline
  advances whether or not the action succeeded. A failed action lands
  in *Rule last error* and is **not retried** — retrying against
  evolved state would be wrong, and a crash-looping action is worse
  than one missed fire.
- A newly created object, a newly enabled rule, and an object new to
  the index all baseline silently on first sight — a Task *born* with
  Done already ticked never "transitions."

### No cascades, ever

This is the single hardest guarantee to give up, so it's worth being
explicit: rule A cannot cause rule B to fire in the same tick or any
later one, and a rule cannot fire itself off its own write. If two
rules watch properties that happen to overlap, rule-load logs an
informational note — but nothing fires. Design each rule to stand
alone.

## Conditions

| Condition | Fires when |
|---|---|
| `Changed to true` | The watched property flips from falsy to `true` (checkbox ticked, select set to a truthy value). |
| `Changed to false` | The watched property flips from `true` to falsy. |
| `Changed` | The value differs at all, in either direction. |
| `Manual` | **Never**, by itself — see below. |

A text property that autosaves as you type can fire `Changed` on a
half-typed value; prefer checkbox or select watch properties when the
rule cares about a deliberate edit. Every type also carries a
**built-in watchable**, `modified_at` (aliases: "modified date", "last
modified date") — the object's own last-modified stamp, watchable with
condition `Changed` on ANY type with zero schema setup. It's read-only:
legal as a watch property, never as an action property.

## Actions

| Action | Effect |
|---|---|
| `Set property to now` | Stamps *Rule action property* with the current local date-time. Format-aware: a date-typed property gets local midnight with an explicit UTC offset (see [the bare-date quirk](adr/039-reactive-rule-engine.md), a text property gets the full timestamp — use text if you want time-of-day). |
| `Set property value` | Writes *Rule action value* into *Rule action property*, type-checked against the property's format where known. |
| `Uncheck others of type` | The exclusive-default recipe: fires on `Changed to true` (leave *Rule condition* and *Rule action property* empty — they're implied), then unchecks the same property on every other object of the type. Idempotent; two objects flipping true in the same tick resolve last-writer-wins by object id. |
| `Run script` | Runs the fenced Python block in the rule's body — see below. |

## The `Run script` action

For anything that isn't a single stamp-or-flip, a rule can carry a
Python script that reads the graph and queues writes. It runs
**out-of-process**, in a locked-down subprocess, once per fire:

- **Isolation is a subprocess, not a sandbox library** (the egress
  firewall blocks anything beyond the stdlib). The child drops its own
  rlimits before running user code: 5s CPU, 256 MiB address space, no
  forking. A wall-clock timeout (`GC_RULE_SCRIPT_TIMEOUT_SECONDS`,
  default 5s) SIGKILLs a runaway script. This is a guard against
  **accidents** — infinite loops, memory bombs — not a hostile author;
  the threat model assumes the rule author is the space owner.
- **The script never touches the store.** It receives a snapshot of
  the whole non-infra graph (capped at 2000 objects) plus the trigger
  context, and can only *queue* effects with `set(...)`. The engine
  validates every queued write, then applies them all through the same
  path the built-in actions use — so script writes get the same
  no-cascade, at-most-once guarantees for free.

The script's globals:

```
trigger          # the changed object: {"id","type","name","summary","fields"}
before, after    # the watched value around the change ("" = empty/unset)
now              # "YYYY-MM-DD HH:MM:SS" server-local — use this, never a clock call

objects(type=None)             # every object, optionally filtered by type
find(name, type=None)          # look up one object by name
field(obj, prop)               # read a scalar property
neighbors(obj, edge_type=None) # read linked objects
set(obj_or_id, prop, value)    # queue a write (str/bool/int/float; ≤20 per fire)
log(msg)                       # a line for the system log (print() is discarded)
```

No imports beyond the standard library, no network, ~5s of run time,
and a space with more than 2000 objects is too large to snapshot for a
script rule.

## Status and self-healing

*Rule status* is lenient, like Scheduled Events: empty and unknown
values read as `Active`. Only an explicit off-word (`Paused`,
`disabled`, `off`, `cancelled`) pauses a rule — that word is always the
human's; the engine never writes it.

`Error` is **engine-owned and self-healing**: every tick, the engine
tries to parse and resolve each rule's config. A rule that fails
(unknown target type, unresolvable property, bad action combination)
gets `Error` + a message in *Rule last error*; the moment the config
parses again, the engine flips it back to `Active` and clears the
error. Nothing needs to be told to retry.

## Manual runs

Some rules aren't reactions — they're formulas you want computed on
demand ("pick tonight's restaurant"). Naming a watch property just to
give such a rule *something* to trigger on is noise. The `Manual`
condition is built for this: `condition_met("manual", ...)` is
satisfied by no transition at all, so a Manual rule never fires on its
own. Its watch property is optional — it's the only condition where
`Rule watch property` can be left empty (a target type is still
required, since a fire still needs some object to run against).

Two ways to trigger a Manual (or any) rule on demand, converging on the
same engine call:

- **`/run <rule name>`** in chat — a command, not a model turn. No LLM
  cost, no intent node recorded. Bare `/run` lists the rules by name;
  `/run <rule name> on <object>` names the trigger object (the bare
  rule name is tried first, so a rule literally named "Turn on lights"
  still resolves). `/run` resyncs the space first, so it acts on what
  you can currently see.
- **The `Rule run now` checkbox**, ticked directly on the rule object
  in Anytype. The engine unticks it as it claims the request (within
  one tick), so re-ticking it runs the rule again. No chat involved.

Both are **at-most-once**: the box is unticked *before* the rule runs,
so a crashing action can't re-fire forever, and a failed claim leaves
the box ticked for a retry on the next tick. A Manual run against a
rule with no named trigger just picks the first object of the target
type — fine for a formula that reads the whole graph, less useful for
a per-object action (use `/run <rule> on <object>` there).

A run against a **Paused** rule refuses loudly (written only to *Rule
last error*, never to `Error` — writing `Error` would make the rule
self-heal back to `Active` on the next tick, silently un-pausing it).

There is deliberately **no way for the assistant to run a rule on its
own** — no tool action for it. The point of Manual rules is to skip
spending a model turn on deterministic computation; giving the model a
"run" action would reintroduce that cost and open a path to mutating
the graph outside the normal write-tool discipline. If asked, the
assistant will point at `/run` or the checkbox, or offer a dry-run
`test` instead.

## Authoring a rule

**In Anytype, directly:** create an object of type Automation Rule and
fill in the fields above — this is the primary, no-code path a human
uses. For `Run script`, write the fenced ` ```python ` block into the
object's body.

**Via the assistant:** the `automation` tool (bound in every mode,
alongside `schedule`) lets the assistant draft, test, and manage rules
on your behalf:

- `create` / `update` — validated immediately (unknown property, bad
  action combination) rather than surfacing as `Error` on the next
  tick.
- `list` — every rule with its config, status, and last-fired time.
- `pause` / `resume` — flip status without deleting (there's no hard
  delete; a rule is bookkeeping the user can always see in Anytype).
- `test` — a **dry run**: simulate one fire against a real object (or
  a chosen one), run any script for real in the sandbox, validate the
  effects through the production code path, and report what *would*
  be written. Nothing is applied. Works against a stored rule or an
  inline draft, so the assistant can iterate on a script before
  creating it — script rules are auto-tested on every create/update.

The tool has **no `run` action** — see [Manual runs](#manual-runs).

## Example setups

These are the same four recipes seeded into the "Example Automation
Rule" explainer object, spelled out:

**1. Stamp a completion date**
> Target type `Task` · watch property `Done` · condition
> `Changed to true` · action `Set property to now` · action property
> `Completion date`

Ticking Done on any Task stamps that Task's Completion date within a
few seconds. Untick-and-retick fires it again.

**2. Keep one default at a time**
> Target type `Project` · watch property `Default` · action
> `Uncheck others of type`

Ticking Default on one Project automatically unticks it on every other
Project. Leave condition and action property empty — they default
correctly for this action.

**3. A reactive script — maintain a derived count**
> Target type `Task` · watch property `Done` · condition `Changed` ·
> action `Run script`
>
> Body:
> ```python
> open_tasks = [t for t in objects(type="Task")
>               if field(t, "Done") != "true"]
> project = find("Roadmap", type="Project")
> if project:
>     set(project, "Open tasks", len(open_tasks))
>     log(f"{len(open_tasks)} open tasks")
> ```

Any Task's Done value changing (in either direction) recomputes the
open-task count on the "Roadmap" Project. This is the shape for
anything the three built-in actions can't express: cross-object
aggregation, conditional logic, computed values.

**4. A formula you run on demand**
> Target type `Restaurant` · watch property *empty* · condition
> `Manual` · action `Run script`, with the picking logic in the body.

Nothing triggers this automatically. Run it by ticking `Rule run now`
on the object, or sending `/run <rule name>` in chat — useful for
anything you want computed *when you ask*, like a dinner picker, not
in reaction to an edit. Fleshed out with a real weighting algorithm and
test data below.

## Worked example: a fair dinner picker

Recipe 4 above is intentionally bare — "put a formula in the body."
Here's a complete one, built to a concrete spec: **30 ranked
restaurants, rank 1 gets picked at most once a month, rank 30 (the
lowest) gets picked at least once a year**, with everyone else scaled
between those two extremes by rank.

### The algorithm

Two ideas, layered:

1. **A per-rank cooldown, interpolated linearly from rank 1 to the
   worst rank.** Rank 1's cooldown is 30 days (hard-excludes it from
   the draw before a month has passed — this alone *is* "at most once
   a month," not a probabilistic approximation of it). The worst rank's
   cooldown is 300 days — long, but deliberately short of the ceiling
   below so the *normal* weighted draw gets a chance to pick it up
   before it has to be forced.
2. **A hard ceiling of 365 days, checked before anything else.** If any
   restaurant has gone a year without a visit, the script force-picks
   the *most* overdue one, bypassing the weighted draw entirely. This
   is the actual mechanism that guarantees "at least once a year" — a
   restaurant's weight alone can never guarantee it wins a lottery
   against 29 competitors, but a hard rule checked every run can.

Among restaurants past their own cooldown (and with nobody over the
365-day ceiling), the pick is a weighted lottery: `weight = (31 - rank)
× (days_since_last_visit ÷ cooldown)`. The `(31 - rank)` term favors
better-ranked restaurants; the ratio term makes a restaurant's odds
climb the longer it's gone unpicked, so a mediocre restaurant that's
been passed over for months can still occasionally win.

The precise, provable guarantee this produces: **no restaurant ever
goes longer than `365 + (days between rule runs − 1)` days without a
visit**, and **rank 1 never repeats sooner than 30 days**, full stop —
both are hard checks, not statistical tendencies. Everything in
between (rank 15 shows up roughly a few times a year, etc.) is the
weighted lottery's job, tuned but not guaranteed.

### The rule

> Target type `Restaurant` · watch property *empty* · condition
> `Manual` · action `Run script`. The type needs two properties:
> `Rank` (number, 1 = best) and `Last visited` (date).

Body:

```python
from datetime import datetime

MIN_COOLDOWN = 30       # rank 1: cannot repeat within this many days
MAX_COOLDOWN = 300      # worst rank: cannot repeat within this many days
                         # either, but leaves room below FORCE_CEILING
                         # for a natural pick before it must be forced
FORCE_CEILING = 365      # nobody goes unpicked longer than this
NEVER_VISITED = 99999    # sentinel "days since" for no history at all

def parse_date(value):
    if not value:
        return None
    return datetime.strptime(value[:10], "%Y-%m-%d")

now_dt = datetime.strptime(now[:10], "%Y-%m-%d")
restaurants = objects(type="Restaurant")
worst_rank = max((int(field(r, "Rank") or 1) for r in restaurants), default=1)

rows = []
for r in restaurants:
    rank = int(field(r, "Rank") or worst_rank)
    last = parse_date(field(r, "Last visited"))
    days_since = (now_dt - last).days if last else NEVER_VISITED
    frac = (rank - 1) / (worst_rank - 1) if worst_rank > 1 else 0
    cooldown = MIN_COOLDOWN + frac * (MAX_COOLDOWN - MIN_COOLDOWN)
    rows.append({
        "obj": r, "rank": rank, "days_since": days_since, "cooldown": cooldown,
    })

# Phase 1: hard backstop -- force the most overdue restaurant if anyone
# has gone a full year without a visit. Never skipped, regardless of weights.
overdue = [row for row in rows if row["days_since"] >= FORCE_CEILING]
if overdue:
    overdue.sort(key=lambda row: (-row["days_since"], -row["rank"]))
    pick = overdue[0]
    log(f"forced pick: {pick['obj']['name']} (rank {pick['rank']}, "
        f"{pick['days_since']}d overdue)")
else:
    # Phase 2: weighted lottery among restaurants past their own cooldown.
    eligible = [row for row in rows if row["days_since"] >= row["cooldown"]]
    if not eligible:
        eligible = sorted(rows, key=lambda row: -row["days_since"])[:1]
        log("nothing past cooldown yet -- falling back to least-recent")
    weights = [
        (worst_rank + 1 - row["rank"]) * (row["days_since"] / row["cooldown"])
        for row in eligible
    ]
    import random
    pick = random.choices(eligible, weights=weights, k=1)[0]
    log(f"weighted pick: {pick['obj']['name']} (rank {pick['rank']}, "
        f"{pick['days_since']}d since last visit, "
        f"{len(eligible)} eligible)")

set(pick["obj"], "Last visited", now[:10])
log(f"tonight: {pick['obj']['name']}")
```

Run it with `/run Dinner Picker` (or tick `Rule run now`) whenever you
want tonight's pick.

### Test data: 30 restaurants

The set used to validate the algorithm below — rank 1 is the favorite,
rank 30 the one everyone forgets exists. `Last visited` is seeded with
a spread of past dates (5–250 days before 2024-01-01) so the test
starts "warm" instead of every restaurant tied at "never visited":

| Rank | Name | Last visited |
|---|---|---|
| 1 | The Copper Fork | 2023-07-17 |
| 2 | Nonna's Table | 2023-11-29 |
| 3 | Blue Heron Grill | 2023-12-21 |
| 4 | Saffron & Salt | 2023-06-21 |
| 5 | The Rusty Anchor | 2023-10-18 |
| 6 | Maple & Vine | 2023-10-26 |
| 7 | Little Lantern | 2023-10-31 |
| 8 | Basil & Bone | 2023-11-22 |
| 9 | Harbor House | 2023-06-22 |
| 10 | The Gilded Spoon | 2023-12-01 |
| 11 | Smoke & Cedar | 2023-07-07 |
| 12 | Paprika House | 2023-06-21 |
| 13 | The Tin Whistle | 2023-05-13 |
| 14 | Olive Branch Bistro | 2023-08-10 |
| 15 | Twin Oaks Tavern | 2023-12-05 |
| 16 | The Wandering Noodle | 2023-07-29 |
| 17 | Ember & Ash | 2023-09-10 |
| 18 | Marigold Kitchen | 2023-12-19 |
| 19 | The Salty Pig | 2023-12-20 |
| 20 | Cinderbrick Pizza | 2023-12-04 |
| 21 | The Quiet Fox | 2023-11-02 |
| 22 | Golden Ladle | 2023-10-29 |
| 23 | Prairie & Pine | 2023-08-20 |
| 24 | The Broken Yolk | 2023-07-26 |
| 25 | Cobblestone Cafe | 2023-12-21 |
| 26 | Northside Diner | 2023-08-06 |
| 27 | The Rickety Table | 2023-11-07 |
| 28 | Copperline BBQ | 2023-06-27 |
| 29 | The Faded Awning | 2023-07-14 |
| 30 | Last Chance Grill | 2023-07-01 |

A sample run, drawing once a week starting 2024-01-01 (each pick's
"Last visited" feeds into the next draw, exactly as the live rule
would behave):

```
2024-01-01 -> tonight: Harbor House        (rank 9)
2024-01-08 -> tonight: Saffron & Salt      (rank 4)
2024-01-15 -> tonight: The Copper Fork     (rank 1)
2024-01-22 -> tonight: Olive Branch Bistro (rank 14)
2024-01-29 -> tonight: The Tin Whistle     (rank 13)
2024-02-05 -> tonight: Smoke & Cedar       (rank 11)
2024-02-12 -> tonight: Nonna's Table       (rank 2)
2024-02-19 -> tonight: Blue Heron Grill    (rank 3)
2024-02-26 -> tonight: Little Lantern      (rank 7)
2024-03-04 -> tonight: The Rusty Anchor    (rank 5)
2024-03-11 -> tonight: Ember & Ash         (rank 17)
2024-03-18 -> tonight: Maple & Vine        (rank 6)
```

### Validation

This script was run for real through the engine's own sandbox executor
(`infrastructure/sandbox/bootstrap.execute`, the same function the
subprocess runs — it's importable standalone for exactly this kind of
test) against the data above, drawing once a week for 5 simulated
years (260 draws), feeding each pick's `Last visited` write into the
next week's draw:

- **All 30 restaurants got picked at least once.**
- **Rank 1** ("The Copper Fork") was picked 24 times; the closest two
  picks were **35 days apart** — never under the 30-day floor, across
  every random seed tried.
- **Rank 30** ("Last Chance Grill") was picked 5 times, roughly once a
  year, with gaps of 365–371 days between visits.
- **Worst gap across all 30 ranks, over the whole 5-year run: 371
  days** — exactly `365 + (7 − 1)`, i.e. the theoretical ceiling given
  weekly draws. Nobody ever went longer than one draw-cycle past a
  year.
- Rank-1 pick frequency (24 times in 5 years) tapered smoothly down to
  roughly 5 times in 5 years for the bottom ranks — the weighted-lottery
  term is doing real work between the two hard-guaranteed extremes, not
  just deferring everything to the ceiling.

The `365 + (interval − 1)` bound is exact, not empirical: the ceiling
check only runs when the rule runs, so the worst case is always one
draw-interval short of "on time." Running the picker daily instead of
weekly tightens that worst case to 365 days flat; running it monthly
loosens it to about 395. Pick a run cadence — a Scheduled Event calling
`/run Dinner Picker`, or just asking whenever you're hungry — with that
trade-off in mind.

## Troubleshooting

- **A rule isn't firing.** Check *Rule status* isn't `Paused`, that
  *Rule target type* and *Rule watch property* both resolve against
  the space's actual schema (case-insensitive display name or key),
  and that the change actually happened while the bot was running —
  nothing retroactive. `automation action=list` (or the assistant)
  shows status and the last error.
- **`Error` status won't go away.** Fix the underlying config (typo'd
  type/property name, a `run script` action missing a body fence,
  action `Set property value` with no *Rule action value*) — the
  engine clears `Error` on its own the next tick it parses cleanly.
  Nothing needs to be manually reset.
- **A script silently does nothing.** Confirm the fenced block is
  tagged ` ```python ` or has no language tag at all — a
  ` ```bash ` (or other) tag is intentionally skipped, and Anytype's
  markdown round-trip strips language tags on save, so a freshly
  created ` ```python ` fence commonly reads back untagged (both are
  fine; only a *foreign* tag is excluded). Use `automation action=test`
  to see exactly what a script would do and any error/log output
  before trusting it live.
- **Two rules seem related but nothing chains.** That's by design —
  rules never trigger rules. If you need a multi-step effect, put both
  steps in one script rule.
