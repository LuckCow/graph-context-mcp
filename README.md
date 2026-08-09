# graph-context-mcp

An MCP server **and agentic orchestrator** exposing a knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph is the source of truth; the LLM builds it and writes from it — and the assistant lives *inside* your Anytype spaces: it chats in-space, fires timed prompts ([scheduled events](#scheduled-events-and-automation-rules)), reacts to property changes with [automation rules](#scheduled-events-and-automation-rules) (including sandboxed Python scripts), and evolves your schema only with your 👍 ([schema proposals](#schema-changes-need-a-)).

The stack, storage core up: an async `GraphRepository` port with two certified implementations (in-memory fake and `AnytypeGraphRepository`, with hydrate/resync and self-write suppression), a FastMCP stdio server exposing **thirteen tools**, and an **orchestrator harness** above it — a real Claude driver on your subscription or the Messages API, in-space activity modes that pin their own model, thinking level, web-search access, and verbosity ([ADR 033](docs/adr/033-per-mode-model-selection.md)/[035](docs/adr/035-in-space-only-activity-modes.md)/[037](docs/adr/037-mode-level-driver-options.md)), automatic per-turn provenance with a durable process-trace card ([ADR 038](docs/adr/038-turn-trace-on-the-intent-node.md)), semantic search with graph-aware ranking ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)/[016](docs/adr/016-graph-aware-ranking.md)), and chat transports for Anytype's own in-space chat ([ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)) and Discord.

**Space-reflecting ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)):** the server reflects your *existing* Anytype space — native types (`character`, `event`, …) are nodes and every `objects`-format relation is a labelled edge. There is no closed `gc_` vocabulary; `gc_` keys survive only for infrastructure. Everything the server writes is a REAL Anytype property or the object body ([ADR 028](docs/adr/028-native-properties-everywhere.md)): summaries live in the built-in `description` property ([ADR 011](docs/adr/011-summary-in-builtin-description.md)), long-form descriptions in the body ([ADR 010](docs/adr/010-descriptions-in-the-body.md)) — visible, filterable, editable in the UI.

Docs: [`docs/adr/`](docs/adr/) (decisions), [`docs/WORK_PACKAGES.md`](docs/WORK_PACKAGES.md) (roadmap + status), [`docs/TESTING.md`](docs/TESTING.md) (suites, live E2E, golden snapshots, behavioral evals, demo scripts).

```bash
pip install -e ".[dev]"    # Python >= 3.11
pytest                     # mock-backed suite; no live server needed
```

Try it without Anytype: `PYTHONPATH=src python scripts/demo_wp2_tools.py` drives the full tool loop in-process against the mock-backed repository.

## Running the MCP server

The server speaks **stdio** (one process per client; no network port). Run it directly only for a quick local check:

```
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server   # dev: in-memory, nothing persists
```

The thirteen tools:

- **Graph:** `create_node`, `update_node`, `edit_document` (hash-anchored single-section body edits, ADR 050), `get_node`, `explore`, `find_path`, `find_node`, `query`
- **Session:** `context` (scratchpad + working set + cold-start `overview` map)
- **Automation & schema:** `schedule` (timed prompts), `automation` (reactive rules), `schema` (draft type changes), `send_file`

Every node parameter accepts a node **name** as well as an id (ambiguous names report their candidates); validation errors echo the allowed values — they are written for an LLM to self-correct. Tool docstrings are prompts (`interface/server.py`). **Cold start:** `context action="overview"` returns a derived entry-point map (per-type counts + highest-degree hubs) to seed the first `explore`/`get_node`/`focus`.

Two surfaces worth calling out:

- **One `properties` dict on the write tools ([ADR 042](docs/adr/042-unified-properties-surface.md)).** Scalars and relations share a single map on `create_node`/`update_node`, discriminated by what the space says the key IS: a key naming an existing `objects` relation takes a node id/name (or a list) and becomes link(s); everything else is a scalar. New keys are declared via `create_missing_properties` (format-explicit, type-attached so rules can watch them); the old `fields`/`links`/`create_missing_*` parameters answer with a redirect, never an opaque error.
- **`query` runs ad-hoc filters *and* your saved Set views ([ADR 018](docs/adr/018-client-side-query-engine.md)).** Ad-hoc: ANDed `where` predicates, multi-key `order_by`, `linked_to` neighbor anchoring — all on the in-memory engine. `query(view=...)` compiles an Anytype Set view (the filters/sorts you maintain in the desktop UI) into the same engine.

## Running the orchestrator (CLI / Discord / Anytype chat)

The orchestrator is the agentic harness over the same tool surface: a driver decides, activity modes bind tools, provenance records each working turn. Every transport shares one runtime assembly (`orchestrator/bootstrap.py`) and differs only in its message loop.

```
python -m graph_context.orchestrator.serve                                  # everything: Anytype bot + Discord (if configured) + inspection server
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.orchestrator.cli   # keyboard loop; dev backend
python -m graph_context.orchestrator.discord_bot                            # Discord bot standalone
python -m graph_context.orchestrator.anytype_chat_bot                       # Anytype in-space chat bot standalone
```

`serve` is the consolidated entry point: one process running the Anytype chat bot (always), the Discord bot (only when the token file has content **and** at least one channel is bound — an empty secret file or a zero-table channels file is the "Discord off" switch), and the inspection server in a daemon thread. One transport's crash takes the whole process down loudly; restarts belong to the supervisor.

