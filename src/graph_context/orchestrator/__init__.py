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
                     or a final reply). ``ScriptedDriver`` powers tests
                     and demos.
* ``driver_common.py`` -- SDK-free logic both real drivers share (system
                     prompt assembly, tool-schema derivation, transcript
                     fencing).
* ``claude_driver.py`` -- the default real driver: ``claude-agent-sdk``
                     over the Claude Code CLI, billing the user's
                     subscription. Tool calls are captured via the
                     permission callback (never executed by the SDK -- the
                     pipeline runs them); schemas are derived from the
                     tool wrappers' signatures.
* ``anthropic_driver.py`` -- the raw Messages-API driver: an explicit
                     opt-in (GC_DRIVER=anthropic_api + ANTHROPIC_API_KEY) that
                     bills API credits instead of the subscription; sends
                     the transcript as a native messages list with
                     tool_use/tool_result round-tripping. NOTHING outside
                     this package may import either model SDK;
                     import-linter enforces it.
* ``pipeline.py`` -- ``Orchestrator.handle_message``: the transport-
                     agnostic entry seam (CLI first; chat transports in
                     WP8).
* ``cli.py``      -- composition root + the first thin transport adapter.
"""
