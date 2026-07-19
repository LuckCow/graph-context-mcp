"""Per-mode model choice (ADR 033): the shared vocabulary.

Which Claude model a mode's decisions run on. Like the activity-detail
levels, domain-homed so every layer shares one spelling: the interface
validates ``ModeSpec.model`` against it, the Anytype adapter mints the
mode type's ``gc_mode_model`` select from it (humans pick from a
dropdown), and the pipeline resolves the choice to the provider model id
it hands the driver. Empty means "not set" -- the deployment's
configured default model (``GC_DRIVER_MODEL``, or the driver's own
default) applies.

The canonical spellings are lowercase; the minted select options are
their ``str.capitalize`` forms ("Sonnet 5"), and the mode loader
lowercases on read, so the round trip is exact (the WP19 select rule).
"""

from __future__ import annotations

# choice -> the provider model id both driver paths accept (the Claude
# Code CLI and the Messages API take full model ids alike).
MODEL_CHOICES: dict[str, str] = {
    "sonnet 5": "claude-sonnet-5",
    "opus 4.8": "claude-opus-4-8",
    "fable 5": "claude-fable-5",
}


def model_id(choice: str) -> str:
    """A canonical choice -> its provider model id; empty stays empty
    (= the deployment default). Unknown choices cannot reach here --
    ``ModeSpec`` validation rejects them at spec load."""
    return MODEL_CHOICES[choice] if choice else ""


def thinking_locked(model: str) -> bool:
    """Whether ``model`` (a provider id) refuses to turn thinking off.

    The Fable/Mythos line runs with thinking always on -- an explicit
    ``disabled`` is rejected with a 400 -- so a mode pinning one of
    these cannot also pick ``thinking = off`` (ADR 037). Prefix match
    covers dated snapshots and point releases."""
    return model.startswith(("claude-fable", "claude-mythos"))