`GC_DRIVER=anthropic_subscription` (default) talks to the model on your Claude subscription; `GC_DRIVER=anthropic_api` talks to it over the Anthropic Messages API instead — an explicit opt-in that bills API credits and requires `ANTHROPIC_API_KEY` (inline citations for web search are API-driver-only); `GC_DRIVER=manual` is the keyboard stand-in (`/tool <name> {json}`). `GC_DRIVER_MODEL` / `GC_DRIVER_EFFORT` set deployment defaults, but **the active mode outranks them**: each Activity Mode object can pin its model, thinking level, output cap, and search limits (below). Provenance is on by default (`GC_PROVENANCE=0` disables; `GC_STORE_LLM_INPUT=0` withholds prompt text from intent nodes); a turn that ran tools, searched, or produced thinking also cards its full background process on the reply via the intent node's `### gc:process` section ([ADR 038](docs/adr/038-turn-trace-on-the-intent-node.md)).

### Activity modes live in the space (ADR 035)

The space's **Activity Mode** objects are the ONLY live source of modes: every object of the bootstrap-minted type defines one mode, and a space that has none is **seeded once** with a starter corpus (`src/graph_context/interface/mode_seeds/*.toml`, selected by profile; `GC_MODES_FILE` or a binding's `modes_file` replaces the packaged corpus as the **seed source** — it never merges at load). After seeding, edit the objects in Anytype:

