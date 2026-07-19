"""Domain profiles: the deployment's *framing* of the graph (WP5).

DEPRECATED (ADR 035 / WP27). Profiles are a transitional framing layer on
the way out: activity modes already moved in-space (the profile's mode
specs are GONE -- the space's Activity Mode objects are the only live
source, with ``interface/mode_config.py`` seeding starters). What remains
here keeps working until WP27 retires it: ``tool_docs`` will collapse to
one neutral code-owned set; ``role_overrides`` variance will be dropped;
``time_property``/``time_format`` will be REPLACED by a redesigned
general-purpose timeline feature (not migrated as-is); ``ranking``
variance moves to deployment config. Do not add new profile fields --
new configuration belongs in the space (Space Context, ADR 034) or in
deployment config.

The schema is space-reflecting and domain-neutral (ADR 006); what actually
differs between a story world and a work knowledge base is framing — the
tool docstrings (which are prompts, WP2), their worked examples, and which
native type keys map to semantic roles. Storage keys (``gc_story_time``,
``gc_prose``, …), tool names, and parameter names are frozen across
profiles: a profile changes words, never wire format.

The composition root selects a profile from ``GC_PROFILE`` (default
``fiction``), registers each tool with the profile's docstring, and passes
``role_overrides`` into the repository. Editing these strings IS prompt
engineering — the snapshot tests in ``tests/interface/test_profiles.py``
pin the assembled output so every change shows up as a reviewable golden
diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graph_context.application.ranker import RankingWeights
from graph_context.domain.activity import (
    ACTIVITY_DETAIL_LEVELS,
    DEFAULT_ACTIVITY_DETAIL,
)
from graph_context.domain.model_choice import (
    MODEL_CHOICES,
    model_id,
    thinking_locked,
)
from graph_context.domain.schema import FIELD_FORMATS, Role
from graph_context.domain.session import (
    DEFAULT_FULL_SLOTS,
    DEFAULT_SUMMARY_SLOTS,
    SCRATCHPAD_MAX_CHARS,
)
from graph_context.domain.thinking_choice import THINKING_LEVELS, THINKING_OFF
from graph_context.errors import GraphContextError

TOOL_NAMES: tuple[str, ...] = (
    "context",
    "create_node",
    "update_node",
    "get_node",
    "explore",
    "find_path",
    "find_node",
    "query",
    "schedule",
    "automation",
    "send_file",
    "schema",
)


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """What an activity mode's auto-capture produces (ADR 015).

    ``artifact_type`` is a type identifier the space must resolve
    (``gc_prose`` for fiction prose; a native type like ``procedure`` for
    an assistant). Native-typed artifacts are first-class nodes -- only
    ``gc_prose`` keeps the infra-role hiding.
    """

    artifact_type: str = "gc_prose"
    references_label: str = "references"
    min_chars: int = 200


# WP19 (ADR 029): ACTIVITY_DETAIL_LEVELS / DEFAULT_ACTIVITY_DETAIL come
# from domain.activity (imported above) -- how much live turn activity a
# mode streams into the chat. A MODE property (not a session setting):
# picking a mode picks its verbosity; the vocabulary is domain-homed so
# the Anytype adapter can mint the select options from it.


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """One activity mode: data, not an enum member (ADR 015).

    ``goal`` is the system-prompt fragment handed to the LLM driver --
    specs are prompts and get the golden-test review bar. ``mutating``
    picks the tool binding (full surface vs read-only + context);
    ``capture`` enables harness-side auto-capture of substantial replies;
    ``activity_detail`` sets how much live progress a turn streams into
    the chat (WP19, ADR 029); ``web_search`` admits the provider's
    server-side web search tool for this mode's decisions (WP20, ADR 030
    -- executed on Anthropic's servers, never by the harness; default off
    so graph-grounded modes stay graph-grounded); ``model`` pins which
    Claude model runs this mode's decisions (ADR 033 -- a canonical
    ``MODEL_CHOICES`` name; empty = the deployment's configured default).

    ADR 037 driver options -- all "empty/zero = not set", API-driver
    surfaces (the subscription driver maps what it can and documents the
    rest): ``thinking`` is a ``THINKING_LEVELS`` choice (a level implies
    adaptive thinking at that effort; ``off`` disables thinking -- and
    is rejected here when the mode pins a Fable/Mythos model, where
    thinking cannot be turned off); ``max_tokens`` caps one decision's
    output; ``web_search_max_uses`` / ``web_search_allowed_domains`` /
    ``web_search_blocked_domains`` bound the server-side search tool
    (inert unless ``web_search`` is on; the API takes at most ONE of the
    domain lists per request, so setting both is a spec error).
    """

    name: str
    goal: str
    mutating: bool = False
    capture: CapturePolicy | None = None
    activity_detail: str = DEFAULT_ACTIVITY_DETAIL
    web_search: bool = False
    model: str = ""
    thinking: str = ""
    max_tokens: int = 0
    web_search_max_uses: int = 0
    web_search_allowed_domains: tuple[str, ...] = ()
    web_search_blocked_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.name.replace("_", "").isalnum():
            raise ValueError(f"mode name must be a slug, got {self.name!r}")
        if not self.goal.strip():
            raise ValueError(f"mode {self.name!r} needs a non-empty goal prompt")
        if self.activity_detail not in ACTIVITY_DETAIL_LEVELS:
            raise ValueError(
                f"mode {self.name!r} has unknown activity_detail "
                f"{self.activity_detail!r}; allowed: "
                f"{', '.join(ACTIVITY_DETAIL_LEVELS)}"
            )
        if self.model and self.model not in MODEL_CHOICES:
            raise ValueError(
                f"mode {self.name!r} has unknown model {self.model!r}; "
                f"allowed: {', '.join(MODEL_CHOICES)}"
            )
        if self.thinking and self.thinking not in THINKING_LEVELS:
            raise ValueError(
                f"mode {self.name!r} has unknown thinking {self.thinking!r}; "
                f"allowed: {', '.join(THINKING_LEVELS)}"
            )
        if self.thinking == THINKING_OFF and thinking_locked(model_id(self.model)):
            raise ValueError(
                f"mode {self.name!r} sets thinking = off but pins "
                f"{self.model!r}, which cannot turn thinking off"
            )
        if self.max_tokens < 0 or self.web_search_max_uses < 0:
            raise ValueError(
                f"mode {self.name!r}: max_tokens and web_search_max_uses "
                "must be non-negative (0 = not set)"
            )
        if self.web_search_allowed_domains and self.web_search_blocked_domains:
            raise ValueError(
                f"mode {self.name!r} sets both allowed and blocked search "
                "domains; the search tool takes at most one list"
            )


@dataclass(frozen=True, slots=True)
class DomainProfile:
    """One deployment's framing: prompt text and roles.

    DEPRECATED (ADR 035 / WP27): activity modes left this dataclass for
    the space's Activity Mode objects; every remaining field is on the
    WP27 retirement list (see the module docstring).
    """

    name: str
    description: str
    # DEPRECATED (WP27): collapses to one neutral code-owned set.
    tool_docs: Mapping[str, str]
    # DEPRECATED (WP27): variance dropped; only Role.EVENT ever changed
    # behavior (timeline), and the timeline itself is being redesigned.
    role_overrides: Mapping[str, Role]
    # The Event-role timeline source (ADR 015): a property key + format.
    # Fiction keeps the gc_story_time number; a date-axis profile names a
    # native date property (ISO strings order lexicographically).
    # DEPRECATED (WP27): replaced by a redesigned general-purpose
    # timeline feature, not migrated as-is -- the seam to preserve is
    # that the pair flows as a parameter through composition.build_runtime.
    time_property: str = "gc_story_time"
    time_format: str = "number"
    # Ranking signal weights (ADR 016) -- data, tuned against the eval
    # golden. Fiction leaves recency at zero; the assistant raises it.
    # DEPRECATED (WP27): moves to deployment config; the seam to preserve
    # is Ranker taking RankingWeights via constructor.
    ranking: RankingWeights = RankingWeights()

    def __post_init__(self) -> None:
        missing = set(TOOL_NAMES) - set(self.tool_docs)
        extra = set(self.tool_docs) - set(TOOL_NAMES)
        if missing or extra:
            raise ValueError(
                f"profile {self.name!r} tool_docs mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )


def get_profile(name: str | None) -> DomainProfile:
    """Resolve ``GC_PROFILE`` (or an explicit name) to a profile.

    DEPRECATED (ADR 035 / WP27): profiles are being retired; new
    configuration belongs in the space, not in a new profile.

    ``None``/empty defaults to ``fiction`` — existing setups see zero
    change. The error, like all our errors, lists the allowed values.
    """
    key = (name or "fiction").strip().lower()
    try:
        return PROFILES[key]
    except KeyError:
        raise GraphContextError(
            f"unknown GC_PROFILE {name!r}; allowed: {', '.join(sorted(PROFILES))}"
        ) from None


# ---------------------------------------------------------------------------
# Docstrings shared verbatim by every profile (genuinely domain-neutral).
# A doc lives here exactly once; putting a copy in a profile dict below
# means the profiles have actually diverged.
# ---------------------------------------------------------------------------

# The one source for the mintable-format menu (domain-owned, ADR 023):
# adding a format updates every prompt that lists it.
_FORMAT_MENU = ", ".join(sorted(FIELD_FORMATS))

_UPDATE_NODE_DOC = f"""\
Modify a node's fields and/or links. Only provided arguments change.

