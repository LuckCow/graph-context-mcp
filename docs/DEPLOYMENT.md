# Deployment & host migration

How this stack is deployed, what state lives outside git, and what has to be
carried or recreated when it moves to another host (a VPS). Written from a
live desktop→VPS migration; the failure modes in
[Troubleshooting](#troubleshooting) are ones that actually happened here.

Related: [secrets/README.md](../.devcontainer/secrets/README.md) (what each
secret is and how to obtain it), [TESTING.md](TESTING.md) (what "verified"
means), [adr/025](adr/025-inspection-server.md) (the inspection server),
[adr/019](adr/019-anytype-chat-transport-and-headless-sidecar.md) (the
headless sidecar), [adr/059](adr/059-tailscaled-egress-by-uid.md) (why root is exempt
from the egress policy, and why the boot chain cannot hang).

## What actually runs

Two compose services (`.devcontainer/docker-compose.yml`):

| Service | Process | Purpose |
|---|---|---|
| `dev` | `python -m graph_context.orchestrator.serve` — ONE process | Anytype chat bot + Discord (when bound) + the inspection/prose server on `:8765` |
| `anytype` | headless Anytype node | The bot account's own node, serving the local API at `anytype:31012`. No published ports; compose-network only. |

The MCP stdio server (`interface/server.py`) is a *separate* entry point for
MCP clients and is not part of the always-on deployment.

## Boot order

The container command and `devcontainer.json`'s `postStartCommand` both run:

```
sudo init-firewall.sh && sudo start-tailscale.sh && gc-serve boot
```

The order is load-bearing, and each link has a different failure contract:

1. **`init-firewall.sh`** — default-deny egress. Must be first: it flushes all
   of netfilter, so anything started earlier would lose its rules. It is the
   one **hard gate** — `&&` means a firewall that cannot be established stops
   the chain, deliberately: no orchestrator without egress lockdown.
2. **`start-tailscale.sh`** — joins the tailnet and publishes `:8765` on it.
   Never fatal and never blocking: every network-touching call is wrapped in a
   timeout (`TS_NET_TIMEOUT`, default 45s), because `tailscale up` blocks
   indefinitely when the control plane is unreachable — and a hang here means
   the orchestrator never starts at all.
3. **`gc-serve boot`** — starts the orchestrator. Idempotent by design: both
   callers run it, and the second finds the first's process and no-ops.
   `GC_SERVE_AUTOSTART=0` skips it entirely — the switch to use when a
   supervisor (systemd, a compose `restart:` policy) should own the process
   instead of the boot chain. `gc-serve start|stop|restart|status|logs`
   manages it by hand; `restart` is also how you pick up code edits, since the
   source is a bind mount.

Both scripts install to `/usr/local/bin/` **at image build time**. Editing
them in `.devcontainer/` changes nothing until a rebuild (or a
`sudo cp` into place for the running container).

A healthy boot looks like this — the last three lines are the ones worth
recognizing:

```
[firewall] OK: default-deny active, GitHub reachable.
[firewall] anytype sidecar reachable at anytype:31012
[tailscale] serving :8765 at https://<node>.<tailnet>.ts.net/
[gc-serve] already running (pid N)        # or: running (pid N) -- logs: ...
```

## State that does NOT travel

Everything below is a named Docker volume on the host. `git clone` on a new
box gets you none of it. **This is the migration checklist.**

| Volume | Holds | If lost |
|---|---|---|
| `anytype-data` (`/root/.anytype`) + `anytype-config` (`/root/.config/anytype`) | The bot account's identity, config, and API-key store | The bot loses its account **and** its membership in every space. Recover from the `anytype_account_key` secret, then re-issue the API key. Both volumes are needed — the CLI splits state across them, and a rebuild wiped them once. |
| `claude-config` (`/home/dev/.claude`) | The Claude Code OAuth login — i.e. all model access on the subscription path | The orchestrator has no model. See [Model access](#model-access). |
| `tailscale-state` (`/var/lib/tailscale`) | The tailnet node identity | The node re-registers from the auth key, leaving a dead duplicate behind. Use a **reusable, non-ephemeral** key. |

The graph itself is **not** in this list: it lives in Anytype and syncs over
Anytype's network. What you are migrating is the machinery that reads it.

## Model access

Two paths, selected by `GC_DRIVER`:

**Subscription (current default).** `GC_DRIVER` unset or
`anthropic_subscription` (aliases: `claude`, `subscription`). Runs
`claude-agent-sdk` over the bundled Claude Code CLI, billed to the Claude
plan. Authentication comes from the OAuth login persisted in the
`claude-config` volume — which is why that volume is on the list above. On a
fresh host, either log in interactively once, or mint a token with
`claude setup-token` on a machine that is already logged in.

> **Known gap:** `.devcontainer/secrets/claude_oauth_token` is documented in
> `secrets/README.md` but is wired to nothing — it is not in compose's
> `secrets:` list and no code reads it. Auth today comes *only* from the
> volume. Wiring it to `CLAUDE_CODE_OAUTH_TOKEN` would give the headless path
> a fresh host actually needs.

**API (planned).** `GC_DRIVER=anthropic_api` (aliases: `anthropic`, `api`)
plus `ANTHROPIC_API_KEY`, and the `anthropic` extra installed. Bills API
credits, not the subscription — bootstrap refuses to start without an
explicit key rather than silently falling back to an OAuth profile, so the
billing switch is always a conscious choice.

The key is read from `ANTHROPIC_API_KEY_FILE` (compose mounts
`/run/secrets/anthropic_api_key`), falling back to `ANTHROPIC_API_KEY` in the
environment for host-local runs — the same file-first precedence every other
secret here uses, because env vars leak through `docker inspect`, `/proc`, and
child processes. The secret file must **exist** for compose to start; an empty
one is the supported "this deployment stays on the subscription" state, since
the billing gate reads it as no key at all.

`GC_DRIVER_MODEL` and `GC_DRIVER_EFFORT` tune the deployment default; per-mode
Activity Mode settings (ADR 033/037) override both. `GC_DRIVER=manual` drives
the loop by hand with no model at all — useful for debugging a new host.

## Network posture

- **Published ports are `127.0.0.1` only** (`8765` inspection; `8000` is
  vestigial — the MCP server speaks stdio and binds nothing, so nothing
  listens there). Keep it that way. The inspection server's **reads are unauthenticated** —
  the entire turn diary and every prose document — so widening the mapping on
  a public host publishes all of it. Writes are separately gated
  (same-origin + `GC_PROSE_TOKEN`), but that protects nothing that reads.
- `GC_LOG_VIEWER_HOST: 0.0.0.0` binds all interfaces *inside* the container
  only; Docker's published-port DNAT delivers to `eth0`, not loopback. The
  host-side `127.0.0.1` mapping is what keeps it private.
- **Remote access is the tailnet**, not an open port:
  `tailscale serve` terminates TLS at `https://<node>.<tailnet>.ts.net/` and
  proxies to `127.0.0.1:8765`. Requires MagicDNS **and** HTTPS certificates
  enabled in the tailnet admin console (DNS tab) — without them `serve` fails
  with `zero serverNoiseKey` and the node is reachable only by tailnet IP.
- **uid 0 is exempt from the egress policy** so tailscaled can reach its
  control plane and DERP relays by UID rather than by an unmaintainable
  destination list. The consequence: **never run the orchestrator as root** —
  it would inherit the exemption and bypass the allowlist the design rests on.
  `gc-serve` refuses to start as root for exactly this reason, and the
  firewall's own verification runs as the workload user, not as root.

## Timezone

`TZ` is pinned in compose (`America/New_York`). Containers default to UTC, and
scheduled events (ADR 027) and the rule engine's *set property to now*
(ADR 039) both mean the **user's** wall clock. Moving to a VPS in another
region without carrying `TZ` silently shifts every schedule and every
date stamp. `GC_TIMEZONE` overrides it independently if the two must differ.

## Moving to a VPS

1. Land and push the working tree; confirm CI is green. The box deploys from
   git, so anything uncommitted does not exist there.
2. Provision secrets (see `secrets/README.md`). `tailscale_authkey` must be
   **reusable**, **non-ephemeral**, and ideally **tagged** (`tag:vps` via
   `TS_UP_EXTRA_ARGS=--advertise-tags=tag:vps`).
3. Recover the Anytype bot identity from `anytype_account_key`, then
   `anytype auth apikey create` and write `anytype_api_key`.
4. Decide the model path *before* first boot (see above); the subscription
   path needs an interactive login or a minted token on a headless box.
5. Keep `ports:` on `127.0.0.1`. Keep `TZ`.
6. Build, boot, and check the four log lines above.
7. Verify: `pytest -q`, then `ANYTYPE_E2E=1 pytest tests/e2e -q` against the
   new sidecar. The live suite find-or-creates its own `GC-E2E` space and
   resets it around each run.

What is *not* on the VPS: the Anytype **desktop** app. `host.docker.internal`
and port `31009` exist for the desktop's local API, which is a human
convenience on a workstation; the headless sidecar at `anytype:31012` is the
only backend a server needs.

## Troubleshooting

**Boot stops after `[tailscale] daemon already running`, no server.**
tailscaled cannot reach its control plane and `tailscale up` is wedged. Check
`tailscale status` for `fetch control key: ... network is unreachable`, then
confirm the firewall logged **both** exemption lines (`iptables:` *and*
`ip6tables: uid 0 exempt`) — the control plane commonly resolves to IPv6, and
the IPv6 policy is deny-all. Timeouts now bound this, so it degrades to a
warning instead of hanging the chain.

**`OSError: [Errno 98] Address already in use` on 8765.** Two boots raced.
Harmless now: the loser reports `already running` and exits. If a server is
running but `gc-serve status` disagrees, it is adopted from the process table
on the next call.

**`[firewall] WARNING: could not resolve <domain>`.** That domain resolved to
no A record when the allowlist was built, so it is absent until the next boot.
`statsig.*` is telemetry; `app.claude.com` matters only for Remote Control.
Re-run `sudo init-firewall.sh` if a Claude path misbehaves.

**`tailscale up` reports the node `offline`.** The status line is read
immediately after `up`, sometimes before the first netmap lands. Re-check with
`tailscale status`: a `-` in that column means online.

**Model calls fail on a fresh host.** The `claude-config` volume did not come
with you. See [Model access](#model-access).
