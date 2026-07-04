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
    "record_prose",
)


@dataclass(frozen=True, slots=True)
class DomainProfile:
    """One deployment's framing: prompt text + type-key -> Role additions."""

    name: str
    description: str
    tool_docs: Mapping[str, str]
    role_overrides: Mapping[str, Role]

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

_FIND_NODE_DOC = """\
Find nodes by NAME when you know the name but not the id.

Matching is case-insensitive: exact-name matches are returned first, and
only if there are none does it fall back to substring matches. Each
result line carries the node id, ready to paste into any other tool.

name: the name (or fragment) to search for.
type: optional type filter (e.g. "Character") to disambiguate.
limit: max substring matches to return (default 10).

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
Inspect or adjust the working session: graph stats, focus stack, resync.

Actions:
  get          -- graph statistics (node/edge counts, stale summaries).
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts plus the highest-degree "hub" nodes with name,
                  type, id and summary. START HERE in a fresh session to
                  obtain node ids for explore / get_node / focus. The map
                  is rebuilt from the graph each call -- nothing to
                  maintain. (alias: map)
  resync       -- pull in edits a human made directly in Anytype; reports
                  which nodes changed. Use before a long writing session.
  focus        -- push node_id onto the focus stack (queries default to
                  the top of this stack when no start is given).
  pin / unpin  -- protect / unprotect node_id from focus-stack eviction.
  remove       -- drop node_id from the focus stack.
  clear        -- empty the focus stack (pinned entries survive).
  set_project  -- relabel the project shown in the header (cosmetic; one
                  server is bound to one story world).

Every tool response begins with `[project | focus | recent]` -- this tool
is how you manage what appears there.
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
include_prose: how many recently-recorded prose passages that reference
  this node to attach (default 0; most-recent first, with excerpts) --
  the "how was this described last time?" consistency lookup. Try 1-3
  before re-describing a returning location or character.
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
""",
    "explore": """\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session the focus stack is empty; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = top of the focus stack. depth: 1-3 (default 1).
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

Prose and SessionContext nodes are hidden unless explicitly named in
include_types.
""",
    "find_path": """\
Find the shortest meaningful connection between two nodes -- "how is
Mira related to the Fall of Brakk?" Surfaces non-obvious links for plot
work. `target` and `start` each accept a node id OR name (resolved for
you). start: empty = focus-stack top. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only social edges: ["knows", "member_of"]).
""",
    "find_node": _FIND_NODE_DOC,
    "record_prose": """\
Save rendered prose INTO the graph so future scenes stay consistent.

Call after writing any scene/passage the user keeps. references is
REQUIRED and must list every node id whose content shaped the prose
(the characters present, the location, the events depicted) -- this
powers later consistency checks. summary: one-liner of what the passage
covers.
""",
}

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
    "context": """\
Inspect or adjust the working session: graph stats, focus stack, resync.

Actions:
  get          -- graph statistics (node/edge counts, stale summaries).
  overview     -- DERIVED entry-point map for a cold start: per-type
                  counts plus the highest-degree "hub" nodes with name,
                  type, id and summary. START HERE in a fresh session to
                  obtain node ids for explore / get_node / focus. The map
                  is rebuilt from the graph each call -- nothing to
                  maintain. (alias: map)
  resync       -- pull in edits a human made directly in Anytype; reports
                  which nodes changed. Use before a long working session.
  focus        -- push node_id onto the focus stack (queries default to
                  the top of this stack when no start is given).
  pin / unpin  -- protect / unprotect node_id from focus-stack eviction.
  remove       -- drop node_id from the focus stack.
  clear        -- empty the focus stack (pinned entries survive).
  set_project  -- relabel the project shown in the header (cosmetic; one
                  server is bound to one Anytype space).

Every tool response begins with `[project | focus | recent]` -- this tool
is how you manage what appears there.
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
include_prose: how many recently-captured passages (meeting notes,
  decision records, saved summaries) that reference this node to attach
  (default 0; most-recent first, with excerpts) -- the "what was written
  about this last time?" lookup. Try 1-3 before re-summarizing a
  recurring topic.
include_provenance: how many intent records that touched this node to
  attach (default 0; most-recent first, with excerpts) -- the "who
  changed this, and why?" audit lookup. The response notes when such
  records exist.
""",
    "explore": """\
Walk the graph outward from a node. THE general retrieval primitive.

In a fresh session the focus stack is empty; call context
action="overview" first to get a starting node id (or pass a node name
as `start` -- it is resolved for you).

start: node id OR name; empty = top of the focus stack. depth: 1-3 (default 1).
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

Prose and SessionContext nodes are hidden unless explicitly named in
include_types.
""",
    "find_path": """\
Find the shortest meaningful connection between two nodes -- "how is
Alice related to the Q3 replatform decision?" Surfaces non-obvious
links. `target` and `start` each accept a node id OR name (resolved for
you). start: empty = focus-stack top. Edge direction is ignored for
reachability but shown in the result. Restrict edge_types to make the
path more meaningful (e.g. only org edges: ["member_of", "works_on"]).
""",
    "find_node": _FIND_NODE_DOC,
    "record_prose": """\
Save a text artifact (meeting notes, a decision record, a report
section) INTO the graph so future work builds on it.

Call after producing any passage the user keeps. references is REQUIRED
and must list every node id whose content shaped the text (the people
involved, the project, the decisions discussed) -- this powers later
consistency lookups. summary: one-liner of what the passage covers.
""",
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


PROFILES: dict[str, DomainProfile] = {p.name: p for p in (FICTION, WORKSPACE)}
