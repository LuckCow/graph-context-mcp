"""Domain profiles: the deployment's *framing* of the graph (WP5).

The schema is space-reflecting and domain-neutral (ADR 006); what actually
differs between a story world and a work knowledge base is framing — the
tool docstrings (which are prompts, WP2), their worked examples, and which
native type keys map to semantic roles. A :class:`DomainProfile` bundles
exactly that and nothing else. Storage keys (``gc_story_time``,
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
from graph_context.domain.schema import Role
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


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """One activity mode: data, not an enum member (ADR 015).

    ``goal`` is the system-prompt fragment handed to the LLM driver --
    specs are prompts and get the golden-test review bar. ``mutating``
    picks the tool binding (full surface vs read-only + context);
    ``capture`` enables harness-side auto-capture of substantial replies.
    """

    name: str
    goal: str
    mutating: bool = False
    capture: CapturePolicy | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.name.replace("_", "").isalnum():
            raise ValueError(f"mode name must be a slug, got {self.name!r}")
        if not self.goal.strip():
            raise ValueError(f"mode {self.name!r} needs a non-empty goal prompt")


@dataclass(frozen=True, slots=True)
class DomainProfile:
    """One deployment's framing: prompt text, roles, and activity modes."""

    name: str
    description: str
    tool_docs: Mapping[str, str]
    role_overrides: Mapping[str, Role]
    mode_specs: tuple[ModeSpec, ...] = ()
    default_mode: str = "world_modeling"
    # The Event-role timeline source (ADR 015): a property key + format.
    # Fiction keeps the gc_story_time number; a date-axis profile names a
    # native date property (ISO strings order lexicographically).
    time_property: str = "gc_story_time"
    time_format: str = "number"
    # Ranking signal weights (ADR 016) -- data, tuned against the eval
    # golden. Fiction leaves recency at zero; the assistant raises it.
    ranking: RankingWeights = RankingWeights()

    def __post_init__(self) -> None:
        missing = set(TOOL_NAMES) - set(self.tool_docs)
        extra = set(self.tool_docs) - set(TOOL_NAMES)
        if missing or extra:
            raise ValueError(
                f"profile {self.name!r} tool_docs mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        names = [spec.name for spec in self.mode_specs]
        if len(names) != len(set(names)):
            raise ValueError(f"profile {self.name!r} has duplicate mode names")
        if self.mode_specs and self.default_mode not in names:
            raise ValueError(
                f"profile {self.name!r} default_mode {self.default_mode!r} "
                f"is not among its modes {names}"
            )


def get_profile(name: str | None) -> DomainProfile:
    """Resolve ``GC_PROFILE`` (or an explicit name) to a profile.

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

_UPDATE_NODE_DOC = """\
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

fields: {"key": "value"} attributes. A key matching one of the space's
own properties (by key or display name -- get_node shows what a node
already carries) updates THAT property, visible and filterable in
Anytype; select options match by name and are created when new;
multi-select values are comma-separated names ("Dark, Hopeful").
Unmatched keys land in a bot-only extras store, which this parameter
replaces wholesale -- resend extras you want kept.

add_links: same shape as create_node's links (set create_missing_relations
to create a brand-new relation label rather than reuse an existing one).
remove_links: list of {"source", "edge_type", "target"} exactly as shown
by get_node.
"""

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


_FIND_NODE_DOC = """\
Find nodes by NAME -- or by DESCRIPTION when you don't know the name.

Matching is tiered: exact name first, then substring, and if nothing
matches by name the input is treated as a description and matched by
MEANING (when semantic search is enabled). Semantic hits are labelled
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


# ---------------------------------------------------------------------------
# Fiction: the original surface, verbatim. The default profile.
# ---------------------------------------------------------------------------