node_id accepts a node NAME as well as an id (resolved for you).

IMPORTANT: any update WITHOUT a new `summary` flags the node's summary
as stale (the one-liner may no longer reflect reality). Pass a fresh
`summary` whenever the change is meaningful; clear backlog stale flags
later via explore(only_stale=true).

description: REPLACES the node's entire long-form text (its Anytype page
body). To make a targeted edit, get_node first and send back the full
revised text -- a human may have rewritten it in Anytype since you last
saw it. An empty string clears it. Never list the node's links in the
description: a Connections section is maintained automatically at the
bottom of the page (you never see or write it).

fields: {{"key": "value"}} attributes. Every key MUST match one of the
space's own properties, by key or display name -- get_node shows what a
node already carries, and context action='overview' lists each type's
properties. The value updates THAT property, visible and filterable in
Anytype; select options match by name and are created when new;
multi-select values are comma-separated names ("Dark, Hopeful"). A key
that names a RELATION (e.g. "Assignee") also works: its value is a node
id or name, and the entry becomes a link, same as add_links. An
unmatched key ERRORS, listing the properties you can reuse -- prefer
reusing one. Only when none fits, resend with
create_missing_fields={{"key": "format"}} to create a real new property
(formats: {_FORMAT_MENU}).

add_links: same shape as create_node's links (set create_missing_relations
to create a brand-new relation label rather than reuse an existing one).
remove_links: list of {{"source", "edge_type", "target"}} exactly as shown
by get_node.
"""

def _fields_doc(examples: str) -> str:
    """The create_node ``fields`` parameter doc (ADR 023): shared semantics,
    profile-specific example property names. Lives here exactly once."""
    return f"""\