- The object **name** becomes the `/mode` name ("Faithful Scribe" → `/mode faithful_scribe`); the **page body** is the goal prompt the model follows; **archive** the object to disable the mode (archive or delete *every* mode object and the next restart reseeds).
- **Behavior fields:** tick `gc_mode_mutating` to allow graph edits; fill `gc_capture_type` (plus optional `gc_capture_references`, `gc_capture_min_chars`) to auto-capture substantial replies.
- **Driver fields ([ADR 033](docs/adr/033-per-mode-model-selection.md)/[037](docs/adr/037-mode-level-driver-options.md)):** `gc_mode_model` pins the model (`sonnet 5` | `opus 4.8` | `fable 5`; empty = deployment default), `gc_mode_thinking` the thinking effort, `gc_mode_max_tokens` the output cap.
- **Web search ([ADR 030](docs/adr/030-mode-gated-web-search.md)):** the `gc_mode_web_search` checkbox admits Anthropic's server-side search tool for the mode (searches run on Anthropic's side, never on this host), with `gc_mode_search_max_uses` and allowed/blocked domain lists.
- **Activity streaming ([ADR 029](docs/adr/029-live-turn-activity-streaming.md)):** `gc_mode_activity_detail` (`off | minimal | tools | full`, default `minimal`) sets how much of a running turn streams into the chat.
- **Edits go live by themselves ([ADR 044](docs/adr/044-unified-change-tick-and-mode-auto-refresh.md)):** the bot's unified change tick (default 5s, `GC_CHANGE_TICK_SECONDS`) notices edited mode objects and reloads the registry — no `/mode`, no restart. `/mode` remains the unconditional reload.
- The mode **new chats start in** is in-space too ([ADR 034](docs/adr/034-space-context-default-mode.md)): the bootstrap-seeded **Space Context** object carries a *Default mode* link — point it at an Activity Mode object (the seeder links the corpus's marked default for you). Empty = the alphabetically first mode; chats that already picked a mode with `/mode` keep it.

### Anytype chat (the all-in path)

The bot chats *inside* your Anytype spaces — the same store that holds the graph ([ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)). Served spaces are declared in `spaces.toml` (`GC_SPACES_FILE`), keyed by space id, with optional `profile` / `project` / `modes_file` / `chat_id` / `exclude_chats`; every chat in a bound space is its own session/thread ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)), with live discovery of new chats and its own persisted mode. The chat cursor persists (`GC_CHAT_CURSOR`), so messages sent while the bot was down are answered on the next startup. The bot runs on its own headless node (the `anytype` compose sidecar) and posts as `graph-context-bot`. Never bind one space in both `spaces.toml` and `channels.toml`. Setup: see [Graduating to the live Anytype backend](#graduating-to-the-live-anytype-backend).

What a conversation gets:

- **Real formatting ([ADR 036](docs/adr/036-chat-text-formatting-marks.md)):** outbound markdown converts to Anytype text marks — clickable links, bold/italic/code — with every referenced object attached as a card (the turn's intent node first, [ADR 038](docs/adr/038-turn-trace-on-the-intent-node.md)).
- **Live turn activity ([ADR 029](docs/adr/029-live-turn-activity-streaming.md)):** while a turn runs, progress streams into one edited-in-place activity message at the mode's `activity_detail`; the reply posts fresh and the trace message is deleted once delivered (the turn log and the intent-node card keep the record).
- **Files both ways ([ADR 032](docs/adr/032-chat-files-to-and-from-the-model.md)):** attach a text file or image and it reaches the model (text folds in fenced, images as native image blocks); the model's `send_file` tool uploads and attaches file cards on the reply.
- **Self-titling chats ([ADR 031](docs/adr/031-chat-auto-titling-and-default-mode.md)):** after an untitled chat's first real exchange the bot names it; human titles are never overwritten.
- **Capped live streams ([ADR 043](docs/adr/043-activity-capped-chat-streams.md)):** only the `GC_CHAT_STREAM_CAP` (default 20) most recently active chats per space hold live SSE streams; the rest hibernate but stay fully served — a new message wakes a chat within one rescan tick and is answered from catch-up. `0`/`off` streams everything.

### Scheduled events and automation rules

Both are **nodes in your space** — authored either by the LLM (the `schedule` / `automation` tools) or by you, directly in the Anytype UI (each type seeds an explainer object that walks through the fields). Only the Anytype bot executes them.

- **Scheduled Events ([ADR 027](docs/adr/027-scheduled-events.md)):** a `gc_scheduled_event` node carries a schedule (one-shot local datetime or five-field cron), a prompt, a status select (Pending fires; flip Cancelled back to Pending to re-arm), and the target chat's session key. When due, the bot injects the prompt as a turn in that chat — at-most-once, with downtime collapsing to a single late fire. `GC_TIMEZONE` sets the local zone.
- **Automation Rules ([ADR 039](docs/adr/039-reactive-rule-engine.md)):** a `gc_rule` node watches ONE scalar property on ONE type (built-ins like the modified stamp included) and runs an action on a value transition — `set property to now`, `set property value`, `uncheck others of type`, or **`run script`** ([ADR 040](docs/adr/040-sandboxed-script-action.md)): the rule body's first ```python block runs in a locked-down subprocess (no network, no filesystem, rlimits, `GC_RULE_SCRIPT_TIMEOUT_SECONDS` default 5s) against a read-only graph snapshot and queues up to 20 validated effects. Rules fire at-most-once per transition, never on restart, and never on the engine's own writes — **rules cannot cascade, by construction**. Failures land in `gc_rule_last_error`; the `automation` tool's `test` action dry-runs a rule through the real sandbox and reports `would set …` lines without applying anything.

### Schema changes need a 👍 (ADR 041)

The space's vocabulary stays yours. The `schema` tool lets the model **draft** a new type or new properties on an existing type — but the tool has no apply action. Each draft posts to the chat as its own message ("React 👍 to APPLY / 👎 to dismiss"); your 👍 applies it with no model turn in between, 👎 dismisses, and the bot's own reactions are ignored. Applying reuses same-name/same-format properties, refuses conflicts loudly, and registers the result live so the model can `create_node` against the new type immediately. Restart clears pending drafts (a stale 👍 is inert); Discord/CLI can draft but tell you to confirm in Anytype chat; the bare MCP server drafts but can never apply.

### Discord

The bot reads its token from `DISCORD_BOT_TOKEN_FILE` and serves **only** the channels you configure (no channel config = serve nowhere, loudly). Two configuration shapes ([ADR 017](docs/adr/017-channel-bound-spaces.md)) — setting both fails at startup:

- **`GC_DISCORD_CHANNELS`** (legacy allowlist): every listed channel shares the one env-configured runtime (`ANYTYPE_SPACE_ID`, `GC_PROFILE`).
- **`GC_CHANNELS_FILE`** (channel-bound spaces): a TOML file mapping each channel to its **own Anytype space** with optional per-channel `profile`, `project` label, and `modes_file`; each channel gets a fully independent runtime. One channel per space.

```toml
[channels.1523551542123298896]
space_id   = "bafyre..."           # required
profile    = "fiction"             # optional; defaults to GC_PROFILE
project    = "Ashfall"             # optional cosmetic label
modes_file = "ashfall-modes.toml"  # optional; the seed source for a mode-less space (ADR 035)
```

It connects outbound via the Gateway websocket, so it runs inside the firewalled devcontainer; the **Message Content** privileged intent must be enabled in the Discord developer portal or every message arrives empty.

### Turn log & inspection server

Every turn — user message, each model decision, every tool call with complete output, the mode/system prompt and per-turn context block the model actually received, per-decide token usage and cost, final replies — is written to a size-capped JSONL diary: `GC_TURN_LOG` sets the path (default `logs/turns.jsonl`; `0` disables), `GC_TURN_LOG_MAX_BYTES` the cap (default ~10 MB, oldest entries drop).

The inspection server ([ADR 025](docs/adr/025-inspection-server.md)) runs automatically inside `serve` (the devcontainer publishes it on the host at port 8765 — see [Running it in the container](#running-it-in-the-container) for who can reach that), or standalone via `python -m graph_context.orchestrator.inspect_server`. It is dependency-free, carries one shared section nav across its pages (any surface reaches any other; the site map lives in `static/nav.js`, so a new page is one entry there and one route), and serves three things: an **eval dashboard** at `/` (every case with its latest verdict and history, per-run grade/judge/prompt detail, one-click trial transcripts — `GC_EVAL_ROOT` points it at the artifacts, default `evals`), the **live turn-log viewer** at `/logs`, grouping the diary into one collapsible card per user request and live-tailing via SSE (filter by session/mode, search, errors-only), and the **prose editor** at `/prose` ([ADR 054](docs/adr/054-prose-editor-and-raw-indexing.md), building on [050](docs/adr/050-section-review-and-prose-page.md)–[053](docs/adr/053-in-page-prose-editing.md); mobile-friendly): one CodeMirror 6 editor over each tracked document's raw markdown — type anywhere and autosave coalesces the session into one revision, select text to set status/intent marks down to exact words (locked text is enforced verbatim against the model), with native browser spellcheck on (advisory only — autocorrect stays off so nothing rewrites your prose into the revision log), and human-typed words, review state, and per-paragraph blame rendered as live highlights that follow the text while you type, revision timelines with word-level diffs, and SSE-driven live updates when the bot edits a document you have open (`serve` only — the standalone server has no live spaces and shows an empty state). Reads need no auth; saving and marking are the server's only writes and need `GC_PROSE_TOKEN` set (or `GC_PROSE_TOKEN_FILE` pointing at a mounted secret — the devcontainer wires `.devcontainer/secrets/gc_prose_token`; neither set = the page is read-only) plus a same-origin request — the page asks for the token once and remembers it. Point the server elsewhere with `--log` / `--port` / `--eval-root` or `GC_LOG_VIEWER_HOST` / `GC_LOG_VIEWER_PORT`. No server needed for the diary either: open `src/graph_context/orchestrator/turn_log_viewer.html` directly in a browser and pick a `turns.jsonl` file (the nav is absent there — there is no server to navigate to).

### Running it in the container

The compose stack starts the orchestrator for you. The container's boot sequence is, in a mandatory order: the egress firewall (it flushes all of netfilter, so nothing that installs rules may precede it) → tailscale → the orchestrator → `sleep infinity`, so a crashed server still leaves a container you can exec into.

`gc-serve` is the process control, and **the answer to "I changed the code, now what"**:

```bash
docker exec graph-context-mcp-dev gc-serve restart   # pick up source edits
docker exec graph-context-mcp-dev gc-serve status    # pid + last log lines
docker exec -it graph-context-mcp-dev gc-serve logs  # tail -f
docker exec graph-context-mcp-dev gc-serve stop      # then run it in the foreground yourself
```

The source is a bind mount and `PYTHONPATH` points into it, so a restart is all any source change needs — no image rebuild, no `compose down/up`. Only dependency changes (`pyproject.toml`) or Dockerfile edits need `up -d --build`. Set `GC_SERVE_AUTOSTART=0` to boot without the server; logs go to `logs/serve.log`.

The server runs as the unprivileged `dev` user, never as root — root is exempt from the egress firewall so that `tailscaled` can reach its control plane, and a root-run orchestrator would inherit that exemption and silently bypass the allowlist.

### Remote access over Tailscale

The container can join a tailnet as its own node, which is how you reach the prose editor from a phone or run this on a VPS without exposing anything publicly. Put a **reusable, non-ephemeral, tagged** auth key in `.devcontainer/secrets/tailscale_authkey` (see [secrets/README.md](.devcontainer/secrets/README.md)) and rebuild. With the file empty — the default — nothing joins anything and the container behaves exactly as it did before.

`start-tailscale.sh` runs `tailscaled` in **userspace networking** mode: no `/dev/net/tun`, and no iptables rules of its own, so it coexists with the egress firewall rather than fighting it (the firewall stays safe to re-run at any time). It then runs `tailscale serve`, which terminates TLS with a real certificate and proxies port 8765, so the inspection server is at `https://graph-context-mcp.<your-tailnet>.ts.net/` — this also gets the `GC_PROSE_TOKEN` off the wire in plaintext. That last step needs MagicDNS and HTTPS certificates enabled in the tailnet admin console (DNS tab); without them the node still joins and the port is still reachable by tailnet address. Tune with `TS_HOSTNAME`, `TS_SERVE_PORT` (off-value = join but publish nothing), and `TS_UP_EXTRA_ARGS` (e.g. `--advertise-tags=tag:vps --ssh`).

Two deliberate limits. **Egress for `tailscaled` is granted by UID, not by destination**: its control plane is behind anycast, its DERP relay fleet rotates, and NAT traversal dials arbitrary peer UDP endpoints, so an IP allowlist would be stale within a week and would fail as "the tailnet works from *some* networks". Root gets unrestricted egress instead, and `/etc/sudoers.d/firewall` — two entries — is what keeps "root" meaning "tailscaled and nothing else". **Userspace mode is inbound-only in practice**: the container can be *reached* over the tailnet, but reaching *out* to tailnet peers needs `--socks5-server`, or TUN mode with `/dev/net/tun` mounted and `--netfilter-mode=off`.

Never use `tailscale funnel` here. Same command shape, but it publishes to the open internet, and the inspection server's reads — the entire turn diary, every prose document — are unauthenticated. `serve` is tailnet-only; that is the one you want. For the same reason, a public-facing VPS should pin the compose `ports:` back to `127.0.0.1` (or drop them entirely, since the tailnet path does not use them).

## Connecting Claude Desktop (from the dev container)

Claude Desktop runs on your **host**; the server runs **inside the dev container**, so Claude Desktop starts it *inside the already-running container* over stdio with `docker exec -i`.

**1. Start the container** (it must be running before Claude Desktop launches the server):

```
docker compose -f .devcontainer/docker-compose.yml up -d --build
```

**2. Add the server to Claude Desktop's config** (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows: `%APPDATA%\Claude\claude_desktop_config.json`; a copy-paste entry lives at [`.devcontainer/claude_desktop_config.example.json`](.devcontainer/claude_desktop_config.example.json)):

```json
{
  "mcpServers": {
    "graph-context": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "GC_BACKEND=memory",
        "graph-context-mcp-dev",
        "python", "-m", "graph_context.interface.server"
      ]
    }
  }
}
```

**3. Restart Claude Desktop.** You should see the thirteen tools in the tools menu. This first smoke test uses `GC_BACKEND=memory` — no Anytype, nothing persists. `docker` must be on Claude Desktop's `PATH`.

### Graduating to the live Anytype backend

`GC_BACKEND=anytype` (the default) talks to the **headless Anytype sidecar** — the `anytype` compose service running a bot account. The container already wires everything (`ANYTYPE_API_BASE_URL=http://anytype:31012`, `ANYTYPE_API_KEY_FILE`, `ANYTYPE_SPACE_ID`), and `docker exec` inherits it all, so the live backend needs **no `-e` flags**: drop the `GC_BACKEND=memory` override from the JSON above and you're live. To point at a different space, add `-e ANYTYPE_SPACE_ID=…`. Your desktop app is the *human* surface: it shares spaces with the bot over Anytype's sync network and never talks to this stack directly.

#### One-time sidecar bootstrap (bot account)

The sidecar runs `anytype serve` with the API on port 31012 and the write rate limit disabled. Its identity/config (`~/.config/anytype`) and object store (`~/.anytype`) are named volumes, so they survive rebuilds; **both** mounts are required — with only one, a rebuild wipes the bot's keys. Setup, from the host:

```bash
# 1. Build + start the stack (the sidecar is part of it)
docker compose -f .devcontainer/docker-compose.yml up -d --build

# 2. Create the bot account (once) and an API key
docker exec -it graph-context-mcp-anytype anytype auth create graph-context-bot
docker exec -it graph-context-mcp-anytype anytype auth apikey create "graph-context"
```

Back up the **account key** printed by `auth create` to `.devcontainer/secrets/anytype_account_key` (it is the bot's identity), and paste the **API key** into `.devcontainer/secrets/anytype_api_key`. Then run `docker compose … up -d` once more so `dev` remounts the key (secret mounts go stale when the file's inode changes).

#### Sharing spaces with the bot

For every space the bot should serve: create an invite link in the desktop app, then

```bash
docker exec -it graph-context-mcp-anytype anytype space join "<invite-link>"
docker exec -it graph-context-mcp-anytype anytype space list   # wait until synced
```

approve the join request in the desktop app and grant **Editor**. Space ids are identical for every member, so copy them straight into `spaces.toml` (chat transport) or `channels.toml` (Discord) — never both for one space. Sanity check from the dev container:

```bash
curl -s http://anytype:31012/v1/spaces -H "Anytype-Version: 2025-11-08" \
  -H "Authorization: Bearer $(cat /run/secrets/anytype_api_key)"
```

## Semantic search (GC_EMBEDDER)

"Find the node I'm describing" — as a **derived projection**, never a new source of truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)). `GC_EMBEDDER` selects the embedder: `off` (default), `hash` (deterministic, used by the test suite), or `local` (sentence-transformers over the image-baked `BAAI/bge-small-en-v1.5`). Embeddings live in a disposable SQLite cache (`GC_SEMANTIC_CACHE`) keyed by `(node_id, content_hash, model)`.

When enabled, `find_node` gains a third tier (exact → substring → semantic, hits labelled so the LLM knows it holds a fuzzy match) and `NodeNotFound` errors append "closest by meaning" candidates. Retrieval runs through the **Ranker** ([ADR 016](docs/adr/016-graph-aware-ranking.md)): semantic recall seeds, graph expansion recruits, spreading activation scores, and every hit carries a nameable evidence line. **Non-feature by decision:** semantic never silently resolves a mutation target — exact resolves, semantic *suggests*.

## Domain profiles (GC_PROFILE) — deprecated

*Deprecated ([ADR 035](docs/adr/035-in-space-only-activity-modes.md) / WP27) — don't build on them.* A profile (`fiction` default | `workspace` | `assistant`) once selected framing wholesale; what remains is the tool-docstring framing the LLM reads, type-key → role mappings, the timeline property (`gc_story_time` story numbers vs `event_date` real ISO dates), and **which starter mode corpus seeds a mode-less space**. Tool names and parameters are identical across profiles. The remainder will collapse to neutral defaults or move into space/deployment config; new configuration belongs on the Space Context object or in deployment config.

## Architecture in one paragraph

Anytype is durable storage and the *human editing surface* — but its API only text-searches names/snippets, so all graph traversal happens in an in-memory `GraphIndex`: a **derived, rebuildable projection** that repository adapters keep coherent (write-through on our writes, hydrate/resync for edits humans make directly in the Anytype UI). Everything above storage follows a strict dependency rule:

```
interface  ──▶  application  ──▶  domain
   (MCP tools,      (use-cases,       (pure logic:
    presenters)      one per tool)     graph, traversal,
                          │            schema, session)
                          ▼
                       ports  ◀──implemented by──  infrastructure
                  (GraphRepository)                 (in-memory fake +
                                                     Anytype adapter)
```

**The rule:** imports only point left-to-right along the arrows. Nothing imports infrastructure except the composition roots — `interface/server.py` and the orchestrator's (`cli.py`, `bootstrap.py`, `discord_bot.py`, `anytype_chat_bot.py`, and `serve.py`), all delegating to the shared service builder `composition.py` — and tests. The orchestrator is a **second interface adapter** ([ADR 007](docs/adr/007-orchestrator-second-interface-adapter.md)): it reuses `interface/tools.py` but never the MCP module, and agent/transport frameworks (claude-agent-sdk, discord.py) never leak outside it. All of this is machine-enforced: import-linter contracts in `pyproject.toml` fail CI on violation.

## Layout

| Path | Role | Key idea |
|---|---|---|
| `domain/schema.py` | Open type vocabulary + semantic `Role` layer | Types/edges are whatever the space has ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)); an editable type-key→Role map drives timeline/`as_of` and infra-hiding |
| `domain/overview.py` | Derived cold-start map | Per-type counts + highest-degree hubs; rebuilt per call, nothing maintained |
| `domain/models.py` | `Node`, `Edge`, `NodeDraft`, `LinkSpec` | Immutable; ids minted by storage, hence draft vs node |
| `domain/graph.py` | `GraphIndex` adjacency projection | The traversal engine's substrate; rebuildable, never authoritative |
| `domain/traversal.py` | Bounded BFS (`explore`) | Pure function; filters prune subtrees; `as_of` hides future events |
| `domain/pathfinding.py` | Bounded shortest path (`find_path`) | Undirected walk, direction-preserving result |
| `domain/query.py` | Pure query engine ([ADR 018](docs/adr/018-client-side-query-engine.md)) | One engine for ad-hoc `query` calls and compiled Set views; `neq` matches absent fields |
| `domain/fields.py` | Field-value coercion, in ONE place | Both backends parse/error/read back identically; contract-pinned |
| `domain/session.py` | `FocusStack`, `RecentHistory`, `SessionState` | Working *set* not a pointer; pinning; top never evicted |
| `domain/scheduling.py` | Schedule parsing + next-fire math ([ADR 027](docs/adr/027-scheduled-events.md)) | Dependency-free cron + one-shot datetimes; recurring events arm on first fire |
| `domain/rules.py` | Automation Rule vocabulary + matching ([ADR 039](docs/adr/039-reactive-rule-engine.md)) | Pure `parse_rule_fields`/`condition_met`; lenient status select; errors echo the allowed words |
| `domain/attribution.py` | Generation-provenance property keys ([ADR 028](docs/adr/028-native-properties-everywhere.md)) | Who/what/when stamps as REAL properties, never a JSON side-channel |
| `domain/activity.py` + `domain/model_choice.py` + `domain/thinking_choice.py` | Mode-level vocabularies ([ADR 029](docs/adr/029-live-turn-activity-streaming.md)/[033](docs/adr/033-per-mode-model-selection.md)/[037](docs/adr/037-mode-level-driver-options.md)) | Activity detail levels, canonical model choices, thinking levels |
| `ports/graph_repository.py` | Persistence contract | Composite-create **rollback contract**; `fetch_body`; `create_type`/`add_type_properties` for confirmed schema changes ([ADR 041](docs/adr/041-schema-proposals.md)) |
| `ports/session_store.py` | Keyed session-snapshot contract | Plain-dict snapshots per required key ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)); lenient load (corrupt → `None`) |
| `ports/mode_store.py` | Activity-Mode config contract | Plain payload dicts; validation lives in the loader, not the store |
| `ports/space_context_store.py` | Space-settings singleton contract ([ADR 034](docs/adr/034-space-context-default-mode.md)) | Payloads carry the default-mode link targets; the loader owns the singleton rule |
| `ports/view_catalog.py` | Saved Set views, compiled and runnable ([ADR 018](docs/adr/018-client-side-query-engine.md)) | A view is a saved query the human maintains; implementations compile, never re-query |
| `ports/script_runner.py` | Sandboxed script execution contract ([ADR 040](docs/adr/040-sandboxed-script-action.md)) | Script + snapshot in, queued effects out; every failure is a legible `gc_rule_last_error` message |
| `ports/semantic.py` | `Embedder` + `SemanticIndex` contracts | Embeddings are a cache keyed by content hash + model, never truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)) |
| `application/node_writer.py` | `create_node` / `update_node` use-case | Owns the summary-staleness rule; touches focus |
| `application/node_reader.py` | `get_node` use-case | Grouped edges + `include_provenance` excerpts |
| `application/explorer.py` | `explore` / `find_path` use-case | Resolves focus-stack defaults |
| `application/querier.py` | `query` use-case | Ad-hoc predicates straight off the tool surface, or a compiled saved view |
| `application/scheduler.py` | Scheduled Events use-case ([ADR 027](docs/adr/027-scheduled-events.md)) | Fire = mark the node, then inject a turn; at-most-once, downtime collapses to one late fire |
| `application/rule_engine.py` | `RuleEngine` tick over Automation Rules ([ADR 039](docs/adr/039-reactive-rule-engine.md)) | In-memory baseline diff: transitions not states; the engine's own writes never trigger rules |
| `application/schema_proposals.py` | Draft ledger for schema changes ([ADR 041](docs/adr/041-schema-proposals.md)) | Session-scoped, cap 5, drafts not records; apply happens only after a human 👍 |
| `application/capture_recorder.py` | Capture service (orchestrator-called) | Policy-typed artifacts ([ADR 015](docs/adr/015-configurable-activity-modes.md)); native types are first-class, only `gc_prose` keeps infra hiding |
| `application/mutation_journal.py` | Writers report created/modified ids at the source | `NullJournal` in the MCP server; drained per turn in the orchestrator |
| `application/intent_recorder.py` | One `gc_intent` node per working turn | Provenance is a harness responsibility ([ADR 008](docs/adr/008-provenance-as-harness-responsibility.md)); carries the process trace ([ADR 038](docs/adr/038-turn-trace-on-the-intent-node.md)) |
| `application/semantic_projector.py` | The embedding cache tracks the graph | Full pass + prune after hydrate; incremental from resync; store touches never re-embed |
| `application/ranker.py` | Graph-aware retrieval ([ADR 016](docs/adr/016-graph-aware-ranking.md)) | Recall seeds → graph recruits → activation scores; every hit carries evidence |
| `application/session_persister.py` | Debounced session persistence | Flush every N / on shutdown; lenient `load_or_fresh`; keyed |
| `application/session_registry.py` | The one source of live sessions ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)) | Lazy keyed `(SessionState, persister)` cache; `flush_all` at teardown |
| `composition.py` | Shared service builder | One wiring; all composition roots delegate to it |
| `infrastructure/memory/` | In-memory repository, session/mode/space-context stores, view catalog | Reference impls; certified by `tests/contract` |
| `infrastructure/semantic/` | Hash + sentence-transformers embedders; memory + SQLite index | `GC_EMBEDDER` selects; the SQLite cache file is disposable |
| `infrastructure/sandbox/` | The script sandbox ([ADR 040](docs/adr/040-sandboxed-script-action.md)) | `bootstrap.py` = the rlimited `python -I -S` child (stdlib-only, importable for tests); `runner.py` spawns, caps, and kills it |
| `infrastructure/anytype/client.py` | Async httpx client | Auth, version pin, pagination, bounded retry; `request_count` for budget asserts |
| `infrastructure/anytype/mapping.py` | The quirk quarantine | All representation assumptions (A1–A14) live here |
| `infrastructure/anytype/registry.py` | `SpaceRegistry`: the space's live types/relations | Resolves requested types & relation labels to existing keys; unknown labels surface for approval |
| `infrastructure/anytype/schema_bootstrap.py` | Idempotent **infra-only** bootstrap | gc_ infra types (Prose, SessionContext, Activity Mode, Scheduled Event, Automation Rule), scalar gc_ properties, starter `gc_edge_*` relations — story entities use the space's native types |
| `infrastructure/anytype/sync.py` | Hydrate / resync engine | Lenient reads, strict writes; search-based modified-since |
| `infrastructure/anytype/repository.py` | `AnytypeGraphRepository` | Persist-first write-through; composite rollback; self-write suppression |
| `infrastructure/anytype/session_repository.py` | `AnytypeSessionStore` | Snapshot JSON in a per-key `SessionContext` node (discriminated by `gc_session_key`) |
| `infrastructure/anytype/mode_seeder.py` | Starter-mode heal ([ADR 035](docs/adr/035-in-space-only-activity-modes.md)) | Seeds a space with ZERO Activity Mode objects, links the default, never touches a space that has any |
| `infrastructure/anytype/mode_store.py` | `AnytypeModeStore` | One mode per `gc_activity_mode` object: name → `/mode` slug, page body → goal, archive = disable |
| `infrastructure/anytype/space_context_store.py` | `AnytypeSpaceContextStore` | The `gc_space_context` singleton's `gc_default_mode` link → the mode new chats start in |
| `infrastructure/anytype/view_catalog.py` | `AnytypeViewCatalog` ([ADR 018](docs/adr/018-client-side-query-engine.md)) | Compiles Set views into `NodeQuery` values; a view-definition source, never a second query engine |
| `infrastructure/anytype/chat.py` | Chat quirk quarantine + `AnytypeChatClient` | Chat payload/SSE assumptions (C1–C13); the chat analogue of `mapping.py` |
| `infrastructure/anytype/marks.py` | Markdown → text marks ([ADR 036](docs/adr/036-chat-text-formatting-marks.md)) | UTF-16 offsets, bounds-validated (an invalid range 500s); malformed syntax degrades to literal text |
| `infrastructure/anytype/mock_server.py` | `MockAnytype` | Spike-pinned behavior simulator (search caps, body-editing quirks, timestamps, chat routes + live SSE + reactions) |
| `interface/presenters.py` | Detail levels + node/path views | Response-budget shaping lives at the edge, not in tested logic |
| `interface/tools.py` | The thirteen tools (SDK-free) | `guarded` wrapper: actionable errors + per-call logging |
| `interface/tool_args.py` | Tool-parameter parsing | Every error echoes the allowed values — written FOR the LLM to self-correct |
| `interface/services.py` | The `Services` bundle | Built once per runtime; re-derived per chat session — sessions are cheap views, never runtimes |
| `interface/context_block.py` | Turn-start context block ([ADR 020](docs/adr/020-curated-cross-turn-context.md)) | Scratchpad + working-set buckets + recent trail, once per turn, budget-degraded |
| `interface/profiles.py` | Domain profiles (DEPRECATED, [ADR 035](docs/adr/035-in-space-only-activity-modes.md)/WP27) | Docstrings are prompts; golden-pinned per profile |
| `interface/mode_config.py` | Mode validation seam + seed-TOML parser ([ADR 035](docs/adr/035-in-space-only-activity-modes.md)) | One payload shape feeds the memory store, the Anytype seeder, and the eval runner |
| `interface/mode_seeds/` | Packaged starter corpora (one TOML per profile) | Seeds a mode-less space once; never merged at load |
| `interface/server.py` | MCP composition root | Only module importing the MCP SDK; lifespan wiring |
| `orchestrator/pipeline.py` | `handle_message` turn loop | Per-turn tool budget; opens with the context block + conversation memory (`/clear` resets it); drains the journal into an intent node at turn end |
| `orchestrator/modes.py` | `ModeSpec` loader (in-space ONLY, [ADR 035](docs/adr/035-in-space-only-activity-modes.md)) | Unbound tools don't exist in the session — unavailable, not refused; `mode_fingerprint` gates the auto-refresh ([ADR 044](docs/adr/044-unified-change-tick-and-mode-auto-refresh.md)) |
| `orchestrator/drivers.py` | `LLMDriver` seam + scripted/manual drivers | Transcript + tool docs + mode goal in; tool calls or a reply out |
| `orchestrator/claude_driver.py` | The subscription driver | claude-agent-sdk on your Claude plan; the SDK never executes tools — calls are harvested and returned as the decision |
| `orchestrator/anthropic_driver.py` | The Messages-API driver | Credit-billed opt-in; native web-search blocks, inline citations, verbatim search-result replay |
| `orchestrator/driver_common.py` | SDK-free shared driver logic | Same system prompt, tool schemas, transcript fencing — importable without either SDK |
| `orchestrator/capture.py` | Authoring auto-capture | Exact-name entity linking; the harness records what tools used to ask for |
| `orchestrator/process_trace.py` | The durable turn trace ([ADR 038](docs/adr/038-turn-trace-on-the-intent-node.md)) | `ActivityLog`'s archive-grade sibling; rendered once into the intent node's `### gc:process` |
| `orchestrator/turn_log.py` | Full-fidelity turn diary (JSONL) | Input, every driver decision, every tool call + complete output, usage/cost, final replies; byte-capped |
| `orchestrator/inspect_server.py` | Inspection server (eval dashboard + turn-log viewer + prose editor) | Stdlib-only; SSE live tail with shrink→reset; hosts the three pages + the vendored CodeMirror bundle (`static/`, first static route); the save/marks POSTs are the only writes (origin check + `GC_PROSE_TOKEN`, ADR 050/054); `turn_log_server.py` is its back-compat shim |
| `orchestrator/prose_bridge.py` | Prose editor ↔ bot-loop seam (WP43/48) | Registry of per-space handles + the SSE version ledger; every page read/write crosses via `run_coroutine_threadsafe` onto the owning loop, writes under the route lock |
| `orchestrator/eval_index.py` | Read-side index over eval artifacts | Tolerant json/tomllib scanning for the dashboard; never imports `evals` |
| `orchestrator/serve.py` | Consolidated composition root | One process: Anytype bot + Discord bot (if configured) + viewer thread; fail-together |
| `orchestrator/bootstrap.py` | Orchestrator runtime wiring | Shared by every transport; `GC_DRIVER` / `GC_PROVENANCE` / `GC_TURN_LOG` resolution; one runtime per channel or space binding |
| `orchestrator/channels.py` | Channel→space bindings (`GC_CHANNELS_FILE`, [ADR 017](docs/adr/017-channel-bound-spaces.md)) | Plain parsing/validation; one channel per space, enforced at startup |
| `orchestrator/discord_transport.py` + `discord_bot.py` | Discord adapter | Per-message policy is plain logic; only the composition-root shim imports discord.py |
| `orchestrator/spaces.py` | Space→chat bindings (`GC_SPACES_FILE`, [ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)/[021](docs/adr/021-per-chat-keyed-sessions.md)) | Table key IS the space id; serve-all-chats minus `exclude_chats`, or a `chat_id` pin |
| `orchestrator/rendering.py` | Shared reply rendering | `render` prefixes + `chunk`ing, shared by the chat transports |
| `orchestrator/anytype_chat_transport.py` + `anytype_chat_bot.py` | Anytype in-space chat adapter | Echo suppression, persisted cursor (offline catch-up), stream planning ([ADR 043](docs/adr/043-activity-capped-chat-streams.md)), reaction-confirmed applies, file upload/download; only the composition root touches infrastructure |
| `orchestrator/turn_activity.py` | Live turn activity ([ADR 029](docs/adr/029-live-turn-activity-streaming.md)) | Folds decisions/tool results into one edited-in-place chat message per the active mode's `activity_detail`; deleted once the reply is delivered |