_FICTION_DOCS: dict[str, str] = {
    "context": """\
Inspect or curate your cross-turn context: scratchpad, working set, resync.

Your scratchpad and working set are echoed to you at the start of every
turn -- they are how you remember across turns. Curate them deliberately.

Actions:
  get          -- session snapshot: graph statistics plus your current
                  scratchpad, working set, and recent trail.
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts plus the highest-degree "hub" nodes with name,
                  type, id and summary. START HERE in a fresh session to
                  obtain node ids for explore / get_node / hold. The map
                  is rebuilt from the graph each call -- nothing to
                  maintain. (alias: map)
  resync       -- pull in edits a human made directly in Anytype; reports
                  which nodes changed. Use before a long writing session.
  note         -- REPLACE your scratchpad with `text` (empty text clears
                  it; max 2000 chars). Keep cross-turn intentions and open
                  threads here -- durable facts belong in the graph as
                  nodes, not in the scratchpad.
  hold         -- keep node_id in your working set at `detail`:
                  "summaries" (default; one-liner each turn) or "full"
                  (body + connections each turn -- for the 1-2 nodes you
                  are actively working from). 2 full slots, 6 summary
                  slots; overflow demotes/releases the oldest, and the
                  response says so. explore/find_path default to the most
                  recently held node when no start is given.
  release      -- drop node_id from the working set.
  clear        -- empty the working set (the scratchpad is kept).
  set_project  -- relabel the session's project (cosmetic; one server is
                  bound to one story world).
""",
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
fields: {"key": "value"} attributes. A key matching one of the space's
  own properties (e.g. role, tech_type -- by key or display name) writes
  THAT property, visible and filterable in Anytype; select options match
  by name and are created when new; multi-select values are
  comma-separated names. Unmatched keys are kept in a bot-only extras
  store.
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
    "get_node": """\
Read ONE node in depth: all fields plus every edge grouped by type,
with neighbor names and ids. Use when you need the full picture of a
single entity; use `explore` to see a neighborhood instead. The full
description (the node's Anytype page body) is fetched fresh on every
call, so a human's latest edits are always included. To read several
related nodes at once (e.g. all participants of a scene), prefer
explore(depth=1, detail="full") over repeated get_node calls.

node_id accepts a node NAME as well as an id (resolved for you; an
ambiguous name reports its candidates so you can pick one).
edge_types: optional filter, e.g. ["participated_in", "knows"].
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
""",
    "explore": """\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session nothing is held or recently touched; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = the most recently held node (falling
back to the most recently touched). depth: 1-3 (default 1).
detail: names | summaries (default) | full.
as_of: story-time cutoff -- Events after it are hidden (a character's
view of the world at that moment); include_future=true restores them
(foreshadowing/direction). limit caps results (default 25; the response
says when it truncated).

SCENE ASSEMBLY is an explore configuration, not a separate tool:
  explore(start="<event id>", depth=2,
          include_types=["Character", "Location", "Item"],
          detail="summaries", as_of=<event time>)

RENDERING PREP (about to write prose about a scene):
  explore(start="<event id>", depth=1, detail="full")
returns the FULL descriptions of the event and every participant in
ONE call -- do not fetch participants one-by-one with get_node.
Caution: "full" emits complete, untruncated descriptions; keep
depth=1 and use `limit`.

STALE-SUMMARY SWEEP (before a big writing session):
  explore(depth=3, limit=50, only_stale=true, detail="names")
  ...then update_node each with a fresh summary.

Captured passages and session bookkeeping are hidden unless explicitly
named in include_types (e.g. include_types=["Capture"]).
""",
    "find_path": """\
Find the shortest meaningful connection between two nodes -- "how is
Mira related to the Fall of Brakk?" Surfaces non-obvious links for plot
work. `target` and `start` each accept a node id OR name (resolved for
you). start: empty = the most recently held (or touched) node. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only social edges: ["knows", "member_of"]).
""",
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
}

