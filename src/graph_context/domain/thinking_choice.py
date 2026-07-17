"""Per-mode thinking level (ADR 037): the shared vocabulary.

How hard a mode's decisions think. One knob covers the API's two
parameters: a LEVEL means adaptive thinking plus that ``output_config``
effort; ``off`` disables thinking outright; empty means "not set" -- the
deployment's configured effort (``GC_DRIVER_EFFORT``, or the model's own
default) applies with adaptive thinking. Domain-homed like the model and
activity-detail vocabularies so every layer shares one spelling: the
interface validates ``ModeSpec.thinking`` against it, the Anytype
adapter mints the mode type's ``gc_mode_thinking`` select from it, and
the drivers translate the choice into request parameters.

The canonical spellings are lowercase; the minted select options are
their ``str.capitalize`` forms ("Xhigh"), and the mode loader lowercases
on read, so the round trip is exact (the WP19 select rule).
"""

from __future__ import annotations

THINKING_OFF = "off"

# ``off`` first, then effort levels in ascending depth (the mint order
# of the select options humans see).
THINKING_LEVELS: tuple[str, ...] = (
    THINKING_OFF, "low", "medium", "high", "xhigh", "max",
)


def thinking_effort(choice: str) -> str:
    """A canonical choice -> the effort level it implies; ``off`` and
    empty imply none (the deployment/model default applies)."""
    return "" if choice == THINKING_OFF else choice
