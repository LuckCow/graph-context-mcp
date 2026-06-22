# Handoff: finishing WP2 & WP3

WP2 (MCP tool layer) and WP3 (story layer) are **scaffolded, not finished**.
The structure, the decisions, and the intent are in place; tests for the
new code are deliberately absent (the existing 79 cover WP0/WP1 and must
stay green). This file is the worklist. Read `docs/WORK_PACKAGES.md` first
for the why; read module docstrings for the how — every scaffolded module
opens with its intent and carries `TODO(junior)` markers at the exact
seams.

## What is already working (verify by running it)

`PYTHONPATH=src python scripts/demo_wp2_tools.py` drives the complete loop
in-process: composite create → scene-assembly explore → find_path →
record_prose → staleness workflow → resync reporting → actionable errors.
`python -m graph_context.interface.server` starts a real stdio MCP server
(`GC_BACKEND=memory` to run without Anytype).

Working: all seven tools (`context`, `create_node`, `update_node`,
`get_node`, `explore`, `find_path`, `record_prose`); the `guarded` wrapper
(header on every response, errors-as-prompts, no leaked tracebacks);
tool-layer policy (Prose/SessionContext hidden from explore by default;
`only_stale` narrowing); `NodeReader`/`ProseRecorder`; body support
end-to-end (write-once, on-demand `fetch_body`, never hydrated);
`SessionState` snapshots; `SessionPersister` (debounce);
`AnytypeSessionStore` (untested); FastMCP composition root with lifespan
and backend selection.

## The worklist, in suggested order

1. **Tests for the tool layer** (WORK_PACKAGES → WP2 → Tests). Drive the
   functions in `interface/tools.py` directly (the demo script shows how);
   no MCP client needed. Priorities: header present on success AND error
   paths; every `_parse_*` error message contains the allowed values; the
   default Prose/SessionContext exclusion; `only_stale`; snapshot/golden
   tests of presenter output over the fixture world.
2. **Wire session persistence** in `interface/server.py` — the lifespan
   carries a commented five-line TODO with the exact code. Then add the
   `note_mutation` assertions (writes flush every N) and the WP3 contract
   tests for both SessionStores (round-trip; corrupt JSON → fresh session,
   never a crash).
3. **`get_node` include_prose** (WP3). The wiring point and the exact
   index query are documented at the top of `application/node_reader.py`;
   excerpts come from `repository.fetch_body` (cap:
   `presenters.PROSE_EXCERPT_CHARS`), ordered by `fields["generated_at"]`
   most-recent first. Then surface the parameter in tools.py + server.py.
4. **Structured per-call logging** (WP2 deliverable): one wrapper at the
   `tools.guarded` seam — tool name, duration, ok/error. Never log prose
   content or summaries above DEBUG.
5. **Run the live-server spike** when an Anytype instance is available
   (WORK_PACKAGES → WP1.0): validate assumptions A1–A6 in
   `infrastructure/anytype/mapping.py`; corrections go to `mapping.py` and
   `mock_server.py` **in the same PR**. Then add the `ANYTYPE_E2E=1`
   contract subclass.
6. **Polish before first external use:** the parameter-naming review
   (WP2 open questions — tool parameter names are the hard-to-change
   public surface); pick `pop` vs `remove` for focus and align the
   FocusStack API with the `context` tool's verb set; iterate tool
   docstrings from dogfooding transcripts (they are prompts — treat
   transcript failures as docstring bugs first).

## Known cosmetic wart (fix with a test)

`NodeWriter.create_node` records link targets into recent-history in a way
that can produce non-consecutive duplicates in the header's `recent` list
(visible in the demo: "Siege of Brakk, Mira, Siege of Brakk"). Decide the
intended semantics (probably: a composite create touches the new node only,
or dedupe the rendered recent list) and pin it with a presenter test.

## Rules that protect you

The dependency rule (README) is enforced by review: tools.py imports
application + domain, never infrastructure; server.py is the only module
that may import both infrastructure and the MCP SDK. Business rules live
in exactly one place — if a fix tempts you to re-check a rule in a second
layer, the rule is in the wrong place; move it, don't copy it. And the
fake repository is the executable spec: anything the Anytype adapter
learns to do, the fake learns in the same PR, or the port is wrong.