_FICTION_MODES = (
    ModeSpec(
        name="world_modeling",
        goal=(
            "You are building and maintaining a story-world knowledge graph. "
            "Create and update nodes for characters, places, events, and "
            "ideas as the user develops the world; link them as you go; keep "
            "every summary current. The graph is the source of truth -- "
            "capture decisions into it rather than leaving them in chat."
        ),
        mutating=True,
    ),
    ModeSpec(
        name="authoring",
        goal=(
            "You are writing prose inside an established story world. Gather "
            "context with the read tools (explore from the scene's nodes; "
            "get_node for full descriptions) and write in the world's voice. "
            "You cannot modify the graph in this mode -- substantial passages "
            "you produce are captured automatically with their sources."
        ),
        capture=CapturePolicy(artifact_type="gc_prose"),
    ),
)

FICTION = DomainProfile(
    name="fiction",
    description="story-world building and prose rendering (the original surface)",
    tool_docs=_FICTION_DOCS,
    role_overrides={},  # DEFAULT_TYPE_ROLES already speaks fiction
    mode_specs=_FICTION_MODES,
)


# ---------------------------------------------------------------------------
# Workspace: a work knowledge base (people, teams, projects, meetings,
# decisions). Same tools, same parameters, same storage keys -- the words
# and worked examples change, and a few native type keys gain roles so the
# timeline (`story_time`/`as_of`) works over real-world time.
# ---------------------------------------------------------------------------

_WORKSPACE_DOCS: dict[str, str] = {
    "context": """\
Inspect or curate your cross-turn context: scratchpad, working set, resync.

Your scratchpad and working set are echoed to you at the start of every
turn -- they are how you remember across turns. Curate them deliberately.

Actions:
  get          -- session snapshot: graph statistics plus your current
                  scratchpad, working set, and recent trail.
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts plus the highest-degree "hub" nodes with name,
                  type, id and summary. START HERE in a fresh session to
                  obtain node ids for explore / get_node / hold. The map
                  is rebuilt from the graph each call -- nothing to
                  maintain. (alias: map)
  resync       -- pull in edits a human made directly in Anytype; reports
                  which nodes changed. Use before a long working session.
  note         -- REPLACE your scratchpad with `text` (empty text clears
                  it; max 2000 chars). Keep cross-turn intentions and open
                  threads here -- durable facts belong in the graph as
                  nodes, not in the scratchpad.
  hold         -- keep node_id in your working set at `detail`:
                  "summaries" (default; one-liner each turn) or "full"
                  (body + connections each turn -- for the 1-2 nodes you
                  are actively working from). 2 full slots, 6 summary
                  slots; overflow demotes/releases the oldest, and the
                  response says so. explore/find_path default to the most
                  recently held node when no start is given.
  release      -- drop node_id from the working set.
  clear        -- empty the working set (the scratchpad is kept).
  set_project  -- relabel the session's project (cosmetic; one server is
                  bound to one Anytype space).
""",
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
fields: {"key": "value"} attributes. A key matching one of the space's
  own properties (e.g. status, priority -- by key or display name)
  writes THAT property, visible and filterable in Anytype; select
  options match by name and are created when new; multi-select values
  are comma-separated names. Unmatched keys are kept in a bot-only
  extras store.
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
    "get_node": """\
Read ONE node in depth: all fields plus every edge grouped by type,
with neighbor names and ids. Use when you need the full picture of a
single entity; use `explore` to see a neighborhood instead. The full
description (the node's Anytype page body) is fetched fresh on every
call, so a human's latest edits are always included. To read several
related nodes at once (e.g. everyone in a meeting), prefer
explore(depth=1, detail="full") over repeated get_node calls.

node_id accepts a node NAME as well as an id (resolved for you; an
ambiguous name reports its candidates so you can pick one).
edge_types: optional filter, e.g. ["works_on", "member_of"].
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
""",
    "explore": """\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session nothing is held or recently touched; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = the most recently held node (falling
back to the most recently touched). depth: 1-3 (default 1).
detail: names | summaries (default) | full.
as_of: time cutoff -- Event-role nodes (meetings, decisions, milestones)
after it are hidden (the state of the world as of that moment);
include_future=true restores them (planned/upcoming work). limit caps
results (default 25; the response says when it truncated).

A MEETING or DECISION BRIEF is an explore configuration, not a separate
tool:
  explore(start="<meeting id>", depth=2,
          include_types=["Person", "Team", "Project"],
          detail="summaries", as_of=<meeting time>)

DEEP CONTEXT (about to write a summary, brief, or report):
  explore(start="<node id>", depth=1, detail="full")
returns the FULL descriptions of the node and every neighbor in ONE
call -- do not fetch neighbors one-by-one with get_node.
Caution: "full" emits complete, untruncated descriptions; keep
depth=1 and use `limit`.

STALE-SUMMARY SWEEP (before a big update session):
  explore(depth=3, limit=50, only_stale=true, detail="names")
  ...then update_node each with a fresh summary.

Captured passages and session bookkeeping are hidden unless explicitly
named in include_types (e.g. include_types=["Capture"]).
""",
    "find_path": """\
Find the shortest meaningful connection between two nodes -- "how is
Alice related to the Q3 replatform decision?" Surfaces non-obvious
links. `target` and `start` each accept a node id OR name (resolved for
you). start: empty = the most recently held (or touched) node. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only org edges: ["member_of", "works_on"]).
""",
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
}

