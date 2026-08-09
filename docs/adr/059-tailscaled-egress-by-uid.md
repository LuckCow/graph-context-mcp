# ADR 059: tailscaled egress by UID, and a boot chain that cannot hang

Date: 2026-08-09
Status: accepted

## Context

The container runs behind a default-deny egress firewall
(`init-firewall.sh`): an ipset of resolved destinations, everything else
REJECTed. That policy is the reason it is safe to run an autonomous
agent with tool access in here at all.

Tailscale then arrived (remote access to the inspection server, and the
way into the VPS this stack is moving to), and it does not fit the
allowlist. tailscaled talks to `controlplane.tailscale.com` **and** to
whichever DERP relay the netmap hands it — a set that is large, changes,
and is not knowable at firewall-init time. The firewall's own header
already promised the answer ("tailscaled, by UID rather than by
destination") but no such rule was ever written, and
`controlplane.tailscale.com` was never added to the allowlist either.

The result was silent and total: tailscaled could reach its control
plane on neither stack — IPv4 hit the REJECT, IPv6 hit a deny-all policy
(the control plane resolves to IPv6 here, so the observed error was
`network is unreachable`). `tailscale up` does not fail on that. It
retries, forever. And because boot is

```
init-firewall && start-tailscale && gc-serve boot
```

a wedged `up` meant the **orchestrator never started at all** — observed
live as two `tailscale up` processes stuck for 17 minutes and no server,
twice, across two rebuilds.

## Decision

### Egress for tailscaled is granted by UID

```
iptables  -A OUTPUT -m owner --uid-owner 0 -j ACCEPT
ip6tables -A OUTPUT -m owner --uid-owner 0 -j ACCEPT
```

appended immediately before each chain's REJECT. Both stacks, because
the control plane resolves to IPv6 and a v4-only exemption is not an
exemption. Expressed once as `exempt_tailscaled <cmd>` so the rule and
its reasoning have one home; `-m owner` needs `xt_owner`, and a kernel
without it warns and continues — a container that is merely locked down,
never a failed boot.

**The cost is real and accepted: any process running as root bypasses
the egress policy.** The alternative — a destination allowlist for DERP
— trades a bounded, stated exemption for an unbounded maintenance
burden that fails exactly when the tailnet is the only way in.

What keeps the exemption honest is that nothing in this stack is
supposed to be root. `gc-serve` refuses to start the orchestrator as
root and says why; the workload user's sudo is scoped to these two
scripts; the agent, its tools, and the sandbox all run as `dev`.

### The firewall verifies itself as the workload user

The policy is written for the workload user, and now root is genuinely
exempt from it — so verifying as root would test the wrong subject and
report `lockdown failed` at precisely the moment the exemption started
working (`init-firewall` exits 1 on that check, which would have taken
down the whole boot chain). The two probes run through `as_workload`.
The script's `WORKLOAD_USER` had been documented as doing this all
along; it is now true.

### No link in the boot chain may hang

Failure was already non-fatal by design in `start-tailscale.sh` ("every
failure path warns and exits 0"). That promise covered non-zero exits
only. Every network-touching tailscale call is now wrapped in a timeout
(`TS_NET_TIMEOUT`, default 45s), so an unreachable control plane
degrades to a warning within a bounded window instead of blocking the
orchestrator behind an optional convenience.

The firewall stays a **hard** gate (`&&`): no orchestrator without
egress lockdown is the one ordering constraint worth keeping.

### Starting the orchestrator is idempotent

`gc-serve boot` runs from both the compose command and devcontainer's
`postStartCommand`, which raced on every rebuild: both saw no pid file,
both spawned, the loser died on the inspection server's port — and its
cleanup deleted the WINNER's pid file, leaving a healthy server that
`status` reported as down and `stop` could not reach.

Three rules, each in one place:

* Starts serialize on an flock, and the spawned server does **not**
  inherit the lock fd (it would hold it for its whole life, and the next
  `start` would block on a running server rather than report it).
* The pid file is a cache, not the truth. `running_pid` falls back to
  the process table, so `status`/`stop`/`start` agree with reality and
  an orphaned server is adoptable. The match is anchored and confirmed
  against `/proc/<pid>/cmdline` — an unanchored `pgrep -f` also matches
  any shell that merely mentions the module, and `stop` would kill that
  bystander (it killed a test harness before the anchor went in).
* The pid file is written **after** the survival check, never before, so
  a doomed start cannot clobber a running server's entry.

## Consequences

* Remote access works: `tailscale serve` publishes `:8765` at
  `https://<node>.<tailnet>.ts.net/` (MagicDNS + HTTPS certificates
  must be enabled in the tailnet admin console; without them `serve`
  fails with `zero serverNoiseKey`).
* Root egress is a standing exemption. Anything added to this stack that
  wants root must be weighed against the allowlist it would bypass.
* A tailnet that cannot come up costs ~45s of boot and a warning. The
  orchestrator starts regardless.

### Accepted limitations

* The exemption is by UID 0, not by binary. A tighter form would run
  tailscaled as its own uid and exempt that — worth doing if anything
  else ever legitimately needs root here.
* `xt_owner`'s absence is only discoverable at boot, from the warning.
* The idempotence work makes duplicate `boot` callers harmless rather
  than removing one: `postStartCommand` also fires on reconnect, which
  is a genuine second chance to start a server that died while the
  container stayed up.
