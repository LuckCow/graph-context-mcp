# ADR 060: Deployment-scoped space bindings

Date: 2026-08-15
Status: accepted (amends ADR 017/019)

## Context

Production moved to a VPS, and the dev box stayed. The VPS deployment
is a `git clone` of this repository running the same compose file, and
— because the bot's Anytype account was *migrated* rather than
recreated — the same bot identity. That account is a member of every
production space, on both hosts.

So the two deployments were separated by nothing but a habit: the dev
orchestrator happened to be stopped. A `docker compose up` here, or a
VS Code "Reopen in Container", started a second bot that answered every
production chat a second time, from a second route lock, writing a
second set of provenance nodes. `spaces.toml` — the file that decides
which spaces a runtime serves — was committed, so both hosts read the
same one, by construction.

The problem is not that the dev config was wrong. It is that *which
spaces this deployment serves* was being tracked as source code, when
it is a property of the host.

## Decision

### Deployment-scoped config leaves the index

`spaces.toml` and `channels.toml` are git-ignored, one copy per host,
alongside the secrets they sit next to in `.gitignore`. They are not
secret — a space id is just an identifier — but they share the
property that matters: they describe *this deployment*, and they must
never travel between hosts.

Builds stay **identical**. Same image, same compose file, same branch,
same environment — the dev box and the VPS differ in exactly one file,
and it is one that git cannot carry between them. Rejected
alternatives:

* **A `docker-compose.dev.yml` override layered by `devcontainer.json`.**
  Works, but makes dev and prod *builds* differ, and leaves a bare
  `docker compose up` on the dev box still pointed at production.
* **A per-host marker file that refuses to boot on a mismatch.** This
  was designed and dropped: it guards an error that no longer exists.
  Once the binding file cannot be shared, a dev box cannot serve a
  production space because its config does not name one. A guard is a
  check; this is a structural impossibility, and the structural fix is
  the one to keep.

### An absent file is minted, then refused

Untracking a file that every deployment needs would make a fresh clone
fail with `cannot read GC_SPACES_FILE` — technically loud, practically
a dead end. So `load_space_bindings` splits `FileNotFoundError` out of
its `OSError` arm and copies the packaged `spaces.example.toml` (beside
the loader, the way `mode_config.py` resolves its mode seeds) to the
requested path, then raises: *"wrote a starter template there —
uncomment its table, set the space id this deployment should serve,
then restart."*

Minting does not make startup succeed. A chat bot bound to nothing is a
misconfiguration, not a quiet no-op — the same judgment `bootstrap.py`
already makes for an unset `GC_SPACES_FILE`. What minting changes is
the failure's *shape*: an unfilled form with its own instructions,
instead of a missing one. The template deliberately binds no space, so
the second boot fails on "at least one table" rather than serving a
placeholder id. `x` mode never clobbers: two boots racing on one
directory must not have the loser overwrite the winner's file.

`channels.toml` is treated as the sibling it is, with one difference:
Discord is opt-in, so a **missing** file means "Discord parked" —
exactly what a file with zero tables already meant since the WP14
cutover — and nothing is minted, because there is nothing to fill in.
`channels_declared` returns False for `FileNotFoundError` and keeps
returning True for unreadable or malformed files, which are broken
configs and still belong to `load_channel_bindings`' loud error.

### Deploys and logs are one command each

`scripts/deploy.sh` runs on the deployment host, outside the container
(it needs git and docker; the workload container has neither), and
`scripts/gc-prod` is the SSH front end run from the workstation shell.
Not from inside the devcontainer: tailscaled runs there with
`--tun=userspace-networking` and no SOCKS proxy (ADR 059), so the
container has no outbound tailnet path at all.

The deploy derives its restart depth from the diff rather than asking:
source is a bind mount, so `gc-serve restart` picks it up, and only a
touched `pyproject.toml` or `.devcontainer/**` needs the multi-GB image
rebuild. It refuses on a dirty tree, fast-forwards only, polls
`gc-serve status` until the orchestrator answers, and dumps the tail of
`serve.log` when it does not. `--to <sha>` is the rollback.

The deploy cannot repoint a host at another deployment's spaces, since
the files it would have to overwrite are no longer in the tree it
pulls. That is the property this ADR is really buying.

## Consequences

* The separation now rests entirely on the two files being **disjoint**:
  a space id belongs in exactly one deployment's copy. That invariant
  is stated in both templates, where the person editing them is.
* `spaces.toml` and `channels.toml` join the migration checklist in
  `docs/DEPLOYMENT.md` — a `git clone` on a new host gets neither, and
  the mint is what fills the gap.
* **The commit that untracks them deletes them on the next `git pull`.**
  A one-time hazard, called out in DEPLOYMENT.md: back the file up on
  the server before deploying this change and restore it after, or take
  the downtime and refill the minted template.
* Both nodes still join the tailnet under the same default
  `TS_HOSTNAME`, so the second registers as `graph-context-mcp-1` and
  "which URL is production" stays ambiguous. Identical builds is what
  keeps that unfixed here; `TS_HOSTNAME` is already env-overridable if
  a per-host env file ever earns its place.