fields: {{"key": "value"}} attributes. Every key MUST match one of the
  space's own properties, by key or display name (e.g. {examples});
  context action='overview' lists each type's properties, and get_node
  shows what a node already carries. The value writes THAT property,
  visible and filterable in Anytype; select options match by name and
  are created when new; multi-select values are comma-separated names.
  A key that names a RELATION (e.g. "Assignee") also works: its value
  is a node id or name, and the entry becomes a link, same as links.
  An unmatched key ERRORS, listing the properties you can reuse --
  prefer reusing one. Only when none fits, resend with
  create_missing_fields={{"key": "format"}} to create a real new property
  (formats: {_FORMAT_MENU})."""


def _query_doc(examples: str) -> str:
    """Assemble the ``query`` doc: shared grammar + profile-specific
    worked examples. The grammar/semantics text lives here exactly once;
    only the examples diverge (same rule as the shared doc constants)."""
    return f"""\
List nodes by ATTRIBUTE VALUES -- filter, order, and cap, like an
Anytype Set view. Scans the whole graph, or one node's direct
neighborhood when `linked_to` is set. Use `explore` to walk outward
from a node, `find_node` to look up a name; use query to answer
"which nodes have these property values, in this order?"

type: optional type filter (an unknown type errors with the known list).
linked_to: node id OR name (resolved for you); restricts candidates to
  that node's DIRECT neighbors, either edge direction. Combine with
  `type` and `order_by` for per-entity listings and timelines.
  edge_types optionally restricts which relations count.
