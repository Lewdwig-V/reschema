# ReSchema Architecture

- [Overview](#overview)
- [Terms](#terms)
- [Component boundaries & data flow](#component-boundaries--data-flow)
- [Life of a submission](#life-of-a-submission)
- [The rejection payload](#the-rejection-payload)
- [Components](#components)
  - [mcp/server.py — 5 tools, dispatch only](#mcpserverpy--5-tools-dispatch-only)
  - [engine.py — task state, gates, ledger](#enginepy--task-state-gates-ledger)
  - [exec/recorder.py — ground-truth records](#execrecorderpy--ground-truth-records)
  - [exec/canonical.py — canonicalize](#execcanonicalpy--canonicalize)
  - [validate/program.py — program gate](#validateprogrampy--program-gate)
  - [driver/ — marshaling, original calls, container boundary](#driver--marshaling-original-calls-container-boundary)
  - [corpus/generate.py — 48-slot seed matrix](#corpusgeneratepy--48-slot-seed-matrix)
  - [disasm/ — task_open facts](#disasm--task_open-facts)
  - [memory.py — deduction cache](#memorypy--deduction-cache)
  - [tools/dogfood/ — 2C live-agent transfer driver](#toolsdogfood--2c-live-agent-transfer-driver)
- [Key architectural decisions](#key-architectural-decisions)
- [Decision records](#decision-records)
- [Verification & maintenance](#verification--maintenance)

## Overview

ReSchema is an MCP tool server (`src/reschema/`) that forces an LLM agent to
hold executable C world-models of x86-64 static ELFs: ground truth is recorded
by emulating binaries (qiling, in-process), and an agent's C candidate is
validated against it at two levels — **program mode** (whole-binary canonical
trace replay + 8 hidden fresh inputs per submission) and **function mode**
(differential fuzzing over `{ret, mem}` against the original function).
Rejections are always structured (`{accepted: false, ...}`), never tracebacks.

The harness itself is adversarial-by-design: agent-authored C is compiled and
executed. All compilation and all native execution happen **inside one-shot
throwaway rootless podman containers** spawned from a pinned toolchain image
(`Containerfile`, debian trixie + gcc + clang), and qiling emulation runs
against **fresh empty rootfs** — host `gcc` is never invoked for agent or
corpus code, and guest file operations can never reach the host filesystem.

State lives on disk under `.reschema/` (task dirs, family memory) with atomic
temp-file+rename writes but **no coordination: single-process access is
assumed** throughout (the engine module docstring carries the same warning).
The state root is `RESCHEMA_HOME`-overridable (default: the repo for src-layout
checkouts); the test suite uses it for per-worker isolation under pytest-xdist.

## Terms

Standard reverse-engineering vocabulary is assumed. These are the project's
own words, used everywhere and defined nowhere else:

- **case** — one input run: an `(argv, stdin)` pair plus the trace it
  produced, persisted per task as `trace_<label>.json`. The unit experiments
  append and gates replay.
- **record** — executing a binary under qiling and capturing its trace
  (`exec/recorder.record`).
- **trace** — the JSON dict for one run: `argv`, `stdin_hex`,
  `stdout`/`stderr` (hex), `exit_code`, `files_written`, `events`.
- **event** — one observed syscall in a trace: `{sc, phase, args, result?}`
  where `phase` is `enter` or `exit` (the recorder hooks both).
- **probe** — one `experiment` call. Counted in the ledger's `probes` key and
  priced in the efficiency metric. A function-mode probe is the cheap scout
  round: exactly one emulated call against the original, one probe
  accounted, no trace persisted.
- **slot** — one cell of the corpus build matrix, e.g. `gcc-O2-sym`
  (compiler × optimization × symbols kept vs stripped). A `task_id` is
  `{seed}::{slot}`.
- **family** — all slots derived from one seed. The family is what the
  deduction cache keys on (`{seed, function}`): a fact learned on
  `gcc-O0-sym` travels to `gcc-O2-sym` of the same seed and nowhere else.
- **TU** (translation unit) — one `.c` file compiled standalone. Composition
  builds one TU per accepted function, then links them.
- **write-family shape** — the syscall skeleton the program gate compares:
  `(sc, fd, byte count)` for `write`/`writev`/`exit_group`, with address
  arguments wildcarded. Which bytes are written is already covered by the
  stdout/files channels; the shape pins *how* they are written.
- **canonicalize** — rewrite legitimately-divergent tokens before comparing
  traces (`ADDR_<n>`, `FD_<n>`, `PATH_<n>`, argv[0] → basename). Rules are
  versioned (`2.1`); a rules change forces a corpus re-record.
- **starvation**, **hidden-starvation** — a submission is rejected when fewer
  than `HIDDEN_N=8` distinct usable hidden inputs can be drawn for it: a loud
  failure instead of a vacuous pass.
- **event-split** — a model emitting output in different syscall-sized chunks
  than the original. Chunking is observable, so it diverges and rejects.
- **dep_slice** — the fd-linked backward syscall chain attached to
  event-divergence and files-mismatch rejects: the write-intent open pair
  that produced the diverging fd, plus every write on it up to the
  divergence, capped at 6 raw events.
- **poison-filled** — fuzz out-buffers pre-loaded with random bytes, so a
  no-op model leaves detectable garbage instead of matching a
  memory-preserving original.
- **case budget** — the 5s wall-clock limit per fuzz case in the native
  worker (one fork per case); the container timeout is sized from it.
- **ledger** — a task's persistent JSON state: accepted entries,
  submission/rejection counters, audit seeds, and the capped `recent`
  journal. Alongside the family deduction cache, the only cross-run memory.
- **verified_fact**, **unverified_hypothesis** — the two tiers of the family
  deduction cache: harness-written ground truth on gate acceptance vs
  agent-declared notes promoted only by the acceptance of their own
  submission.
- **dogfood** — the harness exercised end-to-end through its own MCP
  interface (`tests/test_dogfood.py`).

## Component boundaries & data flow

```
agent ──stdio MCP──> mcp/server (5 tools, dispatch-only)
                        │
                        ├─ engine.TaskStore          ledger/traces on disk
                        │    ├─ exec.recorder.record()      qiling + empty rootfs
                        │    ├─ exec.canonical.canonicalize rules v2 (v2.1)
                        │    ├─ validate.program           replay gate (program)
                        │    └─ validate.function          fuzz gate (function)
                        │
                        ├─ corpus.generate           48-slot seed matrix build
                        ├─ driver.podrun.run_worker  one-shot podman containers
                        │    └─ driver.native_worker validate/compile/compile-link modes
                        └─ disasm.{slice, analyze}   capstone facts for task_open

tools/dogfood/ (benchmark tooling, NOT part of the package):
   campaign (TOML) ──> driver.run_campaign ──> slot.run_slot ──> AgentRunner
                        (pool, sequential primed chains, honest resume)
                        └─ measure.render_report   φ + deltas + abort evidence
```

## Life of a submission

Control flow across the tour sections below, as it actually happens.

### Program mode (`submit_model` without `function=`)

1. `mcp/server.submit_model` dispatches to `engine.submit_program`. The ledger
   `submissions` counter increments first — every outcome is accounted,
   including compile failures.
2. `validate/program.compile_model` hands the C source to
   `driver/podrun.run_worker` (`compile` job): a one-shot container compiles
   it `gcc -O1 -static -fno-pie -no-pie -g0`, binary at `model` in the task
   dir. Missing podman/image fails loudly as a structured `compile` reject,
   never a fallback to the host.
3. **Recorded stage.** `replay_against(model, recorded_traces)`: per stored
   case, the model is run under qiling against a fresh empty rootfs
   (`exec/recorder.record`), the run is canonicalized, and the gate compares
   — in order — stdout/stderr/exit_code (byte-exact), `files_written`
   (paths+bytes exact), and the write-family event shape. The *first*
   divergence wins and comes back as
   `{accepted: false, reason, stage: "recorded", divergence}`.
4. **Hidden stage.** 8 fresh inputs drawn with per-submission entropy
   (`secrets.token_hex(16)`), deduplicated against recorded cases, each
   ground-truth *double-recorded*. Same replay comparison, `stage: "hidden"`.
   Too few distinct usable inputs → `hidden-starvation` reject.
5. **Accept.** The ledger gets the idempotent `"program"` marker,
   `audit.program.hidden_seed`, and a journal entry; `memory.append_fact`
   writes the accepted source as a `verified_fact` (`fn: "__main__"`) other
   slots of the family will see at their `task_open`.
6. **Reject.** Counters + journal update; any agent `notes` land as
   unpromoted `unverified_hypothesis` entries. One submission teaches the
   family nothing verified.

### Function mode (`submit_model(function=f)`)

1. Dispatch to `engine.submit_function`. The agent-declared param spec is
   parsed first; a malformed spec JSON is a counted reject here, at top level:
   `{accepted: false, reason: "spec", detail}`.
2. Spec floors live inside the validator: more than 6 register args rejects
   (stage `arity`); `ret: "void"` without at least one memory-channel param
   (`buffer_i32`/`cstring`) rejects (stage `spec`) — a scalar-only void would
   compare `{} == {}` and a no-op would pass. Floor rejects nest inside the
   divergence slot: `{accepted: false, divergence: {stage, detail}}`.
3. `validate/function.validate_function` draws the fuzz seed (fresh entropy
   unless tests pin it) and builds `n_fuzz` cases (`N_FUZZ=64`, floored at
   the MCP boundary — the agent cannot tune its own judge down; capped at
   4×). Poison-filled out buffers included.
4. Ground truth per case: `driver/calling.batch_call_original` runs the round's
   cases through the original function under ONE qiling VM, restoring a pristine
   post-init snapshot (registers + memory) before every case — pinned
   byte-identical by the batch-vs-fresh equivalence test
   to fresh VMs (tests/test_driver.py equivalence guard). The snapshot contract
   is pinned for syscall-free kernels only: it spans registers+memory, never
   rootfs contents, OS/fd objects, or post-save mapped regions. The structural
   guard (not a caveat) is a syscall scan at batch entry: the function's
   symtab-bounded .text slice is checked for `syscall`/`sysenter` opcodes, and
   any hit — or an unboundable slice (stripped/zero-size symbol) — reroutes
   the round to per-case fresh VMs. Each trace records its route in
   `batch_mode` (`"batched-snapshot"` vs `"fresh-vm-fallback"`), and the
   syscalling-witness test pins fallback routing + result identity.
   Cases where the *original* faults are skipped — a crash is not a behavior
   spec.
5. The model is compiled and executed in ONE worker round trip (`validate`
   job): `gcc -O1 -shared -fPIC` in the container, RTLD_NOW load + symbol
   presence check, then fork-per-case ctypes calls under the 5s case budget.
   A model segfault/hang surfaces as a per-case `crash` result (first crash
   stops the round), never a wedged harness. Comparison is `{ret, mem}` only
   — level-B never compares syscalls (scope guardrail).

Note the deliberate substrate asymmetry: the *original* always executes under
emulation (fidelity), while the *model* `.so` always executes native inside
the container (containment for untrusted code). They never share a substrate.
6. Per case, `{ret, mem}` is compared (`ret` skipped for void specs — eax is
   register residue; mem is their channel). First mismatch rejects with
   `{input, field, expected, actual, seed}`.
7. Accept: newest source wins in the ledger (`{func: c_source}`), audit keeps
   `{seed, n_fuzz}`, and a `verified_fact` (params, source, topology digest)
   is appended to the family cache.

### Composition (`engine.compose`, deliberately not an MCP tool)

Each accepted source is compiled as its own TU and linked with a generated
`main` stub via the worker's `compile-link` mode. A duplicate
externally-visible symbol across TUs fails at the linker and maps to a
structured "declare helpers `static`" reject — that is the linkage contract
the `abi_template` preaches.

## The rejection payload

Every agent-facing verdict is a typed dict. There are three shapes:

**Accepts:**

- program: `{accepted: true, recorded_cases, hidden_cases, hidden_seed}`.
- function: `{accepted: true, compared, skipped, seed}`.

**Gate rejects:** `{accepted: false, ...}`

- Behavior divergences (program gate): reasons `io-mismatch`,
  `files-mismatch`, `event-divergence`, `event-length` — carry
  `stage: recorded|hidden` and a `divergence` payload describing the FIRST
  divergence (decoded previews for io, hex elsewhere, plus a `dep_slice`
  fd-chain for event/files divergences).
- Behavior divergences (function gate): `{accepted: false, divergence:
  {input, field, expected, actual, seed}}` with `field` in `ret|mem|crash`.
- Mechanical rejects carry a human-readable `detail`, but its nesting depends
  on the path: program-gate mechanical rejects (`compile`,
  `hidden-starvation`) return top-level `{accepted: false, reason, detail}`;
  function-gate floor verdicts from the validator (arity, void-without-
  memory-channel, skip-starvation, infra) nest it as `{accepted: false,
  divergence: {stage, detail}}` (skip-starvation also carries the fuzz
  `seed`); a malformed spec JSON fails before validation as top-level
  `{accepted: false, reason: "spec", detail}`. Clients should read the reason
  from `divergence.stage` first and fall back to top-level `reason`.

**Tool-boundary errors (not verdicts):** `{error: not_found|spec|internal,
detail}` — unknown task/function, malformed params crossing the wire, or
harness faults (corrupt ledger, missing image). The `internal` detail names
the exception class; it is a structured report, not a traceback page.

## Components

### mcp/server.py — 5 tools, dispatch only

`corpus_build(seed_ids?, matrix?)`, `task_open`, `experiment`, `submit_model`,
`status`. No business logic; errors are mapped to `{error: not_found|spec|internal}`
dicts. A contract test pins the term coverage of every tool description.
`experiment` on a function task deliberately skips case persistence: one
emulated call against the original, one ledger probe, no `trace_*.json` —
the cheap scout round before an agent commits recorded cases.

### engine.py — task state, gates, ledger

`TaskStore` persists per-task state under `.reschema/tasks/<task_id>/`:
canonical case traces (`trace_<label>.json`) and `ledger.json`
(accepted entries, submissions/rejections counters, `audit` seeds, and a
capped `recent` submission journal).

**Concurrency: single-process (or out-of-band serialized) access is
assumed.** Ledger writes are atomic per file (temp + `os.replace`) but
uncoordinated across processes; run two servers against one `.reschema/`
tree at your own risk. This constraint exists because the alternative —
file locking across MCP stdio servers — buys correctness nobody is
exercising yet.

- **submit_program** compiles the candidate, replays recorded traces, then
  replays 8 freshly drawn hidden inputs (`HIDDEN_N`, fresh entropy per
  submission, double-recorded ground truth), and returns rich accepts
  (`recorded_cases`, `hidden_cases`, `hidden_seed`) or structured rejects with
  an ordered reason (`io-mismatch`, `files-mismatch`, `event-divergence`,
  `event-length`, `hidden-starvation`, `compile`). All outcomes update the
  ledger (counters, accept marker idempotent, journal, `audit.program`).
- **submit_function** compiles and differential-fuzzes via the worker; the
  tool boundary floors `n_fuzz` at `N_FUZZ=64`; a void spec with no
  memory-channel param is rejected up front; accepts carry
  `compared/skipped/seed` and write `audit[func] = {seed, n_fuzz}`.
- **compose** links awaited sources per-TU through the worker's
  `compile-link` mode; duplicate externally-visible symbols map to a
  structured "declare helpers static" reject. Not exposed as an MCP tool.
- **status_snapshot** computes readiness (recorded cases vs the hidden-gate
  minimum), coverage (accepted/total manifest functions), the `recent`
  journal, and the cost-shaped `efficiency` metric
  (`E = accepted * exp(-(0.15*(probes-1) + 0.4*(submissions-1)))`, probe and
  submission counts only — wall-clock noise is deliberately excluded) from
  ledger/manifest only. The 0.15/0.40 constants are **provisional** chosen
  values, not measured ones — see `docs/benchmark-protocol.md` for the state
  of their justification. Probe accounting lives on both experiment paths
  (program-path records and function-path cases), stored as a lazy
  `probes` ledger key.
- **_abi_template** renders a compile-ready function-mode header from the
  driver's own constants (`KINDS` + `Param` defaults, so the docs can't drift
  from the schema): the `RESCHEMA_FN` macro (`__attribute__((sysv_abi,
  noinline))` — SysV lowering is an attribute, not a flag), the param-spec
  JSON sketch, the sketched signature, and the compose rule (single-function
  helpers must be `static`).
- **open_function_task** assembles the function-mode `task_open` payload:
  disasm slice, capstone facts + labeled signature guess, known callees,
  `abi_template`, family memory plus its presentation tier
  (`memory_provenance`, `ready_to_submit` — see memory.py) — and
  **repair_directive** when the task
  ledger's journal shows rejection history (research slot 2B-5): two-pass
  coaching answering rejections FIRST with abstract bit-logic repair
  (fixed-width ints, exact byte behavior, no idiomatic attempts), idiomatic
  annotation only after acceptance. It is provenance-tagged as coaching
  derived from the rejection history, never a verified fact.
- **_topology_digest** fingerprints an accepted function by name-independent
  call shape — callee count, per-child chain depths, own call depth, arity
  hint (names deliberately excluded: a symbol-less slot renames them all,
  the shape survives) — recorded on each function-mode `verified_fact`
  (docs/roadmap.md research slot). Write-side only today; matching stripped
  slots' `fn_0x…` back to family names by shape is the intended consumer.

### exec/recorder.py — ground-truth records

Each `record(binary, argv, stdin)` copies the binary into a fresh empty
rootfs, hooks `read/write/writev/open/openat/creat/close/brk/mmap/exit_group`
ENTER+EXIT, and returns
`{argv, stdin_hex, stdin_sha256, stdout(hex), stderr(hex), exit_code,
files_written, events}`. `files_written` is a post-run scrape of the rootfs
(real fs semantics for truncate/rename/append); crashes/timeouts produce
`exit_code: -1` plus a trailing fault event. Ground truth is always
double-recorded (`_record_stable`) before storage.

### exec/canonical.py — canonicalize (`CANONICALIZER_VERSION = "2.1"`)

Address-shaped tokens (`0x` + ≥6 hex digits) → `ADDR_<n>` by first sighting;
argv[0] → basename; fds returned by write-intent opens → `FD_<n>` by first
sighting (fds 0/1/2 keep ABI literals, read-only opens never enter the table,
so a model's extra read-open cannot shift numbering), applied to fd-carrier
events in both enter and exit phases; absolute host paths in string args →
`PATH_<n>`. All ordinal tables are built **per trace** — `ADDR_0` means
"the first address-shaped token in THIS case", so two cases' `FD_0`/`ADDR_9`
are related only within-case.

Worked example. Raw recorded events (abbreviated):

```json
{"phase": "exit",  "sc": "openat", "args": ["0xffffff9c", "/data/report.txt", "0x241", "0x1b6"], "result": "0x3"}
{"phase": "enter", "sc": "write",  "args": ["0x3", "0x4b72a0", "0x5"]}
{"phase": "enter", "sc": "write",  "args": ["0x1", "0x4b7a10", "0xe"]}
{"phase": "enter", "sc": "exit_group", "args": ["0x0"]}
```

After `canonicalize`:

```json
{"phase": "exit",  "sc": "openat", "args": ["0xffffff9c", "PATH_0", "0x241", "0x1b6"], "result": "0x3"}
{"phase": "enter", "sc": "write",  "args": ["FD_0", "ADDR_0", "0x5"]}
{"phase": "enter", "sc": "write",  "args": ["0x1", "ADDR_1", "0xe"]}
{"phase": "enter", "sc": "exit_group", "args": ["0x0"]}
```

Note what does *not* change: the open's exit `result` keeps its literal
`0x3` (the fd table records it, only fd-carrier *args* are rewritten), fd `0x1`
stays an ABI literal, and small literals like `0xe` (14 bytes) are untouched.

The corpus manifest directory carries a `canonicalizer_version`
sidecar; `engine.load_manifest` hard-refuses a version mismatch with a
rebuild hint.

### validate/program.py — program gate

`compile_model` compiles through the worker's `compile` mode.
`replay_against(model, traces)` compares per case: `stdout`/`stderr`/
`exit_code` byte-exact (with latin-1 decoded previews in divergences),
`files_written` map byte-exact, and the observable event channel `OBS =
("write", "writev", "exit_group")` with `ADDR_*` wildcards on address ordinals
(event fd/count shape is significant; chunking is observable). Reason order:
io-mismatch → files-mismatch → event-divergence/event-length; divergence on
the first mismatch only.
`hidden_input_stream` yields text charset draws by mode and `stdin-bytes`
draws (random bytes with a guaranteed NUL and ≥0x80 byte) for binary-safe
seeds; `STDIN_DRIVEN`/`STDIN_BYTES_DRIVEN` select modes per seed.

Event-divergence and files-mismatch payloads carry a `dep_slice`
([Terms](#terms)): the validator searches backward from the focus event for
the LAST write-intent open pair that produced the focus fd (an earlier
read-only open can reuse the same literal — the anchor must be the
write-intent one), then collects every write/writev on that fd up to and
including the focus, capped at 6 events. io-mismatch delivers no slice — the
decoded stdout previews already localize those.

### driver/ — marshaling, original calls, container boundary

- **spec.py**: `Param` (kinds `i32`/`buffer_i32`/`cstring`, direction,
  length_param, range default `(-100,100)`, `ret` carried on `params[0]`) with
  `from_json`/`to_json`.
- **calling.py**: `call_original(binary, addr, params, case)` executes the
  original function under qiling (scratch rootfs; `SENTINEL` return trap;
  register args in SysV order; memory readback for `buffer_i32`/`cstring`;
  timeout/fault → `exit_code: -1` convention). `batch_call_original(..., cases)`
  is the same trace for a whole case list on one VM (`ql.save`/`ql.restore`
  per case; shared `_run_case` marshaling) — unless the symtab-bounded
  syscall scan finds a `syscall`/`sysenter` opcode (or no bound exists),
  in which case it falls back to per-case fresh VMs; each trace's
  `batch_mode` names the route. `gen_inputs` produces fuzz
  cases (poison-filled out buffers).
- **podrun.py**: `ensure_image()` (hard refusal with the build command when
  podman/image is absent) + `run_worker(job, workdir)`: one-shot rootless
  container with `--network none --read-only --tmpfs /tmp:rw,size=64m
  --memory 1g --pids-limit 128`, repo `src` mounted read-only, and `workdir`
  mounted rw with a hard contract: its contents are fully readable by
  agent-authored C, so callers compiling/executing agent sources pass a
  per-round scratch dir holding ONLY the model source — the task dir
  (traces, ledger, accepted entries) never enters the mount. Timeout is
  sized from the case budget.
- **native_worker.py** (pure stdlib, runs inside the container): `validate`
  (compile `gcc -O1 -shared -fPIC`, RTLD_NOW symbol check, fork-per-case
  ctypes calls with a 5s budget → per-case crash results instead of harness
  death, stop after the first crash), `compile-link` (compose), `compile`
  (batched corpus/model compile jobs), `strip` (binutils is image-pinned).

### corpus/generate.py — 48-slot seed matrix

Four seeds (`rot13`, `check`, `calc`, `filewrite`) × gcc/clang × O0/O1/O2 ×
sym/stripped = 48 slots. All compiles run via worker `compile` jobs inside the
image, and stripped variants are finalized with `strip -s` executed in the
same image (binutils is image-pinned; no host binary tools are invoked in the
corpus artifact path — host-side symtab reads happen before stripping, so
manifest addresses are captured pre-strip). The manifest (`task_id`, binary,
`functions` addresses pre-strip, seed/compiler/opt/stripped) merges targeted
builds in canonical full-build order; unfiltered builds regenerate the
manifest from the current plan (stale slots pruned), targeted builds merge,
preserving out-of-scope entries.

### disasm/ — task_open facts

`analyze.function_insns` is the single loader (elftools + capstone detail).
`analyze.analyze_function(binary, functions)` returns per-function
`{arity_guess, returns_hint, callees, labeled}`. The arity guess reads the
preamble (everything up to the first resolved call site): a SysV arg register
counts as an argument only if it is *purely* read — read, never read-write —
before any write into its alias group. Read-write uses (like `cdq`/`idiv` on
edx) are excluded, since implicit-instruction scratch would fake an argument.
Thin wrappers earn the second piece: at the first resolved call, any of the
callee's own argument groups that this function never writes count as
pass-throughs (e.g. `check_pw`). `returns_hint` fires on an eax write in a
ret-terminated block or a leading accumulator-init; `callees` resolve direct
`call rel32` targets against manifest addresses. Validated against the full
corpus matrix by test; labeled a guess in payload.

### memory.py — deduction cache

Per-family deduction cache at `.reschema/memory/<seed>.jsonl` (temp file +
atomic replace, single-process like the task ledger). Two tiers (see
[Terms](#terms)): `verified_fact` written only by the harness on gate
acceptance (function mode: `{fn, params, c_source, n_fuzz, audit_seed,
topology}`; program mode: `fn: "__main__"`), and `unverified_hypothesis`
agent notes via `submit_model(notes=[...])`, promoted only if the annotated
submission itself is accepted. `task_open` injects family-matched entries
(`{seed, function}` or `__main__`) with their tier tags; the hidden gate's
strictness is unchanged — memory accelerates pathfinding only.

**Presentation tier (#92/#93, config B):** additive framing over the raw
`memory` list, owned by `memory.present()`. On a non-empty cache,
`memory_provenance` carries a constant natural-language sentence
(harness-verified, not agent-claimed); when the cache holds a
`verified_fact`, `ready_to_submit` distills the NEWEST fact into an action
card (`c_source`, `fn`, `verified_on`, static `note`, plus `params` for
function-mode facts). Card content is sourced exclusively from
`verified_fact` entries — agent notes (promoted or not) never reach it
(negative-tested); cache keying and gate strictness are untouched.

The cache's cost effect is wired into CI by the reference benchmark
(`tests/test_transfer_protocol.py`): when the cache carries a verified
source, later family slots accept with zero probes (trajectory
`[exp(-0.75), 1.0, 1.0]` vs the flat unprimed baseline). That trajectory is
an instrumentation tautology check — it demonstrates the plumbing, not
transfer in a live agent; a live-agent measurement is still pending (see
`docs/benchmark-protocol.md`).

### tools/dogfood/ — 2C live-agent transfer driver

Measures what the CI reference run deliberately cannot: whether a REAL agent
benefits from the family memory the harness ships. `slot.py` is the atom: one
live-agent run of one corpus slot — layout (primed chains share one
`RESCHEMA_HOME` across their 3 slots; unprimed opens memory-cold per slot,
enforced by filesystem, the CI isolation invariant), agent spawn, ledger
polling with typed guards (`aborted: timeout|probe-ceiling|agent-exit|
priming-failed`, `infra-error` for endpoint failure), one atomic JSONL record
per slot. `driver.py` expands TOML campaigns into a bounded pool; primed
chains run SEQUENTIALLY inside one worker task (a later slot must open after
slot 0's acceptance wrote the verified_fact it exists to consume; a failing
chain slot short-circuits the rest as `aborted: priming-failed`, but an
endpoint flap leaves the chain resumable). Resume is filesystem truth:
budget-consuming outcomes skip, `infra-error` retries. `runners/base.py`'s
`AgentRunner` Protocol keeps the driver harness-free — `opencode_v1.py` is
today's adapter: session config allowlisting exactly the 5 MCP tools
(top-level `tools` switchboard with `reschema_*` wildcard, empty sandbox cwd,
sanitized HOME/XDG so global config can't leak in — the agent physically
cannot read this repo's seeds), GET/POST preflight language the endpoint
actually speaks, process-group lifecycle. `prompt.py` is one neutral task
template whose neutrality is lint-tested against a stemmed forbidden list and
pinned by a golden digest — prompt contamination is solver scaffolding and
would silently reframe the measurement. `measure.py` is the only statistics
site: φ per (family, rep) as the protocol requires, median/IQR across reps,
rejected slots at E=0 (survivor-bias-free), infra-error evidence shown but
never entering statistics, per-slot deltas when the unprimed trajectory isn't
flat. Reports land `report-<family>.md` beside the JSONL under
`docs/benchmark-results/2c/`; invocation: `uv run python -m
tools.dogfood.driver <campaign.toml> --out <dir> [--pool N]` (AGENTS.md
carries the smoke checklist). 46 CI-safe tests run the whole pipeline against
`FakeRunner`; no LLM, podman, or endpoint in CI.

## Key architectural decisions

1. **Single pinned toolchain image for everything binary** — corpus
   seeds and both levels' model compiles run inside the same container image,
   so guest binaries carry identical toolchain/libc encodings on every
   machine; ambient host toolchains are out of the trust surface.
2. **Emulation and native execution are contained, not enumerated** —
   scratch rootfs (for qiling) and one-shot containers (for native) provide
   structural containment; no syscall allowlist is trusted.
3. **Hidden-state economy over reported-state completeness** — hidden
   inputs are drawn with fresh entropy per submission and ground truth is
   double-recorded; rejections reveal the first divergence only (extraction
   is priced in submissions; the hidden gate backstops overfitting).
4. **Determinism-by-canonicalization + machine-checked versioning** — all
   comparisons apply to canonicalized traces; a canonicalizer rules change
   forces corpus re-record via the sidecar check.
5. **Corpus-as-oracle for heuristics** — `task_open`'s signature
   guesses/callees are validated against the full 48-slot corpus matrix in
   the test suite, keeping compiler/capstone drift observable in CI.
6. **Structured judgment at every boundary** — MCP errors, compile
   failures, divergences, crashes, and starvation are all typed payloads;
   agents parse verdicts, never stderr pages.
7. **Ledger is the only cross-run memory** — accepted
   sources/audit/journal are cumulative state by design; composition
   re-compiles accepted sources per-TU rather than trusting earlier
   artifacts.
8. **No ambient host binaries anywhere in the corpus path** — compilation
   and stripping of corpus binaries happen inside the toolchain image; a
   binutils-less host cannot produce degraded corpora.

## Decision records

Each record leads with the shipped decision and its rationale; the delta
against the (non-public) original plans is kept as history, subordinate.

- **Level-B execution is contained end-to-end** — function-mode models
  compile *and* execute inside mandatory one-shot podman containers; ctypes
  never runs in the engine process. Untrusted source deserves no reachable
  host. (History: the original design recorded "function-mode models execute
  natively (ctypes) as a throughput decision… no additional sandboxing" as a
  guardrail.)
- **Qiling records against a fresh empty rootfs** — every record and
  `call_original` runs with a scratch rootfs holding only the guest binary.
  (History: qiling ran with rootfs `/` until a demonstrated host-write leak —
  a guest `remove()`/`mkdir()` reached host paths.)
- **No host gcc/strip anywhere** — models and corpus compile only inside
  the toolchain image (this also resolved the dirfd-ABI flakiness across
  host/CI glibc versions; recorder-test probe binaries included). (History:
  models compiled on the host for both levels; a later host `strip` residue
  in corpus builds moved into the image as well.)
- **Corpus shape: 48 slots with a file-writing seed** — the `filewrite`
  seed, the `files_written` gate, and a raw-bytes hidden domain
  (`stdin-bytes`) make the file channel first-class; `corpus_build(seed_ids,
  matrix)` targeting exists with merge-and-prune semantics. (History: plan
  said 36 slots, 3 seeds, no targeting.)
- **Canonicalizer is v2.1** — FD and PATH ordinals plus the enforced
  version stamp. (History: planned v1 was ADDR ordinals + argv basename.)
- **task_open carries a contract surface** — disasm slice, known callees,
  labeled signature guess, and ABI template. (History: descoped in the
  original spec.)
- **status is a snapshot, not a counter** — readiness/coverage/recent
  journal/efficiency from ledger+manifest. (History: descoped to recorded
  count + ledger.) See the "No replay-% metric" record below for the
  accept-payload field that died with it.
- **Program-path ledger accounting is symmetric** — counters, idempotent
  accept markers, and a `recent` journal. (History: the first implementation
  left counters untouched and stacked duplicate accept markers.)
- **qiling is bounded to 1.4.x** — the code carries 1.4.6-specific
  adjustments in `exec/recorder.py` and `driver/calling.py` (hook arg
  shapes, exit-code defaults, `QlErrorBase` repr recursion, loader stack
  API); hence `qiling>=1.4.6,<1.5`, and a `pillow>=12.3.0` override is forced
  over `python-fx`'s `pillow<11` cap to clear transitive CVEs (the image/TUI
  paths of python-fx/asciimatics are never exercised).
- **The tool table is exactly five tools; the shipped signatures are
  canonical** — `compose()` is deliberately not exposed. (History: a spec
  §5 table existed with different literal parameter names; per issue #44
  decision, the shipped names are the canonical table — no external consumer
  bound to the old names, and the contract test pins term coverage.)
- **No replay-% metric** — issue #43 closed without a denominator: readiness
  (recorded vs hidden-gate minimum), coverage (accepted/total), the
  audit-trail seeds, and the efficiency metric already subsume the uses.
  (History: program accepts carried a constant `replay_pct: 100` placeholder;
  the placeholder is removed, and the test suite pins its absence.)
- **compose() is a pure linkage check by policy** — accepted sources are
  compiled per-TU and linked, nothing executes the composed binary (issue
  #46): execution semantics for composed programs are undefined scope until
  a real consumer appears; linkability is what the dogfood tests assert.
- **Agent-C compile/execution mounts scratch, not the oracle store** —
  information isolation, not just host isolation: worker containers
  bind-mount a per-round scratch directory holding only the model source.
  (History: the task dir — recorded traces, ledger, accepted sources — was
  itself the rw mount, and gcc's habit of quoting offending source lines in
  diagnostics made `#include "trace_*.json"` an agent-readable channel into
  ground truth; function-mode runtime `fopen` was the same channel. Issue
  #61; closed structurally by mount, not by filtering stderr.)
- **Research slots landed as primitives, not pipelines** — four ideas from
  the 2025–26 RE-literature sweep (docs/roadmap.md), each refactored onto an
  existing surface rather than adding a subsystem: the two-pass repair
  directive (coaching context in `task_open`), syscall dependency slices
  (`dep_slice` in event/files divergences), name-independent topology
  digests on `verified_fact` entries, and the function-mode scout probe
  (one emulated call, no case persisted).
- **Harness capabilities are runtime-agnostic by construction** — the five
  anti-lock-in principles of issue #86 audited against the shipped code; four
  are already structural, one is deferred. (1) Capabilities hold no
  orchestrator state: `mcp/server.py` is dispatch-only, the engine owns all
  tool logic, and the dogfood `AgentRunner` Protocol already drives the same
  five tools from a second, unrelated harness. (2) The transport boundary is
  MCP over stdio, consumable by any compliant client. (3) Tool contracts ship
  as JSON Schema on the wire: `tools/list` serves every tool's `inputSchema`;
  the Python type hints are an authoring surface, never the contract a client
  binds to, and the contract test pins description coverage. (4) Side-effects
  execute out-of-process: every compile/execution inside one-shot rootless
  podman containers, emulation against a fresh empty rootfs, host gcc never
  in the trust surface. (5) Prompt-as-a-skill is the one open gap: procedural
  coaching ships today as structured payload fields (`repair_directive`,
  family-memory injection) and contract-pinned tool descriptions, not as MCP
  `prompts`/`resources` — tracked as issue #88.
- **Scope guardrails observed** — x86-64 static ELFs only, ≤6 register
  integer args (no stack args, no structs/floats), no multi-arch, packing, or
  symbolic equivalence; no branch coverage (explicitly cut).

## Verification & maintenance

This document is verified against the delivered codebase. Latest FULL
line-by-line audit: 2026-08-12 (phase-2 closeout: scratch-mount isolation,
xdist/budget suite, `tools/dogfood/`, evidence records, and the three
issue-closing decision records folded in). It rots by default — policy: any
PR that changes behavior described here must update this file in the same
commit; a dated claim a reviewer can falsify from `src/` is a doc bug, file it.
