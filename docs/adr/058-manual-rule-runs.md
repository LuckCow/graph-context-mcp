# ADR 058: Manual Automation Rule runs

Date: 2026-08-08
Status: accepted (extends ADR 039/040)

## Context

An Automation Rule fires only on an observed property TRANSITION. That
is the right model for a reactive automation ("when Done is ticked,
stamp Completion date") and its guarantees — at-most-once, nothing on
restart, no cascades — all derive from it.

But it strands a whole class of rule: the ones that hold a *formula*
rather than a reaction. The motivating case is a Dinner Picker mode
whose `run script` rule computes the next dinner spot. Nothing about
that is event-driven — the user simply wants it run, now. Today they
have two bad options: fake a property change on some object to trip a
condition that exists only as a trigger of convenience, or ask the
model to do it and spend a whole LLM turn on arithmetic the sandbox
already does deterministically.

There is a second, quieter gap. Because `parse_rule_fields` requires a
watch property, such a rule must *name* a property it does not care
about, which reads as configuration and behaves as noise.

## Decision

### The `manual` condition

A fourth condition token joins `changed to true | changed to false |
changed`. `condition_met("manual", …)` returns False for every
before/after pair — that single line IS "never fires on its own", and
it lives in the domain rather than in the engine's planner so every
caller inherits it. A manual rule's watch property is optional (the
only condition for which `parse_rule_fields` relaxes it); a target
type is still required, because a fire needs a trigger object.

Rejected alternative: express "manual only" by leaving the rule
Paused. It would need no new vocabulary, but it overloads Paused with
two meanings ("switched off" vs. "runs on request"), still forces a
dummy watch property, and makes the refusal rule below incoherent.

### Two trigger surfaces, one entry point

* **`/run <rule>`** in chat, beside `/mode` and `/clear` in the
  pipeline's command dispatch. It short-circuits before the driver, so
  it costs no model turn, mints no intent node, and works in Anytype,
  Discord, and the CLI for free. Bare `/run` lists the rules (names are
  exact-match, so discovery must be one command away).
  `/run <rule> on <object>` names the trigger; the bare name is tried
  FIRST, so a rule called "Turn on lights" still resolves.
* **`gc_rule_run_now`** (display "Rule run now"), a tenth `gc_rule_*`
  property and the first with SHARED ownership: the human ticks it in
  the Anytype editor, the engine unticks it as it claims the request.
  No chat involved.

Both converge on `RuleEngine`: `/run` calls `run_now`, the checkbox
becomes a plan source inside `run_tick` via `_claim_manual_requests`.
Neither consults the condition — an explicit request IS the trigger,
so an ordinary reactive rule can be run by hand too.

The checkbox scan lives INSIDE `run_tick` rather than in a second
change-tick listener: a separate pass would need its own
`_load_rules` + `_attach_scripts` (a body GET per script rule) +
`_note_overlaps` + `_write_bookkeeping` + `_rebaseline`, and a rule
that both transitioned and was hand-requested in one tick would book
and rebaseline twice.

### Deliberately NOT an `automation` tool action

The tool surface stays create/update/list/pause/resume/test. The point
of the feature is to run a rule *without* the model; an `action="run"`
would reintroduce the cost it removes, and give the model a way to
mutate the graph that bypasses the mutation tools' discipline. The
tool doc says so explicitly and names the two human gestures, because
otherwise the model invents the action or claims it fired the rule.

### Paused refuses, loudly

A manual run on a Paused rule raises rather than running. `run_now`
raises a `GraphContextError`; the checkbox path catches the same
refusal into a `RuleProblem`. One rule, two consumers.

Crucially, a paused refusal writes ONLY `gc_rule_last_error` — never
the Error status. `is_paused("Error")` is False, so an Error write
would get the rule scanned on the next tick and self-healed to Active:
ticking a box would silently RESUME a rule the human switched off.

## Invariants preserved

* **At-most-once.** The box is unticked BEFORE the rule runs — the
  scheduler's mark-fired-before-firing discipline. A crashing action
  therefore cannot re-fire forever. A FAILED claim leaves the box
  ticked and skips the fire, so the next tick retries; this is why the
  claim cannot go through the error-swallowing `_write_rule_fields`.
* **No cascades.** Every path ends at `_rebaseline(bound)` over the
  FULL bound set, rebuilt from the post-action index — so a manual
  run's writes can never read as transitions. Scoping the rebaseline
  to the one rule that ran would leave every other rule's baseline
  stale and fire them on pre-run history. `run_now`'s *bookkeeping* is
  deliberately scoped to the one rule (a `/run` must not rewrite
  another rule's error/heal state); the asymmetry is intentional.
* **Rules never trigger rules.** The engine's own untick lands on a
  rule node, whose `Role.RULE` is in `INFRA_ROLES`, which `_targets`
  excludes — the same construction that protects automatic fires.
* **Nothing fires on restart.** `run_now` on an engine that has never
  ticked installs a baseline from the post-action index. This arms the
  engine a few seconds early, which is strictly more correct: it has
  now observed the state. Leaving the baseline `None` would be equally
  safe but makes `_rebaseline` a conditional whose two arms both need
  reasoning about.

## Consequences

* One bug fixed on the way in: `_write_rule_fields` merged over the
  fields snapshot `_load_rules` captured at the top of the tick, while
  its sibling `_write_field` re-reads. Nothing exercised the difference
  before — the run-now claim does, and the stale merge would have
  re-ticked the box from the same tick's `gc_rule_last_fired` write,
  firing the rule forever. It now re-reads.
* `_note_overlaps` gained a guard: two manual script rules both have an
  empty read key and an empty action key, which compared equal and
  logged a cascade warning that cannot exist.
* `_resolve_watch` short-circuits on an empty watch property. Without
  it, resolving `""` against a KNOWN catalog type raises — so a manual
  rule would pass on the memory backend and break on every real space.
  Pinned at the adapter layer, where it is visible.

### Accepted limitations

* The seeded explainer body does not retrofit. `_seed_example_rule`
  runs only when the type is minted, so spaces bootstrapped before this
  keep the old text; the property retrofits (and its display name has
  to carry its own meaning there). `automation action='list'` and bare
  `/run` carry the new vocabulary instead.
* `manual` + `modified_at` is rejected (the built-in watch requires
  condition `changed`), as is `manual` + `uncheck others of type`
  (which forces `changed to true`). Both are loud.
* The checkbox gesture names no trigger, so it takes the first object
  of the target type by id. For a formula rule reading the whole graph
  this is immaterial; for a per-object action, `/run <rule> on
  <object>` is the addressed form.
* `/run` resyncs first — an explicit command should run against what
  the user can see, and the change tick only refreshes every few
  seconds. A failed resync degrades rather than refusing.