_WORKSPACE_MODES = (
    ModeSpec(
        name="world_modeling",
        goal=(
            "You are maintaining a work knowledge base. Create and update "
            "nodes for people, teams, projects, meetings, and decisions as "
            "the user works; link them as you go; keep every summary "
            "current. The graph is the source of truth -- capture decisions "
            "into it rather than leaving them in chat."
        ),
        mutating=True,
    ),
    ModeSpec(
        name="authoring",
        goal=(
            "You are drafting work documents (briefs, summaries, reports) "
            "from an established knowledge base. Gather context with the "
            "read tools and write clearly. You cannot modify the graph in "
            "this mode -- substantial drafts you produce are captured "
            "automatically with their sources."
        ),
        capture=CapturePolicy(artifact_type="gc_prose"),
    ),
)

WORKSPACE = DomainProfile(
    name="workspace",
    description="work knowledge base (people, teams, projects, meetings, decisions)",
    tool_docs=_WORKSPACE_DOCS,
    mode_specs=_WORKSPACE_MODES,
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
    "context": """\
Inspect or curate your cross-turn context: scratchpad, working set, resync.

Your scratchpad and working set are echoed to you at the start of every
turn -- they are how you remember across turns. Curate them deliberately.

Actions:
  get          -- session snapshot: graph statistics plus your current
                  scratchpad, working set, and recent trail.
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts plus the highest-degree "hub" nodes with name,
                  type, id and summary. START HERE in a fresh session to
                  obtain node ids for explore / get_node / hold. (alias: map)
  resync       -- pull in edits made directly in Anytype; reports which
                  nodes changed. Use at the start of a work session.
  note         -- REPLACE your scratchpad with `text` (empty text clears
                  it; max 2000 chars). Keep cross-turn intentions and open
                  threads here -- durable facts belong in the graph as
                  nodes, not in the scratchpad.
  hold         -- keep node_id in your working set at `detail`:
                  "summaries" (default; one-liner each turn) or "full"
                  (body + connections each turn -- for the 1-2 items you
                  are actively working from). 2 full slots, 6 summary
                  slots; overflow demotes/releases the oldest, and the
                  response says so. explore/find_path default to the most
                  recently held node when no start is given.
  release      -- drop node_id from the working set.
  clear        -- empty the working set (the scratchpad is kept).
  set_project  -- relabel the session's project (cosmetic; one server is
                  bound to one Anytype space).
""",
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
fields: {"key": "value"} attributes. A key matching one of the space's
  own properties (e.g. status, priority, due -- by key or display name)
  writes THAT property, visible and filterable in Anytype; select
  options match by name and are created when new; multi-select values
  are comma-separated names. Unmatched keys are kept in a bot-only
  extras store.
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
    "get_node": """\
Read ONE node in depth: all fields plus every edge grouped by type,
with neighbor names and ids. Use when you need the full picture of a
single item; use `explore` to see a neighborhood instead. The full
description (the node's Anytype page body) is fetched fresh on every
call, so the user's latest edits are always included.

node_id accepts a node NAME as well as an id (resolved for you; an
ambiguous name reports its candidates so you can pick one).
edge_types: optional filter, e.g. ["part_of", "assigned_to"].
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
""",
    "explore": """\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session nothing is held or recently touched; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = the most recently held node (falling
back to the most recently touched). depth: 1-3 (default 1).
detail: names | summaries (default) | full.
as_of: an ISO date cutoff -- Event-role nodes (meetings, milestones)
after it are hidden (the state of things as of that date);
include_future=true restores them (planned/upcoming work). limit caps
results (default 25; the response says when it truncated).

A TASK or PROJECT BRIEF is an explore configuration, not a separate tool:
  explore(start="<project id>", depth=2,
          include_types=["Task", "Person", "Procedure"],
          detail="summaries")

DEEP CONTEXT (about to write a summary or repeat a procedure):
  explore(start="<node id>", depth=1, detail="full")
returns the FULL descriptions of the node and every neighbor in ONE
call -- do not fetch neighbors one-by-one with get_node.
Caution: "full" emits complete, untruncated descriptions; keep
depth=1 and use `limit`.

STALE-SUMMARY SWEEP (before a review session):
  explore(depth=3, limit=50, only_stale=true, detail="names")
  ...then update_node each with a fresh summary.

Captured passages and session bookkeeping are hidden unless explicitly
named in include_types (e.g. include_types=["Capture"]).
""",
    "find_path": """\
Find the shortest meaningful connection between two nodes -- "how does
this task relate to that decision?" Surfaces non-obvious links.
`target` and `start` each accept a node id OR name (resolved for you).
start: empty = the most recently held (or touched) node. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only org edges: ["part_of", "assigned_to"]).
""",
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
}