## Conventions

The working conventions live in [CLAUDE.md](CLAUDE.md) (CLEAN principles and their repo-specific applications). The load-bearing ones: business rules live in exactly one place; the domain stays pure (no I/O, no clocks — `tests/unit` runs in milliseconds); fakes are contracts (adapter behavior lands in the fake too, or the port is wrong); every tool response and error string is written for an LLM to act on.

## Status & what's next

Full history and specs live in [`docs/WORK_PACKAGES.md`](docs/WORK_PACKAGES.md). Shipped, storage core up: the thirteen-tool MCP server and space-reflecting pivot; the query engine with saved Set views; semantic search + graph-aware ranking; the orchestrator harness with subscription and Messages-API drivers; automatic provenance, auto-capture, and the turn-trace card; per-chat keyed sessions; the Discord and Anytype chat transports (headless sidecar, live activity streaming, formatting marks, files both ways, auto-titling, activity-capped streams); in-space activity modes with per-mode model/thinking/web-search/verbosity and auto-refresh on edit; scheduled events; the reactive rule engine with sandboxed scripts; reaction-confirmed schema proposals; the unified `properties` write surface; and the behavioral eval harness + inspection server with the mobile-friendly prose editor (a CodeMirror page with live authorship/review highlights, autosaving inline edits, and locked sections). Definition of Done holds: `pytest`, `ruff`, `mypy --strict`, and `lint-imports` are all clean; CI runs exactly these on every push.

Open work, in rough order of proximity: WP27 profile retirement (collapse the deprecated `DomainProfile` remainder into neutral defaults + space/deployment config); the WP8 multi-user remainder (per-user sessions *within* one space, per-user mode authorization/consent, Telegram/Slack transports, queue fairness); WP11 stage 2 (passage-level search, reranker adapters, the Voyage embedder, the `off`→`local` embedder default flip, RAG prefetch); cross-turn driver memory (each `decide()` is deliberately a fresh stateless session for now); and the parked WP4 items (knowledge-query helper, staleness propagation).