where: list of {{"field", "op", "value"}} conditions, ALL must hold.
  Ops: eq, neq, lt, lte, gt, gte, contains, exists, missing
  (exists/missing take no value). Values compare numerically when both
  sides are numbers, otherwise as text -- ISO dates order correctly.
  ABSENT FIELDS: a node may lack a field entirely (an unticked checkbox
  is stored as absence). `neq` MATCHES absent ("not known to be
  value"); eq/lt/lte/gt/gte/contains never match absent; exists/missing
  test presence itself. An unknown field name errors with the fields
  that DO exist -- read that list and retry.
order_by: e.g. ["due_date", "priority desc"] -- each entry is "field",
  "field asc", or "field desc". Nodes missing the field sort last.
  Sort-key values are echoed on each result line.
  Queryable fields: the node's own properties (get_node shows them)
  plus name, type, summary, story_time, modified_at, summary_stale.
view: run one of the user's SAVED Anytype Set views by name instead
  (e.g. view="Open Tasks") -- its filters and sorts are read fresh from
  the space, so whatever the user configured in Anytype applies as-is.
  Cannot be combined with type/linked_to/edge_types/where/order_by.
  An unknown name errors with the runnable views; a set only appears
  once its source is configured in Anytype and it holds an object.
limit: max results (default 25, cap 100). The header reports "N of M
  match(es)" -- tighten `where` or raise `limit` when truncated.
detail: names | summaries (default) | full.

{examples}"""


_SCHEDULE_DOC = """\
Schedule a future or recurring check-in (WHEN to act, not world data).
At the scheduled time the system starts a turn in this same chat and
hands you your stored prompt -- use it for reminders ("remind me a week
before taxes are due"), follow-ups ("ask on Friday whether the draft
shipped"), or recurring reviews ("every Monday 09:00, list stale
summaries").

Actions:
  set    -- create one. Requires all three:
            name     -- a short label (e.g. "tax reminder").
            schedule -- WHEN, in the server's LOCAL time, two forms:
                        * one-shot ISO date-time "2027-04-08T09:00"
                          (fires once; must be in the future; no UTC
                          offset)
                        * cron, 5 fields "minute hour day month weekday"
                          e.g. "0 9 * * 1" = Mondays 09:00 (ranges a-b,
                          steps */n, lists a,b; weekday 0 and 7 = Sunday)
            prompt   -- the instructions your future self receives when
                        it fires, with NO other context -- write them
                        self-contained: who/what/why and what to do
                        (e.g. "Remind Nick that taxes are due April 15
                        and ask whether he has filed yet.").
            The response echoes the computed next-fire time and the
            current server time -- CHECK the math against what the user
            asked for ("a week before April 15" -> April 8).
  list   -- every scheduled event: id, schedule and next fire, target
            chat, prompt, status. Also shows the current server time --
            call this when you need today's date.
  cancel -- stop one; node_id is the id shown by list, or the exact
            event name. Sets its "Schedule status" to Cancelled; the
            object and its schedule stay in Anytype, and the user can
            re-enable it there by setting the status back to Pending.

A recurring event fires at most once per occurrence; occurrences missed
while the system was down collapse into ONE late fire. Events live as
"Scheduled Event" objects in Anytype (fields: Schedule, Schedule
prompt, Schedule status, Last fired), so the user can view, edit, or
create them there too -- an empty status counts as Pending, and a fired
one-shot is marked Completed automatically.
"""


_AUTOMATION_DOC = """\
Create and manage Automation Rules: "when a property CHANGES on objects
of one type, do something" -- automations the system runs on its own a
few seconds after the change (e.g. "when a Task's Done is checked,
stamp Completion date"). Rules live as "Automation Rule" objects in
Anytype, so the user can view, edit, pause, or write them there too.

Tool actions:
  create -- needs name, target_type, watch_property, condition,
            rule_action (some rule actions need more, below).
  update -- change an existing rule; rule=<id or exact name> plus any
            create params to replace (an empty param keeps the stored
            value).
  list   -- every rule: id, config, status (active / paused / error
            with the last error), last fired.
  pause / resume -- switch a rule off/on without deleting it (rule=...).
  test   -- DRY RUN: simulate one fire against a real object and report
            what WOULD be written; nothing is applied. rule=<id/name>
            tests a stored rule; passing the create params (+ script)
            tests a DRAFT before creating it. Optional trigger=<object
            id or name> picks the simulated object. ALWAYS test a
            script before creating it.

condition -- the rule watches TRANSITIONS, not states:
  "changed to true" / "changed to false" (checkbox flips), "changed"
  (any value change).

rule_action -- what happens when the condition fires:
  "set property to now"    -- write the current date-time into
                              action_property (a date-format property
                              gets the date only).
  "set property value"     -- write action_value into action_property.
  "uncheck others of type" -- keep a checkbox exclusive across the
                              type's objects (condition and
                              action_property default correctly --
                              leave them empty).
  "run script"             -- run the `script` param (Python) in a
                              sandbox. The script's globals:
      trigger        -- the changed object:
                        {"id","type","name","summary","fields"}
      before / after -- the watched value around the change ("" = empty)
      now            -- "YYYY-MM-DD HH:MM:SS" server-local (use this,
                        never the clock)
      objects(type=None), find(name, type=None), field(obj, prop),
      neighbors(obj, edge_type=None) -- read any object in the space
      set(obj_or_id, prop, value) -- queue one write (str/bool/int/
                        float; max 20 per fire; the property must
                        already exist on the target's type)
      log(msg)       -- a line for the system log (print() is discarded)
      No imports beyond the stdlib, no network, ~5s time limit, and the
      space snapshot caps at 2000 objects.

Rules fire only on changes the system OBSERVES while running (nothing
retroactive), rules never trigger other rules, and a broken rule shows
status Error + "Rule last error" on its object -- it heals on its own
once fixed.
"""


_SEND_FILE_DOC = """\
Send a FILE to the user, attached to your reply in this chat (use it
when the deliverable is a document, not a message: an export, a table
as CSV, a longer write-up, code). Give the full filename with its
extension (e.g. "characters.csv", "summary.md") and the complete text
content -- the file is created from exactly what you pass; there is no
appending. Text formats only (csv, md, json, code, ...). Call once per
file, up to 4 per turn. The file uploads when your reply is delivered;
keep the reply itself short and let the file carry the bulk.
"""


_SCHEMA_DOC = """\
Propose a change to the space's SCHEMA -- a new object type, or new
properties on an existing type -- for the USER to confirm. The user owns
the schema: use this to turn "we should track factions" into a concrete
draft, but the change itself is executed by the system ONLY when the
user reacts \N{THUMBS UP SIGN} on the confirmation message that is
posted after your reply (\N{THUMBS DOWN SIGN} dismisses it). You have NO
way to apply a proposal -- never claim a schema change is done unless
the system reported it applied; a "yes" in words is not a confirmation,
the reaction is (if the user agrees verbally, point them at the
confirmation message).

Actions:
  propose_type   -- draft a NEW type. type=<display name, e.g.
                    "Faction">, optional plural (defaults to name+"s"),
                    optional properties (below), optional reason (one
                    line on why, shown to the user).
  propose_fields -- draft NEW properties on an EXISTING type.
                    type=<existing type name>, properties (required),
                    optional reason.
  list           -- pending proposals with their ids.
  cancel         -- discard a proposal (proposal_id): e.g. the user said
                    no or asked for changes (then re-propose).

properties -- a list of objects, each:
  {"name": "Status", "format": "select", "options": ["Open", "Done"]}
  formats: text, number, select, multi_select, date, checkbox, url,
  email, phone. options only for select/multi_select. A property that
  already exists in the space with the SAME format is reused (attached
  to the type); a different format is a conflict -- the error names it.
  Links between objects are NOT properties: use create_node/update_node
  links with create_missing_relations for a new edge label.

Don't repeat the draft's contents in your reply -- the confirmation
message carries them. Proposals are drafts for THIS conversation (they
do not survive a restart; re-propose if lost). Applied changes are real
Anytype types and properties -- immediately usable by create_node, and
the user can rename or refine them in Anytype afterwards (never rename
what a human set).
"""


_FIND_NODE_DOC = """\
Find nodes by NAME -- or by DESCRIPTION when you don't know the name.

Matching is tiered: exact name first, then substring, and if nothing
matches by name the input is treated as a description and matched by
MEANING (when semantic search is enabled). A name miss automatically
pulls in edits made directly in Anytype and retries, so a no-match
answer already accounts for just-created objects -- trust it before
creating anything new. Semantic hits are labelled
and each carries a "why" line (what matched, what it is linked to) so
you can verify before using an id. Each result line carries the node
id, ready to paste into any other tool.

name: the name, name fragment, or a plain-words description
  (e.g. "the engineer who reads stone").
type: optional type filter (e.g. "Character") to disambiguate.
limit: max matches to return (default 10).

You usually don't need this first: get_node, explore, find_path,
update_node and link `other` targets all accept a name directly in place
of an id and resolve it for you. Reach for find_node to browse, to
disambiguate when a name is ambiguous, or to confirm a node exists.
For a cold start with no name in mind, use context action='overview'.
"""


def _context_doc(
    *, resync: str, overview_note: str, held_noun: str, bound_to: str
) -> str:
    """The ``context`` doc: shared curation semantics assembled once.

    Profile-specific bits arrive as parameters (resync advice, framing
    nouns); the working-set slot counts and the scratchpad cap
    interpolate from their owning modules so the prompt can never lie
    about a budget.
    """
    return f"""\
Inspect or curate your cross-turn context: scratchpad, working set, resync.

Your scratchpad and working set are echoed to you at the start of every
turn -- they are how you remember across turns. Curate them deliberately.

Actions:
  get          -- session snapshot: graph statistics plus your current
                  scratchpad, working set, and recent trail.
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts, each type's properties (reuse these as create/
                  update `fields` keys), plus the highest-degree "hub"
                  nodes with name, type, id and summary. START HERE in a
                  fresh session to obtain node ids for explore /
                  get_node / hold. {overview_note}(alias: map)
  resync       -- {resync}
  note         -- REPLACE your scratchpad with `text` (empty text clears
                  it; max {SCRATCHPAD_MAX_CHARS} chars). Keep cross-turn intentions and open
                  threads here -- durable facts belong in the graph as
                  nodes, not in the scratchpad.
  hold         -- keep node_id in your working set at `detail`:
                  "summaries" (default; one-liner each turn) or "full"
                  (body + connections each turn -- for the 1-2 {held_noun} you
                  are actively working from). {DEFAULT_FULL_SLOTS} full slots, \
{DEFAULT_SUMMARY_SLOTS} summary
                  slots; overflow demotes/releases the oldest, and the
                  response says so. explore/find_path default to the most
                  recently held node when no start is given.
  release      -- drop node_id from the working set.
  clear        -- empty the working set (the scratchpad is kept).
  set_project  -- relabel the session's project (cosmetic; one server is
                  bound to {bound_to}).
"""


def _get_node_doc(
    *, entity_noun: str, editor: str, multi_read: str, edge_example: str
) -> str:
    """The ``get_node`` doc: one body, profile-specific example nouns and
    the optional read-several-at-once tip."""
    return f"""\
Read ONE node in depth: all fields plus every edge grouped by type,
with neighbor names and ids. Use when you need the full picture of a
single {entity_noun}; use `explore` to see a neighborhood instead. The full
description (the node's Anytype page body) is fetched fresh on every
call, so {editor} latest edits are always included.{multi_read}

node_id accepts a node NAME as well as an id (resolved for you; an
ambiguous name reports its candidates so you can pick one).
edge_types: optional filter, e.g. {edge_example}.
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
"""


def _explore_doc(
    *, as_of: str, scenario: str, deep: str, sweep_label: str
) -> str:
    """The ``explore`` doc: shared walk semantics; the timeline cutoff,
    the worked scenario, and the full-detail recipe arrive whole (their
    wording AND wrapping are profile voice)."""
    return f"""\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session nothing is held or recently touched; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = the most recently held node (falling
back to the most recently touched). depth: 1-3 (default 1).
detail: names | summaries (default) | full.
{as_of}

{scenario}

{deep}
Caution: "full" emits complete, untruncated descriptions; keep
depth=1 and use `limit`.

STALE-SUMMARY SWEEP ({sweep_label}):
  explore(depth=3, limit=50, only_stale=true, detail="names")
  ...then update_node each with a fresh summary.

Captured passages and session bookkeeping are hidden unless explicitly
named in include_types (e.g. include_types=["Capture"]).
"""


def _find_path_doc(*, intro: str, edge_example: str) -> str:
    """The ``find_path`` doc: the example question (and its wrapping) is
    profile voice; the semantics lines are shared."""
    return f"""\
{intro}start: empty = the most recently held (or touched) node. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only {edge_example}).
"""


# ---------------------------------------------------------------------------
# Fiction: the original surface, verbatim. The default profile.
# ---------------------------------------------------------------------------

_FICTION_DOCS: dict[str, str] = {
    "context": _context_doc(
        resync=(
            "pull in edits a human made directly in Anytype; reports\n"
            "                  which nodes changed. Use before a long "
            "writing session."
        ),
        overview_note=(
            "The map is rebuilt from the graph\n"
            "                  each call -- nothing to maintain. "
        ),
        held_noun="nodes",
        bound_to="one story world",
    ),
    "create_node": """\
Create a story-world node and its initial links in ONE call.

type: an existing type in your Anytype space (e.g. Character, Location,
  Event, Organization, Technology, Theme -- whatever your space defines).
  An unmatched type is reported back with the list of known types.
summary: REQUIRED one-liner; keep it current -- exploration shows it.
description: long-form text (a portrait, a place's atmosphere, an
  event's account). Stored as the node's Anytype page BODY, where the
  user reads and edits it directly; returned by get_node and
  explore(detail="full"). Write it for the page, in Markdown.
story_time: REQUIRED for an Event-role node (number; timeline position).
""" + _fields_doc("role, tech_type") + """
links: list of {"edge_type", "other" (target node id OR name),
  "outgoing" (default true)}. `other` accepts a node name -- it is
  resolved for you (ambiguous names report the candidates).
  edge_type is a relation LABEL. Reuse an existing relation (e.g. knows,
  located_at, participated_in, triggered_by, or any relation already in
  your space). A label with no existing relation is surfaced for approval;
  set create_missing_relations=true to create it on the fly.
  outgoing=false means the edge points FROM `other` TO the new node --
  e.g. creating an Event that an existing Character took part in:
    {"edge_type": "participated_in", "other": "<character id>",
     "outgoing": false}
icon: a single emoji for the page, shown in lists and the graph view --
  pick one that fits the node (a face for a person, a place mark for a
  location, an object for an item). Optional; humans may change it later.

Prefer linking at creation over separate update_node calls. Do not list
the node's links in the description -- a Connections section is
maintained automatically at the bottom of the page.
""",
    "update_node": _UPDATE_NODE_DOC,
    "get_node": _get_node_doc(
        entity_noun="entity",
        editor="a human's",
        multi_read=(
            " To read several\n"
            "related nodes at once (e.g. all participants of a scene), prefer\n"
            'explore(depth=1, detail="full") over repeated get_node calls.'
        ),
        edge_example='["participated_in", "knows"]',
    ),
    "explore": _explore_doc(
        as_of="""\
as_of: story-time cutoff -- Events after it are hidden (a character's
view of the world at that moment); include_future=true restores them
(foreshadowing/direction). limit caps results (default 25; the response
says when it truncated).""",
        scenario="""\
SCENE ASSEMBLY is an explore configuration, not a separate tool:
  explore(start="<event id>", depth=2,
          include_types=["Character", "Location", "Item"],
          detail="summaries", as_of=<event time>)""",
        deep="""\
RENDERING PREP (about to write prose about a scene):
  explore(start="<event id>", depth=1, detail="full")
returns the FULL descriptions of the event and every participant in
ONE call -- do not fetch participants one-by-one with get_node.""",
        sweep_label="before a big writing session",
    ),
    "find_path": _find_path_doc(
        intro="""\
Find the shortest meaningful connection between two nodes -- "how is
Mira related to the Fall of Brakk?" Surfaces non-obvious links for plot
work. `target` and `start` each accept a node id OR name (resolved for
you). """,
        edge_example='social edges: ["knows", "member_of"]',
    ),
    "find_node": _FIND_NODE_DOC,
    "query": _query_doc("""\
EXAMPLES -- the census tool (explore walks outward; query scans the world):
  every Character whose status property is "missing":
    query(type="Character",
          where=[{"field": "status", "op": "eq", "value": "missing"}])
  a character's TIMELINE (all their Events, in story order):
    query(type="Event", linked_to="Mira", order_by=["story_time"])
  the most recently edited nodes, any type:
    query(order_by=["modified_at desc"], limit=10)
"""),
    "schedule": _SCHEDULE_DOC,
    "automation": _AUTOMATION_DOC,
    "send_file": _SEND_FILE_DOC,
    "schema": _SCHEMA_DOC,
}

# Starter activity modes live in mode_seeds/*.toml since ADR 035 -- the
# space's Activity Mode objects are the only live source; profiles no
# longer carry mode specs.

FICTION = DomainProfile(
    name="fiction",
    description="story-world building and prose rendering (the original surface)",
    tool_docs=_FICTION_DOCS,
    role_overrides={},  # DEFAULT_TYPE_ROLES already speaks fiction
)


# ---------------------------------------------------------------------------
# Workspace: a work knowledge base (people, teams, projects, meetings,
# decisions). Same tools, same parameters, same storage keys -- the words
# and worked examples change, and a few native type keys gain roles so the
# timeline (`story_time`/`as_of`) works over real-world time.
# ---------------------------------------------------------------------------

_WORKSPACE_DOCS: dict[str, str] = {
    "context": _context_doc(
        resync=(
            "pull in edits a human made directly in Anytype; reports\n"
            "                  which nodes changed. Use before a long "
            "working session."
        ),
        overview_note=(
            "The map is rebuilt from the graph\n"
            "                  each call -- nothing to maintain. "
        ),
        held_noun="nodes",
        bound_to="one Anytype space",
    ),
    "create_node": """\
Create a knowledge-base node and its initial links in ONE call.

type: an existing type in your Anytype space (e.g. Person, Team, Project,
  Meeting, Decision, Document -- whatever your space defines). An
  unmatched type is reported back with the list of known types.
summary: REQUIRED one-liner; keep it current -- exploration shows it.
description: long-form text (a person's role and history, a project's
  charter, a decision's rationale). Stored as the node's Anytype page
  BODY, where the user reads and edits it directly; returned by get_node
  and explore(detail="full"). Write it for the page, in Markdown.
story_time: REQUIRED for an Event-role node (meetings, decisions,
  milestones): its position on the timeline as a sortable number -- use
  epoch seconds or YYYYMMDD (e.g. 20260702). The parameter name is
  historical; read it as "time".
""" + _fields_doc("status, priority") + """
links: list of {"edge_type", "other" (target node id OR name),
  "outgoing" (default true)}. `other` accepts a node name -- it is
  resolved for you (ambiguous names report the candidates).
  edge_type is a relation LABEL. Reuse an existing relation (e.g.
  member_of, works_on, attended, decided_in, or any relation already in
  your space). A label with no existing relation is surfaced for approval;
  set create_missing_relations=true to create it on the fly.
  outgoing=false means the edge points FROM `other` TO the new node --
  e.g. creating a Meeting that an existing Person attended:
    {"edge_type": "attended", "other": "<person id>", "outgoing": false}
icon: a single emoji for the page, shown in lists and the graph view --
  pick one that fits the node (a face for a person, a calendar for a
  meeting, a target for a milestone). Optional; humans may change it later.

Prefer linking at creation over separate update_node calls. Do not list
the node's links in the description -- a Connections section is
maintained automatically at the bottom of the page.
""",
    "update_node": _UPDATE_NODE_DOC,
    "get_node": _get_node_doc(
        entity_noun="entity",
        editor="a human's",
        multi_read=(
            " To read several\n"
            "related nodes at once (e.g. everyone in a meeting), prefer\n"
            'explore(depth=1, detail="full") over repeated get_node calls.'
        ),
        edge_example='["works_on", "member_of"]',
    ),
    "explore": _explore_doc(
        as_of="""\
as_of: time cutoff -- Event-role nodes (meetings, decisions, milestones)
after it are hidden (the state of the world as of that moment);
include_future=true restores them (planned/upcoming work). limit caps
results (default 25; the response says when it truncated).""",
        scenario="""\
A MEETING or DECISION BRIEF is an explore configuration, not a separate
tool:
  explore(start="<meeting id>", depth=2,
          include_types=["Person", "Team", "Project"],
          detail="summaries", as_of=<meeting time>)""",
        deep="""\
DEEP CONTEXT (about to write a summary, brief, or report):
  explore(start="<node id>", depth=1, detail="full")
returns the FULL descriptions of the node and every neighbor in ONE
call -- do not fetch neighbors one-by-one with get_node.""",
        sweep_label="before a big update session",
    ),
    "find_path": _find_path_doc(
        intro="""\
Find the shortest meaningful connection between two nodes -- "how is
Alice related to the Q3 replatform decision?" Surfaces non-obvious
links. `target` and `start` each accept a node id OR name (resolved for
you). """,
        edge_example='org edges: ["member_of", "works_on"]',
    ),
    "find_node": _FIND_NODE_DOC,
    "query": _query_doc("""\
EXAMPLES:
  open Tasks, most urgent first:
    query(type="Task",
          where=[{"field": "status", "op": "neq", "value": "done"}],
          order_by=["priority desc", "due_date"], limit=10)
  everything decided around a project (Decisions linked to it, by date):
    query(type="Decision", linked_to="Q3 Replatform",
          order_by=["story_time"])
"""),
    "schedule": _SCHEDULE_DOC,
    "automation": _AUTOMATION_DOC,
    "send_file": _SEND_FILE_DOC,
    "schema": _SCHEMA_DOC,
}

WORKSPACE = DomainProfile(
    name="workspace",
    description="work knowledge base (people, teams, projects, meetings, decisions)",
    tool_docs=_WORKSPACE_DOCS,
    role_overrides={
        # Only Role.EVENT changes behavior (story_time invariant + as_of
        # timeline); the rest are cosmetic role names for error suggestions.
        "person": Role.CHARACTER,
        "team": Role.ORGANIZATION,
        "meeting": Role.EVENT,
        "decision": Role.EVENT,
        "milestone": Role.EVENT,
        "tool": Role.TECHNOLOGY,
    },
)


# ---------------------------------------------------------------------------
# Assistant: a personal work assistant & note taker (WP12/ADR 015). Tasks,
# procedures, and notes are first-class native types (no roles needed);
# meetings/milestones are Event-role so the timeline works -- over REAL
# dates (time_property=event_date), not a story number. Capture modes
# produce native-typed artifacts: a recorded procedure is work product.
# ---------------------------------------------------------------------------

_ASSISTANT_DOCS: dict[str, str] = {
    "context": _context_doc(
        resync=(
            "pull in edits made directly in Anytype; reports which\n"
            "                  nodes changed. Use at the start of a work "
            "session."
        ),
        overview_note="",
        held_noun="items",
        bound_to="one Anytype space",
    ),
    "create_node": """\
Create a node in the user's workspace and its initial links in ONE call.

type: an existing type in the Anytype space (e.g. Task, Procedure, Note,
  Meeting, Person, Project -- whatever the space defines). An unmatched
  type is reported back with the list of known types.
summary: REQUIRED one-liner; keep it current -- exploration shows it.
description: long-form text (a task's context, a procedure's overview, a
  meeting's agenda). Stored as the node's Anytype page BODY, where the
  user reads and edits it directly; returned by get_node and
  explore(detail="full"). Write it for the page, in Markdown.
story_time: REQUIRED for an Event-role node (meetings, milestones): an
  ISO date like "2026-07-04". The parameter name is historical; read it
  as "when".
""" + _fields_doc('status, priority, "Due date"') + """
links: list of {"edge_type", "other" (target node id OR name),
  "outgoing" (default true)}. `other` accepts a node name -- it is
  resolved for you. Reuse an existing relation (e.g. part_of, assigned_to,
  documents, or any relation already in the space); a label with no
  existing relation is surfaced for approval; set
  create_missing_relations=true to create it on the fly.
icon: a single emoji for the page, shown in lists and the graph view --
  pick one that fits (a checkbox for a task, a clipboard for a
  procedure, a calendar for a meeting). Optional.

Prefer linking at creation over separate update_node calls. Do not list
the node's links in the description -- a Connections section is
maintained automatically at the bottom of the page.
""",
    "update_node": _UPDATE_NODE_DOC,
    "get_node": _get_node_doc(
        entity_noun="item",
        editor="the user's",
        multi_read="",
        edge_example='["part_of", "assigned_to"]',
    ),
    "explore": _explore_doc(
        as_of="""\
as_of: an ISO date cutoff -- Event-role nodes (meetings, milestones)
after it are hidden (the state of things as of that date);
include_future=true restores them (planned/upcoming work). limit caps
results (default 25; the response says when it truncated).""",
        scenario="""\
A TASK or PROJECT BRIEF is an explore configuration, not a separate tool:
  explore(start="<project id>", depth=2,
          include_types=["Task", "Person", "Procedure"],
          detail="summaries")""",
        deep="""\
DEEP CONTEXT (about to write a summary or repeat a procedure):
  explore(start="<node id>", depth=1, detail="full")
returns the FULL descriptions of the node and every neighbor in ONE
call -- do not fetch neighbors one-by-one with get_node.""",
        sweep_label="before a review session",
    ),
    "find_path": _find_path_doc(
        intro="""\
Find the shortest meaningful connection between two nodes -- "how does
this task relate to that decision?" Surfaces non-obvious links.
`target` and `start` each accept a node id OR name (resolved for you).
""",
        edge_example='org edges: ["part_of", "assigned_to"]',
    ),
    "find_node": _FIND_NODE_DOC,
    "query": _query_doc("""\
EXAMPLES:
  10 open todos, due first, ties by priority:
    query(type="Task",
          where=[{"field": "done", "op": "neq", "value": "true"}],
          order_by=["due_date", "priority desc"], limit=10)
  (an unticked checkbox is stored as ABSENCE and neq matches absent, so
  done-neq-true finds every not-done item.)
  the user's own saved list, exactly as they configured it in Anytype:
    query(view="Open Tasks")
  a person's meeting history, most recent first:
    query(type="Meeting", linked_to="Alice", order_by=["story_time desc"])
"""),
    "schedule": _SCHEDULE_DOC,
    "automation": _AUTOMATION_DOC,
    "send_file": _SEND_FILE_DOC,
    "schema": _SCHEMA_DOC,
}

ASSISTANT = DomainProfile(
    name="assistant",
    description="personal work assistant & note taker (tasks, procedures, notes)",
    tool_docs=_ASSISTANT_DOCS,
    role_overrides={
        "person": Role.CHARACTER,
        "team": Role.ORGANIZATION,
        "meeting": Role.EVENT,
        "milestone": Role.EVENT,
        "tool": Role.TECHNOLOGY,
    },
    time_property="event_date",
    time_format="date",
    # "The deploy task" usually means the live one: recency matters here
    # (a weight, never a rule -- ADR 016).
    ranking=RankingWeights(recency=0.3),
)


PROFILES: dict[str, DomainProfile] = {
    p.name: p for p in (FICTION, WORKSPACE, ASSISTANT)
}
