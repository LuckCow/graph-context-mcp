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

# Mode-config property keys (ADR 045): since the meta-inspection
# privilege made Activity Mode objects a readable/writable surface, these
# reflect into ``Node.fields`` like the scheduling/rule/attribution keys
# -- so their spellings are domain-homed the same way, and the Anytype
# adapter's ``mapping`` aliases them.
FIELD_MUTATING = "gc_mode_mutating"
FIELD_META_INSPECTION = "gc_mode_meta_inspection"
FIELD_ACTIVITY_DETAIL = "gc_mode_activity_detail"
FIELD_WEB_SEARCH = "gc_mode_web_search"
FIELD_MODEL = "gc_mode_model"
FIELD_THINKING = "gc_mode_thinking"
FIELD_MAX_TOKENS = "gc_mode_max_tokens"
FIELD_TURN_LIMIT = "gc_mode_turn_limit"
FIELD_SEARCH_MAX_USES = "gc_mode_search_max_uses"
FIELD_SEARCH_ALLOWED = "gc_mode_search_allowed_domains"
FIELD_SEARCH_BLOCKED = "gc_mode_search_blocked_domains"
FIELD_CAPTURE_TYPE = "gc_capture_type"
FIELD_CAPTURE_REFERENCES = "gc_capture_references"
FIELD_CAPTURE_MIN_CHARS = "gc_capture_min_chars"

MODE_CONFIG_FIELDS: dict[str, str] = {  # key -> format; bootstrap mints these
    FIELD_MUTATING: "checkbox",
    FIELD_META_INSPECTION: "checkbox",
    FIELD_ACTIVITY_DETAIL: "select",
    FIELD_WEB_SEARCH: "checkbox",
    FIELD_MODEL: "select",
    FIELD_THINKING: "select",
    FIELD_MAX_TOKENS: "number",
    FIELD_TURN_LIMIT: "number",
    FIELD_SEARCH_MAX_USES: "number",
    FIELD_SEARCH_ALLOWED: "text",
    FIELD_SEARCH_BLOCKED: "text",
    FIELD_CAPTURE_TYPE: "text",
    FIELD_CAPTURE_REFERENCES: "text",
    FIELD_CAPTURE_MIN_CHARS: "number",
}
