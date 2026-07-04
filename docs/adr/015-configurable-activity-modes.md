# ADR 015: Activity modes are data; capture is configurable

**Status:** Accepted (2026-07-04) — WP12; extends ADR 007's modes and
ADR 008's capture beyond fiction; builds on WP5's profiles

## Context

The storage layer generalized long ago: the space-reflecting schema
(ADR 006) makes `task` and `procedure` first-class the same way
`character` is, WP10 reflects native attributes both ways, and WP11's
semantic search is domain-neutral. What remains fiction-shaped is
concentrated in the *behavior* layer:

- **Modes are a hardcoded enum.** WP6 ships exactly `world_modeling` and
  `authoring`, with bindings in a literal table. "Authoring" bakes in a
  fiction assumption: that the one non-modeling activity is writing prose.
- **Capture is hardcoded three ways**: the artifact is always a
  `gc_prose` node, the framing is always "rendered prose," and the
  trigger is one constant. A work assistant wants *Record Procedure* —
  "notate each step I take so it can be repeated" — whose artifact is a
  native `procedure` node, not prose.
- **The time axis is fiction-keyed.** `gc_story_time` is a bare number;
  the workspace profile awkwardly instructs "read it as epoch seconds"
  while the space's real `event_date`/due-date properties (readable and
  writable since WP10a) sit unused by `as_of`.
- **Vocabulary leaks**: `Role.PROSE` as a concept name, "prose" in
  docstrings and demo framing.

The `record_prose` tool is already gone (ADR 008 amendment): capture is
exclusively the harness's job, which means the harness's configuration IS
the capture surface. And the LangGraph driver has not landed yet — so the
mode system can grow its goal-prompt slot *before* anything depends on
its current shape.

## Decision

1. **A mode becomes a `ModeSpec` — data, not an enum member:**

   ```
   ModeSpec:
     name            # "world_modeling", "authoring", "record_procedure", ...
     goal            # the system-prompt fragment handed to the driver
     mutating        # binds the full surface, or read-only + context
     capture         # optional CapturePolicy:
       artifact_type    # native type key or gc_prose
       references_label # default "references"
       min_chars        # substantiality threshold
   ```

   WP6's enum and binding table become the *loader's output*: profiles
   ship default specs (fiction: `world_modeling` + `authoring` exactly as
   today; assistant: `record_procedure`, `meeting_notes`, …), and the
   binding-boundary mechanism ("unavailable, not refused"), per-session
   `/mode` switching, and the tool-budget loop carry over unchanged —
   specs only change what fills the tables. `/mode` lists whatever specs
   the deployment loaded.

2. **`ProseRecorder` generalizes to `CaptureRecorder`.** The artifact
   type, references label, and threshold come from the active mode's
   `CapturePolicy`; `gc_prose` is just the fiction default. Journal
   integration is untouched — a captured procedure journals itself and
   the turn's intent node links it, exactly like prose today. Artifacts
   of native types are ordinary story/work nodes (visible in traversal);
   only `gc_prose` retains the infra-role hiding.

3. **Configuration source, staged:** profiles ship default ModeSpecs in
   code (they are prompt engineering and get golden tests, like
   docstrings); a user file (`GC_MODES_FILE`, TOML) can add or override
   specs per deployment. **In-space configuration** — mode definitions as
   Anytype objects a human edits like everything else — is the stated
   direction, deferred with WP5's per-space-profile open question (they
   are the same feature).

4. **The time axis is profile-declared.** A profile names which property
   is the Event-role timeline: fiction keeps `gc_story_time` (a number);
   an assistant profile names a native date property (ISO strings —
   which sort lexicographically, so `as_of` comparison generalizes to
   "ordered timeline value"). The `story_time` mechanism stays; only its
   source and rendering are profile words.

5. **Vocabulary follows the profile.** `Role.PROSE` renames conceptually
   to `Role.CAPTURE` (the `gc_prose` type key stays frozen for existing
   spaces); presenter and docstring fragments that say "prose" become
   profile-supplied words. A dogfooded `assistant` profile
   (tasks/procedures/notes) replaces guesswork in the current
   `workspace` framing where transcripts show it wrong.

## Consequences

- "Record Procedure" is a config entry, not a feature: a ModeSpec with a
  goal prompt, read-only-or-not binding, and `artifact_type: procedure`.
  Users invent activities the authors didn't.
- The LangGraph driver lands against a mode system that already supplies
  its system-prompt fragment per mode — no rework when the rebuild ships.
- Native-typed artifacts participate in the graph as first-class nodes
  (searchable via WP11, footered via ADR 013, attributed via ADR 008);
  the capture pipeline stops being a fiction cul-de-sac.
- Two config surfaces exist until in-space config lands (code defaults +
  user file); the loader must make precedence obvious and validated —
  specs are prompts, so bad ones fail loudly at startup, not mid-turn.
- The timeline change touches the domain's one typed timeline value;
  ordering semantics ("comparable, ascending = later") become the
  contract instead of "float".
