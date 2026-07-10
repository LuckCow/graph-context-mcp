# ADR 012: Native scalar properties reflect into fields, with a noise filter

**Status:** Accepted (2026-07-02) — WP10a; completes the attribute half of
ADR 006's space-reflecting model. **Amended by ADR 023 (2026-07-10):**
the write-side blob fall-through is retired for story nodes — an
unmatched `fields` key now errors, with `create_missing_fields` as the
explicit path to a new native property; `gc_fields` is written for
infra-role nodes only (read-compat merge unchanged).

## Context

The space-reflecting pivot (ADR 006) reflected *types* and *object
relations* but not scalar *attributes*: `to_node` read only our `gc_`
properties, so every native property a human maintained in the UI was
invisible to the LLM. This is not hypothetical — a census of the real
story space (78 objects) found 10+ human-created `select` properties
(`role`, `tech_type`, `org_type`, `narrative_status`, …) plus text/date
attributes (`real_life_inspiration`, `notes`, `themes`, `event_date`),
none of which the model could see. Meanwhile the bot's own attribute
channel is `gc_fields`, a JSON blob invisible to humans — the two sides
literally cannot see each other's attributes.

The same census showed the noise is small and consistent: every object
carries system timestamps (`created_date`, `last_modified_date`,
`added_date`, `last_opened_date`) that would pollute the LLM's context
window if reflected verbatim. (`creator_origin`, which looked
system-flavored, turned out to be a user relation — evidence beats
name-vibes; the filter must be a curated list, not a heuristic.)

Write-side spikes (2026-07-02, GC-E2E, live):

- PATCHing a `select`/`multi_select` with a bare string **400s**
  ("invalid select option") — options must pre-exist.
- Options are **"tags"**: `GET/POST
  /spaces/{space}/properties/{propertyId}/tags` works (the `/options`
  route does not exist). Creating a tag and PATCHing immediately
  succeeded.
- The PATCH `select` value accepts the tag **id or key**; the stored
  value reads back as an inline envelope
  (`"select": {"id", "key", "name", "color"}`).
- The tags routes require the property **id**, not its key — so the
  registry must carry property ids.
- Native `text`/`number`/`url` properties PATCH directly, as expected
  (the API tolerates any space-level property in PATCH).

## Decision

1. **Read side: `to_node` reflects native scalar properties into
   `Node.fields`**, normalized to strings — `select` → the option's
   display name, `multi_select` → comma-joined names, others via `str`.
   Empty/falsy values are skipped. Excluded from reflection:
   - `objects`-format properties (they are edges),
   - all `gc_`-prefixed keys (first-class or retired),
   - the built-in `description` (the summary channel, ADR 011),
   - the **system-noise denylist** below.
2. **The noise filter is a curated denylist with a user knob.**
   `mapping.SYSTEM_PROPERTY_DENYLIST` holds the census-confirmed system
   keys (`created_date`, `last_modified_date`, `added_date`,
   `last_opened_date`, `last_used_date`); the composition root extends it
   from **`GC_FIELD_DENYLIST`** (comma-separated keys), threaded like
   `role_overrides`, so any space-specific noise can be silenced without
   a code change. Curated-list-over-heuristic is deliberate
   (`creator_origin` proves name-shape lies).
3. **Write side: `fields` writes route to native properties when the key
   matches one** (by key or display name), else fall through to the
   `gc_fields` blob exactly as before. Formats `text`/`number`/`url`/
   `email`/`phone`/`date`/`checkbox` write directly; `select`/
   `multi_select` resolve the value against the property's tags by
   name/key, **creating the tag when missing** (bulldoze stance: options
   are cheap, approval ceremony is not) and logging the creation.
4. **Native wins over the blob on read** when both carry the same key —
   the human-visible surface is authoritative; the blob remains the
   channel for keys with no native property.
5. The in-memory fake keeps its open, blob-free `fields` semantics;
   reflection is adapter-read behavior (like `links`-mirror handling),
   tested in `tests/anytype`, not the contract suite.

## Consequences

- The LLM finally sees the attribute layer humans maintain in Set views —
  and can update it through the same `fields` parameter it already knows,
  with values landing where humans filter and sort.
- The `gc_fields` blob is now legacy-shaped: still written for unmatched
  keys, still read, but every attribute a human promotes to a real
  property automatically migrates the *channel* on the next write. Full
  blob retirement stays out of scope (existing worlds use it).
- Context-window cost is bounded by the denylist: only human-meaningful
  properties reach `fields`. A noisy space has `GC_FIELD_DENYLIST`.
- The registry carries property ids and formats for all properties (not
  just `objects`-format), and tag lookups add one GET per select write
  (unthrottled, S7).
- Out-of-band attribute edits reach the index via resync like any
  property change; last-write-wins stands.