_ASSISTANT_MODES = (
    ModeSpec(
        name="organizing",
        goal=(
            "You are a work assistant maintaining the user's knowledge "
            "base. Create and update nodes for tasks, procedures, notes, "
            "meetings, and people as the user works; link them as you go; "
            "keep every summary current. The graph is the source of truth "
            "-- capture decisions into it rather than leaving them in chat."
        ),
        mutating=True,
    ),
    ModeSpec(
        name="record_procedure",
        goal=(
            "The user is doing something they want to be able to repeat. "
            "Notate each step they describe -- commands, clicks, decisions, "
            "gotchas -- as a clean numbered procedure, naming the systems "
            "and items involved by their node names where they exist. Ask "
            "for the step outcome when it is unclear. Your write-up is "
            "captured automatically as a procedure with its references."
        ),
        capture=CapturePolicy(artifact_type="procedure", min_chars=120),
    ),
    ModeSpec(
        name="meeting_notes",
        goal=(
            "The user is in or has just left a meeting. Turn what they "
            "tell you into structured notes: attendees, decisions, action "
            "items, open questions -- naming people and projects by their "
            "node names where they exist. Your notes are captured "
            "automatically with their references."
        ),
        capture=CapturePolicy(artifact_type="note", min_chars=120),
    ),
)

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
    mode_specs=_ASSISTANT_MODES,
    default_mode="organizing",
    time_property="event_date",
    time_format="date",
    # "The deploy task" usually means the live one: recency matters here
    # (a weight, never a rule -- ADR 016).
    ranking=RankingWeights(recency=0.3),
)


PROFILES: dict[str, DomainProfile] = {
    p.name: p for p in (FICTION, WORKSPACE, ASSISTANT)
}
