# ADR 046: Reply-card visibility is a mode property (hide intent / hide touched-node cards)

Date: 2026-07-25
Status: accepted

## Context

Every Anytype reply can carry object cards (C7: text is plain, cards are
the clickable surface). Two sources feed them: ADR 038 stamps the turn's
intent (process-trace) node on the reply via `ReplyEvent.attach`, and the
transport scrapes object ids the reply text mentions — in practice, the
nodes the turn just created or edited. That is the right default for
authoring modes, but dogfooding shows it is noise in others: a
conversational mode that touches a node per turn buries the chat in
cards, and the process-trace card is uninteresting in modes whose work is
routine. Verbosity of this kind is already mode-shaped configuration
(`activity_detail`, ADR 029); card visibility should be too.

## Decision

**Two Activity Mode checkboxes control what a reply cards; both default
to showing (the pre-046 behavior).**

* **`ModeSpec.hide_intent_card`** (`gc_mode_hide_intent_card`): the
  pipeline skips stamping the intent node on the reply's `attach`. The
  intent node is still RECORDED — ADR 038's durable record is not a
  display choice; only the card goes.
* **`ModeSpec.hide_node_cards`** (`gc_mode_hide_node_cards`): the turn's
  created/edited node ids (the drained `MutationRecord`s — the same
  facts provenance uses) ride the events as a new transport-neutral
  **`ReplyEvent.suppress`** tuple; the Anytype transport skips them when
  scraping `object_references` from the text. The text keeps naming the
  nodes — only the cards go. Nodes a reply merely *mentions* (reads)
  still card: the toggle hides the turn's own writes, not references.
* **Plumbing follows the `web_search` checkbox precedent**: keys/formats
  in `activity.MODE_CONFIG_FIELDS` (so bootstrap mints + retrofits and
  ADR 045 reflection come free), `spec_from_mapping` validation, store
  payload + seeder round trip, seed-TOML pre-fill (ADR 035), auto-
  refresh on edit (ADR 044). `_finish_turn` now returns
  `(intent, mutations)`; explicit `attach` is never filtered by
  `suppress` — the pipeline owns both stamps and never contradicts
  itself. Transports without a card surface (Discord/CLI/MCP) ignore
  `suppress` like they ignore `attach`.

## Consequences

* A space owner tunes card noise per mode in the Anytype UI, live within
  one change tick — no restart, no code.
* Both toggles off = byte-identical pre-046 behavior; deployments
  without provenance wired have no mutation record to suppress, so
  `hide_node_cards` is inert there (cards were the model's text
  references all along).
* The Space Setup mode's menu and the Example Mode explainer document
  both fields, so the interview can offer them.
