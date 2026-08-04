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

## Terms

The standard reverse-engineering vocabulary (SysV order, symtab, rel32,
RTLD_NOW, ...) is deliberately left undefined — it is googleable. These are
the project's own words, used everywhere and defined nowhere else:

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
  priced in the efficiency metric.
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
4. Ground truth per case: `driver/calling.call_original` runs the original
   function under qiling (SENTINEL return trap, SysV register args, memory
   readback). Cases where the *original* faults are skipped — a crash is not
   a behavior spec.
5. The model is compiled and executed in ONE worker round trip (`validate`
   job): `gcc -O1 -shared -fPIC` in the container, RTLD_NOW load + symbol
   presence check, then fork-per-case ctypes calls under the 5s case budget.
   A model segfault/hang surfaces as a per-case `crash` result (first crash
   stops the round), never a wedged harness.
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

- program: `{accepted: true, replay_pct, recorded_cases, hidden_cases,
  hidden_seed}` — `replay_pct` is currently always `100`, a stub: the real
  replay-% metric is undefined and unshipped (see
  [Decision records](#decision-records)).
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
- **_abi_template / open_function_task** emit a compile-ready header skeleton
  plus the capstone-derived facts and a labeled signature guess.

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

### driver/ — marshaling, original calls, container boundary

- **spec.py**: `Param` (kinds `i32`/`buffer_i32`/`cstring`, direction,
  length_param, range default `(-100,100)`, `ret` carried on `params[0]`) with
  `from_json`/`to_json`.
- **calling.py**: `call_original(binary, addr, params, case)` executes the
  original function under qiling (scratch rootfs; `SENTINEL` return trap;
  register args in SysV order; memory readback for `buffer_i32`/`cstring`;
  timeout/fault → `exit_code: -1` convention). `gen_inputs` produces fuzz
  cases (poison-filled out buffers).
- **podrun.py**: `ensure_image()` (hard refusal with the build command when
  podman/image is absent) + `run_worker(job, workdir)`: one-shot rootless
  container with `--network none --read-only --tmpfs /tmp:rw,size=64m
  --memory 1g --pids-limit 128`, repo `src` mounted read-only, task dir
  mounted rw, timeout sized from the case budget.
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
`{arity_guess, returns_hint, callees, labeled}`: arity from pure-read SysV
arg-register families in the preamble plus pass-through credit at the first
resolved call; returns-hint from an eax write in a ret-terminated block or a
leading accumulator-init; callees from direct `call rel32` targets resolved
against manifest addresses. Validated against the full corpus matrix by test;
labeled a guess in payload.

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

The cache's cost effect is wired into CI by the reference benchmark
(`tests/test_transfer_protocol.py`): when the cache carries a verified
source, later family slots accept with zero probes (trajectory
`[exp(-0.75), 1.0, 1.0]` vs the flat unprimed baseline). That trajectory is
an instrumentation tautology check — it demonstrates the plumbing, not
transfer in a live agent; a live-agent measurement is still pending (see
`docs/benchmark-protocol.md`).

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
  count + ledger.) Known wart: program accepts still carry a `replay_pct:
  100` stub; a real replay-% metric is undefined and unshipped.
- **Program-path ledger accounting is symmetric** — counters, idempotent
  accept markers, and a `recent` journal. (History: the first implementation
  left counters untouched and stacked duplicate accept markers.)
- **qiling is bounded to 1.4.x** — the code carries 1.4.6-specific
  adjustments in `exec/recorder.py` and `driver/calling.py` (hook arg
  shapes, exit-code defaults, `QlErrorBase` repr recursion, loader stack
  API); hence `qiling>=1.4.6,<1.5`, and a `pillow>=12.3.0` override is forced
  over `python-fx`'s `pillow<11` cap to clear transitive CVEs (the image/TUI
  paths of python-fx/asciimatics are never exercised).
- **The tool table is exactly five tools, matching spec behavior** — some
  literal parameter names diverge from the spec's §5 table; `compose()` is
  deliberately not exposed.
- **Scope guardrails observed** — x86-64 static ELFs only, ≤6 register
  integer args (no stack args, no structs/floats), no multi-arch, packing, or
  symbolic equivalence; no branch coverage (explicitly cut).

## Verification & maintenance

This document is verified against the delivered codebase; the last full
line-by-line pass was 2026-08-02 (commit `ad06b37`) with a partial refresh on
2026-08-04. It rots by default — policy: any PR that changes behavior
described here must update this file in the same commit; a dated claim a
reviewer can falsify from `src/` is a doc bug, file it.
