# ADR 030: Mode-gated server-side web search

Date: 2026-07-16
Status: accepted (amended 2026-07-16: the v1 transcript-continuity
limitation is retired by WP22 — searching decisions carry their raw
result payloads on the decision event, so the next decide replays what
the search returned; see Consequences)

## Context

Dogfooding wants Claude-app parity inside Anytype chats: ask a question,
get an answer that can draw on the live web. The harness runs inside a
devcontainer whose egress firewall blocks general web traffic, so a
client-side search implementation (call a search API, fetch pages) is a
non-starter — and would be redundant anyway: Anthropic offers web search
as a **server-side tool** on both of our driver paths. On the Messages
API it is a `web_search_*` tool block; on the claude-agent-sdk it is the
built-in `WebSearch` tool. Either way the search executes on Anthropic's
infrastructure inside the same request — the only host the container
talks to is `api.anthropic.com`, which the firewall already allows.

Two design tensions:

- **ADR 007's boundary.** The bound graph tools are the model's whole
  surface; the subscription driver enforces this with `tools=[]` (all
  Claude Code built-ins off) and a deny-all permission callback. Web
  search must not quietly dissolve that boundary.
- **Story modes must stay graph-grounded.** A rendering mode that can
  reach for the web mid-scene undermines "the graph is the source of
  truth". Search availability is a property of the *activity*, not of
  the deployment.

## Decision

**Web search is a MODE property, default off** — exactly the ADR 029
`activity_detail` pattern. `ModeSpec.web_search: bool` is settable in
all three mode-config sources: profile specs, `GC_MODES_FILE`
(`web_search = true`), and the in-space Activity Mode object via a new
`gc_mode_web_search` **checkbox** property (minted/retrofitted by
bootstrap like every `MODE_PROPERTIES` entry; the explainer body
documents it). Picking a mode picks whether the model can search.

**The flag rides `decide()`**: the pipeline forwards
`web_search=spec.web_search` on every decision (a keyword-only protocol
parameter — one bit does not justify a context object; a constructor
knob would be wrong because one driver instance serves many
spaces/modes). Both drivers honor it:

- **`AnthropicDriver`** appends the server tool block after the sorted
  graph tools (`web_search_20260209` on dynamic-filtering models,
  `web_search_20250305` otherwise). Server-executed
  `server_tool_use`/`web_search_tool_result` blocks come back in the
  SAME response; a `pause_turn` stop (the provider's internal loop hit
  its cap) is resumed inside `decide` by re-sending with the partial
  assistant content appended (capped at 5 resumes; usage summed, the
  metrics tap fires once). The pipeline's one-decision contract is
  untouched.
- **`ClaudeAgentDriver`** re-admits exactly `WebSearch` from the CLI's
  built-ins (`tools=["WebSearch"]`) and allows it — and only it —
  through the permission gate. It executes server-side within the
  session, so the search stays inside one decide and the ADR 007
  boundary still excludes everything that touches the host.

**Observability, not pipeline work**: server-executed calls surface as
`LLMTurn.server_tool_calls`. The pipeline never executes them; the turn
diary logs them beside the decision (no `tool_result` will follow) and
the WP19 activity stream renders them as already-resolved calls without
touching its FIFO result pairing.

## Consequences

- No firewall change, no new dependency, no self-built search stack.
- Modes are safe by default: an unticked checkbox keeps a mode exactly
  as grounded as before WP20. `/mode` reports `web search: on/off`.
- Cost rides the driver choice: subscription quota on the default path,
  API billing (per-search + tokens) on `anthropic_api`.
- ~~Known v1 limitation: when a searching decision ALSO emits local
  tool calls, the transcript rebuilt for the next decide omits the
  server-tool blocks.~~ **Retired by WP22 (2026-07-16).** The transcript
  stays provider-neutral, but a searching decision now carries its raw
  result payloads as opaque JSON (`TranscriptEvent.server_tool_calls` /
  `server_tool_results`, position-paired, turn-local like `thinking`):
  the API driver replays `server_tool_use` + raw result block pairs
  verbatim (`encrypted_content` untouched — the API's multi-turn
  requirement; an unpaired half is never sent, a dangling block is a
  400), and the subscription driver — whose fresh CLI sessions take
  text — replays each search as a fenced call + result-DIGEST pair
  (`search_digest`, single-homed in `driver_common`). The turn diary
  logs digests, never raw payloads.
- The subscription path's headless-WebSearch behavior is pinned by a
  gated live test (`tests/e2e/test_live_claude_driver.py`); if the CLI
  ever refuses WebSearch under OAuth subscription auth, web search
  degrades to the API driver only.
