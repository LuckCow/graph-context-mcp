# ADR 037: Mode-level driver options — thinking, output cap, search limits

Date: 2026-07-17
Status: accepted

## Context

The deployment is switching its default driver to the raw Messages API
(`anthropic_api`), which exposes request options the orchestrator has so
far hardcoded or ignored. An audit found:

* **Thinking was a fixed constant.** The API driver always sent
  `thinking: {"type": "adaptive"}` with no `display`, so on current
  models thinking blocks arrived with EMPTY text (`display` defaults to
  `omitted`) — the turn diary recorded nothing readable. Depth
  (`output_config.effort`) was only configurable deployment-wide via
  `GC_DRIVER_EFFORT`.
* **`max_tokens` was hardcoded** (`DEFAULT_MAX_TOKENS = 16000`), with no
  per-mode or even env override.
* **The web-search tool never carried its own limits** even though the
  driver already picks the dynamic-filter variant whose point is domain
  filtering: `max_uses`, `allowed_domains`, `blocked_domains` all unset.
* **Per-decide usage was discarded in production**: `on_result` (tokens,
  cache stats, subscription cost) was only ever wired by the eval
  harness.

Current-model API facts that shape the design (verified 2026-07-17):
thinking is adaptive-only (`budget_tokens` 400s); depth rides
`output_config.effort` (`low…max`); `thinking: {"type": "disabled"}` is
valid on Opus 4.8/Sonnet 5 but **400s on Fable 5/Mythos** (always-on);
omitting `thinking` means off on Opus 4.8 but adaptive on Sonnet 5 — so
requests must always be explicit; `display: "summarized"` is required
for readable thinking text; the search tool takes at most ONE of
allowed/blocked domains per request.

## Decision

**Five new Activity Mode properties, one value object to the drivers.**

* `gc_mode_thinking` — a select: `Off | Low | Medium | High | Xhigh |
  Max`; the EMPTY select is the default (no "Default" option, the
  `model`/`activity_detail` unset idiom). A level means adaptive
  thinking at that effort; `Off` disables thinking; vocabulary in
  `domain/thinking_choice.py` (`THINKING_LEVELS`), options minted from
  it by bootstrap like every mode select.
* `gc_mode_max_tokens` (number), `gc_mode_search_max_uses` (number),
  `gc_mode_search_allowed_domains` / `gc_mode_search_blocked_domains`
  (text, comma/whitespace-separated) — all "zero/empty = not set".
* `ModeSpec` gains the matching fields; validation at spec load fails
  loudly naming the mode for: an unknown level, `off` with a pinned
  Fable/Mythos model (`model_choice.thinking_locked`), negative counts,
  or BOTH domain lists set. Search limits are inert when `web_search`
  is off (erroring would brick the mode object on load).
* **`DecideOptions`** (frozen, `orchestrator/drivers.py`) replaces the
  growing kwarg list on `decide()`: web_search + its limits, model,
  thinking, max_tokens, built by `modes.decide_options(spec)` (which
  resolves the model choice to a provider id). A new option is one field
  plus the drivers that honor it.
* **API driver** (`thinking_params`): precedence mode level >
  `GC_DRIVER_EFFORT` > model default. Adaptive is always explicit and
  always `display: "summarized"` — the diary, activity stream, and
  intent-node trace (ADR 038) get real summaries. `off` sends
  `disabled`, guarded again at the driver against a Fable/Mythos
  EFFECTIVE model (covers a deployment default the spec never saw).
  `max_tokens` and the search-limit keys apply only when set.
* **Subscription driver, best effort:** a thinking level maps onto
  `ClaudeAgentOptions.effort` (the same five names); `off`,
  `max_tokens`, and search limits have no SDK surface — skipped with
  one process-wide warning per combination (`inexpressible_options`).
  The API driver is the full implementation.
* `/mode` reports `thinking: <level|default>` plus the other knobs
  only-if-set.
* **Usage stops being discarded**: `bootstrap.build_driver` wires
  `on_result` to the turn diary (`TurnLog.usage`) whenever the diary is
  on — per-decide tokens, cache stats, and (subscription) dollars now
  land as `usage` lines.

## Consequences

* Humans tune thinking depth, output caps, and search scope per mode in
  the Anytype UI; a `/mode` reload applies edits, seed TOMLs can
  pre-fill all five keys.
* Every adaptive request now carries `display: "summarized"` — thinking
  text downstream is real. Marginal cost: the summary tokens are billed
  as output either way; visibility does not change billing.
* Known discards left for later (documented, not captured):
  `redacted_thinking` blocks, thinking `signature` (matters only if
  thinking ever replays cross-turn), response-level `id`/`model`,
  `usage.server_tool_use` counts.
* The eval runner's `[[case.modes]]` overlay still exposes only
  name/goal/mutating — the new properties share the existing gap with
  `web_search`/`model`.
