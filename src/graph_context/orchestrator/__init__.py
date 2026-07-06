"""The orchestrator: an agentic pipeline over the application layer (ADR 007).

A SECOND interface adapter beside the MCP server -- it sees the whole
conversation (user prompts, model output, turn boundaries), which is what
mode-gated tool availability and, later, automatic provenance (WP7) need.
It reuses ``interface/tools.py`` verbatim (the tools' consumer is an LLM in
both adapters) and never imports ``interface/server.py`` or the MCP SDK
(import-linter-enforced).

Layout:

* ``modes.py``    -- the mode -> tool-binding tables. Authoring mode's
                     binding *lacks* the mutation tools; unavailable, not
                     refused.
* ``drivers.py``  -- the LLM seam: a driver decides each step (tool calls
                     or a final reply). ``ScriptedDriver`` powers tests and
                     demos; the real driver plugs in here once the image
                     carries ``claude-agent-sdk`` (subscription-billed
                     model access via Claude Code; langgraph itself is
                     installed), and NOTHING outside this package may
                     import either framework.
* ``pipeline.py`` -- ``Orchestrator.handle_message``: the transport-
                     agnostic entry seam (CLI first; chat transports in
                     WP8).
* ``cli.py``      -- composition root + the first thin transport adapter.
"""
