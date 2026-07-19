# ADR 040: Sandboxed script action + the `automation` tool

Date: 2026-07-19
Status: accepted (supersedes ADR 039's no-scripts / no-LLM-tool scope)

## Context

ADR 039 shipped the rule engine with three built-in actions and two
deliberate scope cuts: no user scripts (the original design sketch's
"Python snippet in a text relation" is code execution for anyone with
vault write access) and no LLM tool (authoring stayed in the Anytype
UI). Both cuts came due immediately: the built-ins cannot express
cross-object logic ("keep an open-task count on the project"), and the
assistant had no discoverable way to turn "whenever I finish a task,
stamp it" into a rule.

## Decision

**A fourth action, `run script`, executed out-of-process.** The rule's
Python source lives in the rule node's BODY as a fenced ```` ```python ````
block. `rules.extract_script` takes the FIRST fence that is
python-tagged **or untagged** — the live server's markdown round trip
drops fence language tags (quirk A13, live-confirmed during this WP:
a ```` ```python ```` block written at create reads back bare), so
tag-only matching would never fire against a real space. Foreign-tagged
fences (```` ```bash ````) and prose bodies never execute, and this path
only reads rules whose author explicitly chose the script action. Quirk
A9's first-line-heading flattening cannot touch a fence line. The engine fetches bodies via
`fetch_body`, cached per rule by `Node.modified_at` — a body edit bumps
the stamp and swaps the script in; the memory backend's empty stamp
degrades to refetch-per-tick (its fetch is free).

**Isolation is a subprocess, for robustness before security.** The tick
holds the space's turn lock and Python threads cannot be killed, so an
in-process `exec` hitting `while True: pass` would hang the whole bot;
a child process is the only clean wall-clock kill. Mechanics
(`infrastructure/sandbox/`, stdlib only — the egress firewall blocks
sandbox libraries):

- The parent (`SubprocessScriptRunner`, behind the `ScriptRunner` port
  in `ports/script_runner.py`) spawns `sys.executable -I -S
  bootstrap.py` in its own session with a scrubbed env (`PATH` +
  `PYTHONIOENCODING` only — no `ANYTYPE_API_KEY`, no `ANTHROPIC_*`),
  pipes one JSON payload in, reads capped stdout/stderr, and enforces
  the WALL clock (`GC_RULE_SCRIPT_TIMEOUT_SECONDS`, default 5s) with
  `killpg(SIGKILL)`. After a kill it **drains the pipes to EOF before
  `process.wait()`** — a pipe still holding data never disconnects its
  transport, and `wait()` would hang forever on an already-dead child.
- The child's first act is lowering its OWN rlimits — hard limits
  cannot be raised back, so the script cannot undo them: CPU 5s,
  address space 256 MiB, file size 1 MiB, 16 fds, NPROC 1 (blocks
  fork; per-UID semantics, so belt not boundary). Script `print()` goes
  to a capped in-child sink; `os.write(1, …)` floods hit the parent's
  output cap instead.
- **Threat model: the author is the space owner.** This contains
  *accidents* — runaway loops, memory bombs, output floods — not a
  hostile author (`open()`/`socket` remain importable; seccomp/bwrap
  would need dependencies the firewall blocks). The env scrub keeps
  secrets out of reach either way.

**Scripts read a snapshot and queue effects; the engine owns all
writes.** The payload is the whole non-infra graph (id, type, name,
summary, fields per node + the edges among them; hard cap 2000 nodes,
loud beyond) plus the trigger context (`trigger`, `before`, `after`,
`now` — engine-injected, so scripts never read a clock). The in-child
API is deliberately tiny: `objects(type=)`, `find(name, type=)`,
`field(obj, prop)`, `neighbors(obj, edge_type=)`, `set(obj, prop,
value)` (str/bool/int/float, coerced to the wire string), `log(msg)`.
`set()` queues — nothing touches the store from the child. The engine
validates EVERY effect before applying ANY (target exists and is
non-infra, property resolves against the target's own type with the
catalog hints, checkbox/number values parse, ≤ 20 writes per fire),
then applies through the same `_write_field` choke point the built-ins
use. Consequences inherited wholesale from ADR 039: the end-of-tick
rebaseline absorbs script writes (**scripts can never trigger rules,
including themselves**), transitions are consumed at-most-once, and
failures land in `gc_rule_last_error` (traceback tail included) with
the same change-only writes and self-healing.

**The `automation` tool is the LLM surface** (the ADR 027 `schedule`
precedent: a dedicated tool whose docstring is the prompt beats
undiscoverable generic object creation). Actions: `create` / `update`
(validated at the tool boundary — config parse AND bind-time property
resolution fail with hints *now*, not as an Error on the next tick;
script rules store the fence into the body), `list` (status, last
error, last fired, config one-liner), `pause` / `resume` (status
flips; no hard delete — the port has no archive, matching the
scheduler's cancel-is-a-status-flip), and `test` — a **dry run**:
simulate one fire against a real object (chosen or first-of-type),
synthesize the transition the condition describes, run the script in
the real sandbox, validate its effects with the production code path,
and report `would set …` lines + logs while applying nothing. `test`
works on a stored rule or on a draft passed inline, so the model can
iterate before creating. Bound in every mode beside `schedule`
(automation config is space bookkeeping, not story authorship; the
minted node is infra), registered on the MCP server too (creation
works there; only the bot's `_watch_rules` loop fires).

## Consequences

- Cross-object automation works from either surface: a human writes a
  fenced script in the rule page; the assistant authors one via
  `automation action=create` after an `action=test` preview.
- Per-fire cost: one Python startup (~50 ms) + snapshot serialization,
  O(space), only on transitions — invisible on a 5s tick. Worst case a
  tick stalls chat for (fires × timeout) under the turn lock; a
  per-tick script budget is noted future work.
- `set property to now`-style determinism extends to scripts via the
  injected `now`; scripts that read the real clock get the child's UTC
  container time and deserve what they get (documented).
- Script `log()` lines reach the bot log only; a last-run-log property
  is a cheap later addition.
- The `automation` tool is the TENTH tool; profile goldens moved.

## Alternatives considered

- **In-process restricted `exec`** — simpler, but not a boundary
  (dunder escapes) and, decisively, a hung script hangs the bot with
  no kill path.
- **RestrictedPython / seccomp / bubblewrap** — all need packages the
  egress firewall blocks; the subprocess gets containment for free
  from the kernel primitives already present.
- **A `gc_rule_script` text property instead of the body** — miserable
  multi-line editing in the UI and a second source of truth; the body
  is where humans already write, with code-block styling.
- **Scripts as conditions** — unnecessary: fire on `changed` and let
  the script decide; a no-op script fire costs one subprocess.
- **Direct store access from the child** (hand it the API key) — would
  turn every script bug into a data-integrity incident and break the
  single-writer/self-write-suppression invariants the engine builds on.
