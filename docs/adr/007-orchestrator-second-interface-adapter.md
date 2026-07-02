# ADR 007: Orchestrator as a second in-process interface adapter

**Status:** Accepted (2026-07-02)

## Context

Two forces push the project beyond a passive MCP server. First, the product
is generalizing beyond fiction: ADR 006 already made the schema
space-reflecting, so a work knowledge base is structurally supported today —
what remains fiction-specific is prompt framing, not structure. But the
behaviors wanted next — automatic capture of produced artifacts, provenance
for graph mutations, mode-dependent tool availability — all require seeing
the conversation, and an MCP server cannot: it receives tool calls and their
arguments only, never the user's prompt, the model's intermediate output, or
turn boundaries. Dogfooding shows the symptom: `record_prose` depends on the
model *choosing* to call it, and the model forgets, under-cites, or skips it
under context pressure. No docstring fixes that; it is a process-boundary
limit.

Second, mode-gated tool availability (world-modeling vs authoring) is only
an enforcement if the harness owns the tool binding. Asking the model to
respect a mode it could ignore is a convention, not a boundary.

## Decision

Add an orchestrator package (`orchestrator/`, same repo) as a **second
interface adapter** over the application layer — alongside the MCP server,
not replacing it:

```
                ┌─ interface/  (MCP server — unchanged, standalone product)
application ◀───┤
                └─ orchestrator/  (agentic pipeline: modes, capture,
                                   provenance — ADR 008)
```

- **In-process coupling.** The orchestrator imports application services
  directly and reuses `interface/tools.py` — the thin LLM-facing wrappers
  and their docstrings-as-prompts carry over unchanged, because the tools'
  consumer is an LLM in both adapters. No MCP hop, no reparsing of
  presenter output. It never imports `interface/server.py` (the module that
  owns the MCP SDK).
- **The MCP server remains a supported standalone product.** It works in
  any MCP client today; "graph knowledge base as tools" is independently
  valuable. The orchestrator is the opinionated product on top.
- **LangGraph is the initial framework, quarantined.** No
  langgraph/langchain type crosses into interface, application, or domain —
  the same discipline that confines Anytype quirks to `mapping.py`.
  Swapping frameworks later must cost only orchestrator-internal changes.
  It ships as an optional extra (`pip install -e ".[orchestrator]"`) so the
  core server install stays lean.
- **Modes bind tool subsets.** World-modeling mode binds the full surface;
  authoring mode binds read-only retrieval plus focus management. In
  authoring mode the mutation tools are *not bound at all* — unavailable,
  not refused.
- **The dependency rule extends** (import-linter, CI-enforced):
  orchestrator → interface (tools/presenters only) → application → domain;
  the orchestrator's composition root joins `interface/server.py` on the
  short list allowed to import infrastructure. Nothing imports orchestrator.

## Consequences

- Everything that requires conversation visibility (ADR 008: intent nodes,
  automatic capture) now has a home; everything that doesn't stays where it
  is.
- Two composition roots want the same wiring — factor the
  config → client → bootstrap → repository → hydrate → session build out of
  `interface/server.py` into a shared builder instead of duplicating it.
- The devcontainer must grow the langgraph dependency (egress firewall:
  it goes into the container build, not an ad-hoc `pip install`).
- MCP-only clients (e.g. Claude Desktop) get no modes and no automatic
  provenance; `record_prose` remains their voluntary capture path.
- The orchestrator needs its own chat surface (CLI loop first) and makes its
  own model calls via the Anthropic API. The pipeline is deliberately NOT
  re-exposed as an MCP server: driving it from Claude Desktop would put two
  LLMs in the loop (cost, latency, the outer model paraphrasing the inner)
  while the outer model — not the harness — still decides whether the
  pipeline runs at all. Claude Desktop keeps the plain tool server.
