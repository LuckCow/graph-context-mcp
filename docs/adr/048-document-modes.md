# ADR 048: Document modes — the manuscript lives in a node, not the chat

Date: 2026-07-27
Status: accepted

## Context

Dogfooding a chapter-writing capture mode (`gc_capture_type: "Chapter"`,
the user's ProseWeaver) surfaced two structural problems with using
capture for iterative long-form writing:

1. **The chat floods.** Capture (ADR 008/015) works by copying the
   model's REPLY into a node — so the model must paste the whole chapter
   into chat for the harness to store it. A few revision rounds drown
   the conversation in prose.
2. **Iteration mints duplicates.** `CaptureRecorder` CREATES a node per
   qualifying reply; revising a chapter five times leaves five Chapter
   objects, when the user wants one chapter whose body evolves.

Capture also bypasses `NodeWriter`, so its writes skip the journal-based
carding/suppression semantics (ADR 046) and the type-scoped resolution
discipline (ADR 047) — the fix for the ADR 047 capture regression
(attribution stamps exempted BY KEY) was a symptom of prose living on
the wrong write path.

## Decision

**A mode that maintains long-form documents declares a
`gc_mode_document_type` (text, e.g. `Chapter`), and the MODEL maintains
the document node itself through the normal write tools.** Capture is
retired for such modes — `document_type` and `capture` on one mode is a
spec validation error; `CapturePolicy` survives unchanged for
note/procedure assistants whose replies genuinely are the artifact.

* **`ModeSpec.document_type`** requires `mutating`. Plumbing rides the
  checkbox precedent end to end: `activity.MODE_CONFIG_FIELDS` (mint +
  retrofit + ADR 045 reflection for free), `spec_from_mapping`
  validation, store payload, seed-TOML key, ADR 044 auto-refresh, Space
  Setup menu documentation.
* **The flow**: first draft = `create_node(type=<document_type>,
  description=<text>)`; revision = `update_node(description=)` on the
  SAME node (wholesale body replace, ADR 010); the model curates the
  document's `references` property itself — replacing capture's
  regex-scraped `entity_links` with curated links. All document writes
  flow through `NodeWriter`: journal, attribution via the intent node,
  ADR 045/046/047 semantics apply with no special cases.
* **Standing guidance, one place**: `modes.goal_for(spec)` appends a
  `DOCUMENT_GUIDANCE` block to the mode goal (write via tools; reply =
  short change summary + a markdown link; never paste the document into
  chat). The pipeline routes its three goal consumers (prompt
  fingerprint, diary, `decide`) through `goal_for`, so the diary never
  lies about what the model saw.
* **Carding backstop**: after `_finish_turn`, the pipeline stamps the
  ids of drained mutations whose type matches `spec.document_type` onto
  `ReplyEvent.attach`, beside the ADR 038 intent id. Explicit attach
  rides ahead of the transport's text-scrape and is never filtered by
  `hide_node_cards` — the reply only summarizes, so the card IS the
  link. Reply discipline itself stays prompt-enforced (considered and
  rejected: a harness stub that rewrites oversized replies — the user
  prefers prompt + evals over harness-rewritten conversation memory).
* **`_finish_turn` drains the journal unconditionally** (previously
  only when provenance was wired): carding and suppression are display
  concerns and must not depend on the provenance toggle.

## Consequences

* Chat stays conversational: a document turn reads as "Drafted Chapter
  Three — cut the harbor scene, tightened the reveal" plus a card.
* One chapter = one node whose body evolves; revision history over that
  body is ADR 049's subject (the two ADRs land together — `document
  type` is the ergonomics knob, tracking is space-level data config).
* Prompt-enforced reply discipline can be disobeyed; the failure mode
  is a pasted chapter in chat (annoying, not corrupting). Tighten the
  mode goal or pin with an eval (`/evals-add`) if observed.
* The bare MCP server gets the same tools but no mode goals; document
  discipline there is the client's prompt problem.

## Migration (ProseWeaver)

Edit the mode object in Anytype: clear `gc_capture_type` /
`gc_capture_references` / `gc_capture_min_chars`; set
`gc_mode_document_type` = `Chapter`; add `Chapter` (and other types
worth versioning) to the Space Context's **Tracked types** (ADR 049);
replace the goal with:

> You are ProseWeaver, a chapter-writing collaborator. The manuscript
> lives in Chapter objects in this space, never in chat. When we start a
> new chapter, create one Chapter node and write the draft into its
> description; when we revise, update that same node's description with
> the full revised text. Keep the chapter's `references` property
> pointing at the characters, locations and events that appear in it. In
> chat, reply only with a short summary of what you wrote or changed and
> why — a few sentences — plus a markdown link to the chapter node.
> Never paste chapter prose into the chat.

ADR 044 applies the edit within one change tick.
