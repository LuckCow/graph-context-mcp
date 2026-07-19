# ADR 033: Per-mode model selection

Date: 2026-07-17
Status: accepted (amended 2026-07-17 by ADR 035: of the "three
mode-config sources" below, only the in-space Activity Mode object
remains live — a seed TOML can pre-fill the field)

## Context

One deployment model (`GC_DRIVER_MODEL`, or the driver's own default)
serves every activity mode. Dogfooding wants to spend capability where
it pays: a heavyweight research or authoring mode on Opus 4.8 or
Fable 5, quick organizing turns on Sonnet 5 — without restarting the
bot or splitting deployments. Which model an activity runs on is a
property of the *activity*, exactly like its live-activity verbosity
(ADR 029) and web-search admission (ADR 030).

## Decision

**The model is a MODE property, default unset.** `ModeSpec.model` holds
one of the canonical choices — `sonnet 5`, `opus 4.8`, `fable 5` —
settable in all three mode-config sources: profile specs,
`GC_MODES_FILE` (`model = "opus 4.8"`), and the in-space Activity Mode
object via a new `gc_mode_model` **select** property (minted/
retrofitted by bootstrap like every `MODE_PROPERTIES` entry, options
pre-seeded per the WP19 select rule: Title-Case display names,
lowercased on read; the explainer body documents it). Empty means "the
deployment default" — existing setups see zero change.

**The vocabulary is domain-homed** (`domain/model_choice.py`, the
ADR 029 `activity.py` pattern): `MODEL_CHOICES` maps each choice to the
full provider model id (`claude-sonnet-5`, `claude-opus-4-8`,
`claude-fable-5`) that both driver paths accept — the Claude Code CLI
and the Messages API take full ids alike. The interface validates specs
against it (an unknown model fails loudly at load, naming the config
source), the Anytype adapter mints the select options from it, and the
pipeline resolves the choice through `model_id()`.

**The override rides `decide()`** — the ADR 030 keyword pattern: the
pipeline passes `model=model_id(spec.model)` on every decision (one
driver instance serves many spaces/modes, so a constructor knob would
be wrong). Both drivers treat it as a per-decision override of their
configured default: `ClaudeAgentDriver` hands it to the session options
(`model or self._model`), `AnthropicDriver` uses the effective model
for the request AND for picking the web-search tool variant. Modelless
drivers (scripted, manual) ignore it.

**Attribution stamps the truth**: a mode-pinned model is what actually
generated the turn, so `gc_model` on intent nodes records the resolved
id; only unpinned modes stamp the deployment default. `/mode` reports
`model: <choice|default>`.

## Consequences

- Switching modes switches models mid-chat; no restart, no second
  deployment. Persisted per-chat modes (ADR 021) make the pinning
  sticky per conversation.
- Cost rides the driver choice as before: subscription quota on the
  default path, per-token API billing on `anthropic_api` — an Opus/
  Fable-pinned mode spends faster on both.
- The choice list is a deliberate, small curation. A new model family
  is a one-line `MODEL_CHOICES` addition; the select options seed on
  the next startup (create-only — human renames of existing options
  are never clobbered).
- The turn diary's prompt fingerprint does not include the model, so a
  model-only mode edit re-logs nothing; the intent stamp and the /mode
  line are the observable record.
