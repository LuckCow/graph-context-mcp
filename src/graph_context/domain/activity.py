"""Live-activity detail levels (WP19, ADR 029): the shared vocabulary.

How much of a running turn the orchestrator streams into the chat. The
level is a property of the activity MODE (``ModeSpec.activity_detail``);
what each level shows is interpreted by the activity renderer
(``orchestrator/turn_activity.py``) and nowhere else.

Domain-homed like ``scheduling``'s status values so every layer shares
one spelling: the interface validates specs against it, and the Anytype
adapter mints the mode type's ``gc_mode_activity_detail`` select with
exactly these options -- humans pick from a dropdown instead of typing
the enum.
"""

from __future__ import annotations

ACTIVITY_DETAIL_LEVELS = ("off", "minimal", "tools", "full")
DEFAULT_ACTIVITY_DETAIL = "minimal"
