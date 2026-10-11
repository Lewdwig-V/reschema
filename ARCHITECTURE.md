# ReSchema Architecture

- [Overview](#overview)
- [Terms](#terms)
- [Component boundaries & data flow](#component-boundaries--data-flow)
- [Life of a submission](#life-of-a-submission)
- [The rejection payload](#the-rejection-payload)
- [Components](#components)
- [Key architectural decisions](#key-architectural-decisions)
- [Decision records](#decision-records)
- [Verification & maintenance](#verification--maintenance)

## Overview

ReSchema is a [Warranted](https://github.com/Lewdwig-V/warranted) domain
(`reschema.domain:ReSchemaDomain`, Warranted pinned at v0.4.0) that makes an
LLM agent hold executable C world-models of x86-64 static ELFs. Ground truth
is recorded by emulating the corpus binaries (qiling, on the host), and the
agent's C candidate is judged against it by two checkers: **program-replay**
(whole-program canonical trace replay of the worker's nominated cases plus
8 hidden fresh inputs per check) and **function-fuzz** (differential fuzzing
over `{ret, mem}` against the original function). Rejections are always
structured feedback, never tracebacks.

Warranted owns everything that is not reverse engineering: task state, the
evidence ledger and accounting, budgets, the duplicate guard, the worker
sandbox, contained checker jobs, and verified vs unverified memory. The port
and its decisions (D1–D3) are in `docs/proposals/m8-rebuild-on-warranted.md`;
the engine ledger, MCP server and live-agent driver it replaced were removed
in M8 S7.

The harness is adversarial by design: agent-authored C is compiled and
executed only inside Warranted jobs from the pinned toolchain image
(`Containerfile`, debian trixie + gcc + clang + pinned qiling), with no
network, no host mounts and a read-only root. Qiling emulation of the
originals runs against a **fresh empty rootfs**; host `gcc` is never invoked.

ReSchema's own on-disk state is the corpus under `.reschema/corpus/`
(`RESCHEMA_HOME`-overridable; the test suite uses it for per-worker
isolation under pytest-xdist). Everything a run produces lives in the
Warranted project.

## Terms

Standard reverse-engineering vocabulary is assumed. These are the project's
own words:

- **case** — one input run: an `(argv, stdin)` pair plus the trace it
  produced. A program candidate nominates its cases in `cases.json`; the
  checker re-records each on the corpus binary (D3).
- **record** — executing a binary under qiling and capturing its trace
  (`exec/recorder.record`).
- **trace** — the JSON dict for one run: `argv`, `stdin_hex`,
  `stdout`/`stderr` (hex), `exit_code`, `files_written`, `events`.
- **event** — one observed syscall in a trace: `{sc, phase, args, result?}`
  where `phase` is `enter` or `exit` (the recorder hooks both).
- **probe** — one `experiment` or `experiment_function` operation, charged
  one `probe` unit by Warranted.
- **slot** — one cell of the corpus build matrix, e.g. `gcc-O2-sym`
  (compiler × optimization × symbols kept vs stripped). A slot ID is
  `{seed}::{slot}`; its tasks are `{slot ID}::fn::<name>` and
  `{slot ID}::program`.
- **family** — all slots derived from one seed: the tasks' shared memory
  scope. `family.json` (seed, seed source digest, function names,
  canonicalizer version) is the memory dependency, so a change to any of
  them marks earlier facts stale.
- **write-family shape** — the syscall skeleton the program checker compares:
  `(sc, fd, byte count)` for `write`/`writev`/`exit_group`, with address
  arguments wildcarded.
- **canonicalize** — rewrite legitimately-divergent tokens before comparing
  traces (`ADDR_<n>`, `FD_<n>`, `PATH_<n>`, argv[0] → basename). Rules are
  versioned (`2.1`); a rules change forces a corpus rebuild.
- **hidden-starvation** — a check is rejected when fewer than `HIDDEN_N=8`
  distinct usable hidden inputs can be drawn: a loud failure instead of a
  vacuous pass.
- **event-split** — a model emitting output in different syscall-sized chunks
  than the original. Chunking is observable, so it diverges and rejects.
- **dep_slice** — the fd-linked backward syscall chain attached to
  event-divergence and files-mismatch rejects, capped at 6 raw events.
- **poison-filled** — fuzz out-buffers pre-loaded with random bytes, so a
  no-op model leaves detectable garbage instead of matching a
  memory-preserving original.
- **case budget** — the 5s wall-clock limit per fuzz case in the native
  worker (one fork per case); the job timeout is sized from it.
- **check evidence** — what Warranted records for each check: the seeds the
  checker drew (`seeds.json`), its jobs (`jobs.json`: image, argv, input
  digests, limits, exit status) and their output, and the verdict.

## Component boundaries & data flow

```
warranted CLI / Project ──> ReSchemaDomain (domain.py)
   │ task specs (tasks.py)          checkers (checks/)          operations
   │  inputs: task.md,               function-fuzz ─┐            experiment
   │  family.json, disasm,           program-replay ┤            experiment_function
   │  abi_template.c, ...            (host: qiling  │             (host: qiling on
   │  private: binary, task.json     ground truth)  │              the private binary)
   │                                                └─ ctx.run_job: the model,
   │                                                   in the toolchain image
   └─ worker sandbox (toolchain image + tools/worker: experiment, submit)

corpus.generate ──> toolchain.run_build   60-slot seed matrix + manifest
exec.{recorder, canonical}                qiling + empty rootfs; rules v2 (v2.1)
validate.{program, function, scout}       hidden suite + compare; fuzz gate
regrade                                   current checkers over recorded evidence
```

## Life of a submission

The worker writes `workspace/model.c` and its declaration, then runs
`submit`, which packs them into `/work/result.json`. Warranted captures the
candidate, applies the duplicate guard (`ReSchemaDomain.normalize`:
comment/whitespace-stripped source plus the canonical declaration), then
runs the task's checker in a `CheckContext`.

### Program task (`program-replay`, decisions D1 and D3)

1. The candidate is `model.c` plus `cases.json`: at most 16 nominated cases
   `{argv, stdin_hex}`. A malformed candidate is a `candidate`/`cases`
   reject before anything runs.
2. **Ground truth on the host.** Each nominated case is double-recorded on
   the private corpus binary (a flaky case rejects as `flaky-case`, the
   input's fault). The hidden suite is drawn from `ctx.draw_seed()` (fresh
   per check, recorded as check evidence): `HIDDEN_N` distinct inputs,
   deduplicated against the nominated ones, each double-recorded; crashing
   or flaky draws are redrawn.
3. **The model in one job.** The job receives only the model source, the
   recorder, the canonicaliser and the case INPUTS. It compiles the model
   `gcc -O1 -static -fno-pie -no-pie -g0` and replays every case under
   qiling, printing canonical traces. Ground truth never enters the job, so
   its output is the model's claim, shape-checked on the host.
4. **Recorded stage**, then **hidden stage**: `validate.program.compare`
   checks per case, in order, stdout/stderr/exit_code (byte-exact),
   `files_written` (paths and bytes) and the write-family event shape. The
   first divergence wins. Too few hidden inputs is `hidden-starvation`.
5. **Accept**: the fact `{fn: "__main__", c_source}` (or its digest when too
   large) becomes verified memory for the family.

### Function task (`function-fuzz`, decision D2)

1. The candidate is `model.c` plus `params.json` (the declared parameter
   list). A malformed declaration is a `spec` reject.
2. `validate/function.validate_function` applies the spec floors (more than
   6 register args: stage `arity`; `ret: "void"` with no memory-channel
   param: stage `spec`), draws the fuzz cases from `ctx.draw_seed()`
   (`N_FUZZ=64`, uniform plus the static-immediates scout slice,
   poison-filled out buffers) and records ground truth with
   `driver/calling.batch_call_original` under qiling on the host. Original
   timeouts are skipped; an original memory fault on a declared case is a
   `spec` reject; total skipping is `skip-starvation`; fewer than 2
   distinct inputs is a `spec` reject.
3. The model is compiled and called in ONE job
   (`checks/function_fuzz.worker_job`): `native_worker.py` and `spec.py` as
   flat files, `TMPDIR=/work` (the job's `/tmp` is noexec), `gcc -O1 -shared
   -fPIC`, RTLD_NOW load and symbol check, then fork-per-case ctypes calls
   under the 5s case budget. A crash or hang is a per-case `crash` result.
   The job's output is the model's claim: a malformed or short result list
   is a `worker` reject, and any stage it reports is a reject, never an
   infrastructure failure.
4. Per case, `{ret, mem}` is compared on the host (`ret` skipped for void
   specs). The first mismatch rejects with `{input, field, expected,
   actual, seed}`.
5. **Accept**: the fact `{function, params, source}` (or digests) becomes
   verified memory, which the family's later tasks, the program task
   included, see in `memory.json`.

Note the deliberate substrate asymmetry: the *original* always executes under
emulation (fidelity), while the *model* `.so` always executes natively inside
a job (containment for untrusted code).

## The rejection payload

A checker's verdict feedback is what the worker sees, capped under
Warranted's `FEEDBACK_LIMIT` (divergence payloads are shrunk to fit).

- **Accepts:** program `{accepted: true, recorded_cases, hidden_cases}`;
  function `{accepted: true, compared, skipped}`.
- **Program rejects:** `{accepted: false, reason, stage, ...}` with `reason`
  one of `io-mismatch`, `files-mismatch`, `event-divergence`, `event-length`
  (`stage: recorded|hidden`, with a `divergence` describing the FIRST
  divergence: decoded previews for io, hex elsewhere, plus a `dep_slice` for
  event/files divergences), `compile` (with the compiler's `detail`),
  `hidden-starvation`, `flaky-case`, `candidate`/`cases` (malformed
  candidate), `replay-fault`/`replay-timeout`/`replay-output` (the job).
- **Function rejects:** `{accepted: false, reason: "spec", detail}` for a
  malformed candidate or declaration; otherwise `{accepted: false,
  divergence}` where `divergence` is either a behavior divergence `{input,
  field, expected, actual, seed}` (`field` in `ret|mem|crash`) or a floor
  verdict `{stage, detail}` (`arity`, `spec`, `skip-starvation`, `compile`,
  `link`, `symbol`, `worker`). Clients read `divergence.stage` first and
  fall back to top-level `reason`.
- **Duplicates** are Warranted's: a candidate the guard refuses is decided
  `duplicate` without a check, and still uses a submission.
- **Host faults** (missing image, job runner failure) raise; Warranted
  records the check as an infrastructure failure, never a verdict.

## Components

### domain.py, tasks.py, operations.py — the Warranted surface

- **ReSchemaDomain**: `checkers` (`function-fuzz`, `program-replay`),
  `operations` (`experiment`, `experiment_function`), `sources` (the whole
  `reschema` package, so any change is a new domain identity), `normalize`
  (the duplicate guard's fingerprint), `worker_image` (the toolchain image
  ID).
- **tasks.py**: `function_task`/`program_task`/`slot_tasks` build the
  `TaskSpec`s for a slot (function tasks before the program task, one memory
  scope per seed, `DuplicateGuard(exact_repeats=2, near_repeats=3,
  near_edit_floor=24, near_edit_percent=6, window=8)`); `task_md` renders the
  worker's instructions from the checkers' own limits; `write_campaign`
  writes the TOML files `warranted campaign run` reads.
- **operations.py**: `experiment` records the private binary on
  worker-chosen argv/stdin (double-recorded, canonical trace shown, large
  traces delivered as a file); `experiment_function` calls the task's
  function (target from the private `task.json`, never the worker) once on
  a worker-chosen case. One `probe` each. Experiments are hints: no checker
  reads them (D3).

### checks/ — the two checkers

`program_replay.ProgramReplay` and `function_fuzz.FunctionFuzz`, as in
[Life of a submission](#life-of-a-submission). `checks.candidate_files`
reads the candidate either as files or from the `submit` helper's
`result.json`; a name given both ways is malformed.

### exec/recorder.py — ground-truth records

Each `record(binary, argv, stdin)` copies the binary into a fresh empty
rootfs, hooks `read/write/writev/open/openat/creat/close/brk/mmap/exit_group`
ENTER+EXIT, and returns
`{argv, stdin_hex, stdout(hex), stderr(hex), exit_code,
files_written, events}`. `files_written` is a post-run scrape of the rootfs
(real fs semantics for truncate/rename/append); crashes/timeouts produce
`exit_code: -1` plus a trailing fault event. Ground truth is always
double-recorded (`validate.program.record_stable`).

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
sidecar; `corpus.generate.load_manifest` hard-refuses a version mismatch
with a rebuild hint. The version is also in every task's `family.json`, so
a rules change marks the family's memory stale.

### validate/program.py — program gate (host side)

`record_stable` double-records ground truth; `hidden_suite` draws and
records the hidden cases; `compare(traces, got)` compares per case:
`stdout`/`stderr`/`exit_code` byte-exact (with latin-1 decoded previews in
divergences), `files_written` map byte-exact, and the observable event
channel `OBS = ("write", "writev", "exit_group")` with `ADDR_*` wildcards on
address ordinals (event fd/count shape is significant; chunking is
observable). Reason order: io-mismatch → files-mismatch →
event-divergence/event-length; divergence on the first mismatch only.
`hidden_input_stream` yields text charset draws by mode and `stdin-bytes`
draws (random bytes with a guaranteed NUL and ≥0x80 byte) for binary-safe
seeds; `INPUT_MODE` selects the mode per seed. Seeds with
real wire formats override this per-name: `_SEED_GRAMMARS` (`pkfmt`) makes
60% of hidden draws seed-grammar packets (real magic/version/records + the
structured attack variants), interleaved with the uniform stream. Truncation
draws overclaim the final payload without adding bytes to satisfy the claim.
Without grammar-aware draws, valid magic is rare and an always-`"bad magic"`
stub can pass. The hidden suite is finite random sampling, not guaranteed
path coverage or proof of equivalence (codex P1 on #125; the regression
attack is the golden fixture's `pkfmt-o2-always-bad-magic`).

Event-divergence and files-mismatch payloads carry a `dep_slice`
([Terms](#terms)): the validator searches backward from the focus event for
the LAST write-intent open pair that produced the focus fd (an earlier
read-only open can reuse the same literal — the anchor must be the
write-intent one), then collects every write/writev on that fd up to and
including the focus, capped at 6 events. io-mismatch delivers no slice — the
decoded stdout previews already localize those.

### driver/ — marshaling, original calls, the native worker

- **spec.py**: `Param` (kinds `i32`/`buffer_i32`/`cstring`, length_param,
  range default `(-100,100)`, `ret` carried on `params[0]`) with
  `from_json`/`to_json`.
- **calling.py**: `call_original(binary, addr, params, case)` executes the
  original function under qiling (scratch rootfs; `SENTINEL` return trap;
  register args in SysV order; memory readback for `buffer_i32`/`cstring`;
  timeout/fault → `exit_code: -1` convention). `batch_call_original(...,
  cases)` runs a whole case list on one VM (`ql.save`/`ql.restore` per case)
  unless the symtab-bounded syscall scan finds a `syscall`/`sysenter` opcode
  (or no bound exists), in which case it falls back to per-case fresh VMs;
  each trace's `batch_mode` names the route. The snapshot contract and the
  `hook_scope` lifecycle are pinned in `tests/test_driver.py`. `gen_inputs`
  produces fuzz cases (poison-filled out buffers).
- **native_worker.py** (pure stdlib, runs inside the image): `validate`
  (the function checker's job), `compile` and `strip` (the corpus build),
  `compile-link`.

### toolchain.py — the pinned image

`image_id()` resolves `localhost/reschema-toolchain:1` to its image ID, which
pins every checker job and the worker sandbox (Warranted records it per job
in `jobs.json`). `run_build(job, workdir)` is the corpus build's one-shot
container: `--network none --read-only`, `src` mounted read-only, `workdir`
mounted rw so the binaries come back. Only ReSchema's own seed sources ever
go through it.

### corpus/generate.py — 60-slot seed matrix

Five seeds (`rot13`, `check`, `calc`, `filewrite`, `pkfmt`) × gcc/clang ×
O0/O1/O2 × sym/stripped = 60 slots. All compiles run via the worker's `compile` mode
(`toolchain.run_build`, the one bind-mounted container: trusted seed
sources only) inside the image, and stripped variants are finalized with `strip -s` executed in the
same image (binutils is image-pinned; no host binary tools are invoked in the
corpus artifact path — host-side symtab reads happen before stripping, so
manifest addresses are captured pre-strip). The manifest (`task_id`, binary,
`functions` addresses pre-strip, seed/compiler/opt/stripped) merges targeted
builds in canonical full-build order; unfiltered builds regenerate the
manifest from the current plan (stale slots pruned), targeted builds merge,
preserving out-of-scope entries.

`pkfmt` accepts exactly the header's record count, with no trailing bytes,
at most 4096 packet bytes and 1024 bytes per record payload. Unknown record
tags are skipped by extraction. The wire-format entry point `main` performs
libc I/O and runs via the whole-program recorder, not snapshot batching.
Only `pk_version_ok` is exposed as a function task. `pk_extract` and
`pk_checksum` require a readable byte buffer for valid lengths (4..4096);
both reject other lengths before dereference. Their unsigned results use
guest `.bss` `pk_errno` for errors, reset at each call and included in the
memory snapshot. Magic/version validation belongs to `main`; checksum only
hashes the bytes after the header. No arbitrary-pointer safety is promised.

`tests/test_pkfmt.py` checks the public surface and snapshot/fresh-VM route
on all 12 build slots. On the six symbol-bearing builds it also checks the
three helper slices for calls, external/indirect jumps and syscall opcodes,
and invokes the buffer helpers with explicit test-only ABI setup, guarded
buffers, size/shape boundaries, and restored state in both case orders.
These are bounded regression checks, not a general snapshot-safety proof:
the driver's opcode screen does not follow callees, and stripped functions
still take the fresh-VM fallback. Byte-buffer Param support and new coverage
and timing measurements remain follow-ups.

### disasm/ — function task facts

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

### regrade.py — re-grade recorded checks

`uv run python -m reschema.regrade PROJECT [--run R] [--submission N]`
re-runs the CURRENT checker of each completed check in a Warranted project
over what that check saw: the candidate files, the task's worker-visible and
private files and the recorded seeds, replayed in order. One JSON line per
check (`run, submission, check, recorded, regraded, holds,
checker_version`, `feedback` on a flip); totals to stderr. Read-only.

Everything but the judge is pinned by the evidence: the corpus binary is the
recorded private file (a corpus rebuild cannot move a verdict); a checker
that draws more seeds than were recorded, or whose recorded jobs ran another
image than today's, is `unreplayable` with a reason, never a flip. Tampered
evidence is refused per row: `Ledger.lookup` re-verifies every artifact
against its digest, and the candidate files must match the submission
record. Warranted (as of v0.4.0) has no public re-check entry point and `Project`
refuses a changed domain identity, so regrade reads the ledger through
`warranted.host` (Lewdwig-V/warranted#78).

## Key architectural decisions

1. **Single pinned toolchain image for everything binary** — corpus seeds,
   both checkers' model jobs and the worker sandbox use one image, pinned by
   its ID, so guest binaries carry identical toolchain/libc encodings on
   every machine; ambient host toolchains are out of the trust surface.
2. **Emulation and native execution are contained, not enumerated** —
   scratch rootfs (for qiling) and one-shot Warranted jobs (for agent C)
   provide structural containment; no syscall allowlist is trusted.
3. **Hidden-state economy over reported-state completeness** — hidden
   inputs are drawn with fresh entropy per check and ground truth is
   double-recorded; rejections reveal the first divergence only.
4. **Determinism-by-canonicalization + machine-checked versioning** — all
   comparisons apply to canonicalized traces; a canonicalizer rules change
   forces a corpus rebuild via the sidecar check, and stales family memory.
5. **Corpus-as-oracle for heuristics** — the function tasks' signature
   guesses/callees are validated against the full 60-slot corpus matrix in
   the test suite, keeping compiler/capstone drift observable in CI.
6. **Structured judgment at every boundary** — compile failures,
   divergences, crashes, starvation and malformed candidates are typed
   feedback; only host faults raise.
7. **ReSchema keeps only reverse engineering** — anything that is not
   (state, accounting, budgets, guard, containment, memory) is Warranted's;
   a misfit is fixed in Warranted, not worked around here.
8. **No ambient host binaries anywhere in the corpus path** — compilation
   and stripping of corpus binaries happen inside the toolchain image.

## Decision records

Each record leads with the shipped decision and its rationale; the delta
against the (non-public) original plans is kept as history, subordinate.

- **Agent C is contained end-to-end** — models compile *and* execute only
  inside Warranted jobs (both levels since M8, decision D1); ctypes and the
  model's qiling replay never run in a host process. Untrusted source
  deserves no reachable host. (History: the original design ran
  function-mode models natively "as a throughput decision… no additional
  sandboxing"; until M8 the level-A model replay ran under qiling on the
  host.)
- **Qiling records against a fresh empty rootfs** — every record and
  `call_original` runs with a scratch rootfs holding only the guest binary.
  (History: qiling ran with rootfs `/` until a demonstrated host-write leak —
  a guest `remove()`/`mkdir()` reached host paths.)
- **No host gcc/strip anywhere** — models and corpus compile only inside
  the toolchain image (this also resolved the dirfd-ABI flakiness across
  host/CI glibc versions; recorder-test probe binaries included). (History:
  models compiled on the host for both levels; a later host `strip` residue
  in corpus builds moved into the image as well.)
- **Corpus shape: 60 slots incl. a file-writing seed and a parser-class
  seed** — the `filewrite` seed makes the `files_written` gate and a
  raw-bytes hidden domain (`stdin-bytes`) first-class;
  `corpus.generate.build(seed_ids=, matrix=)` targeting exists with
  merge-and-prune semantics. (History: plan said 36 slots, 3 seeds, no targeting.)
  **pkfmt (added post-#121):** TLV parser — magic u16, version bounds,
  tag-dispatch records, FNV checksum. The #121 spike motivated a richer
  corpus, but its historical 14-edge
  count and 1.18x overhead verdict predate the complete hook-lifecycle repair
  and must be remeasured with `uv run python -m tools.coverage_spike` before
  reuse. Its error channel is `pk_errno` static state
  (negative results collide with wrapped uint32 accumulators — the recorder
  caught that at seed-rewrite review). Function-mode
  exposure is deliberately `pk_version_ok` ONLY — pointer-buffer helpers
  (`pk_extract`, `pk_checksum`) are not representable specs today (an
  all-i32 sketch marshals pointers as register junk and passes stubs on
  unknown tags, codex P1 on #125); the pointer-kind sketch inference that
  would make them safe tasks is a named follow-up. Hidden sampling is
  grammar-aligned (see validate/program `_SEED_GRAMMARS`), improving the
  chance of rejecting an always-`"bad magic"` stub; a finite hidden draw
  does not guarantee rejection of every overfit model.
- **Canonicalizer is v2.1** — FD and PATH ordinals plus the enforced
  version stamp. (History: planned v1 was ADDR ordinals + argv basename.)
- **Function tasks carry a contract surface** — disasm slice, known
  callees, labeled signature guess, and ABI template, as task inputs.
  (History: descoped in the original spec; served by `task_open` until M8.)
- **qiling is bounded to 1.4.x** — the code carries 1.4.6-specific
  adjustments in `exec/recorder.py` and `driver/calling.py` (hook arg
  shapes, exit-code defaults, `QlErrorBase` repr recursion, loader stack
  API); hence `qiling>=1.4.6,<1.5`, and a `pillow>=12.3.0` override is forced
  over `python-fx`'s `pillow<11` cap to clear transitive CVEs (the image/TUI
  paths of python-fx/asciimatics are never exercised).
- **Superseded in M8 S7 (Warranted owns them now):** the five-tool MCP
  table, `status` snapshots and the efficiency metric over the engine ledger
  (a new metric epoch starts on Warranted accounting), symmetric ledger
  accounting, `compose()` as a linkage check, the flail guard (now
  Warranted's `DuplicateGuard`), the family deduction cache (now Warranted
  memory), the repair directive and continuation feedback, the topology
  digest, and the #86 runtime-agnosticism audit of the MCP surface (the
  worker surface is now files plus the image's helpers).
- **Agent-C jobs see only their own files** — information isolation, not
  just host isolation: a job holds the model source and the harness files it
  needs, never ground truth (the program job gets case inputs only).
  (History: the task dir — recorded traces, ledger — was once the worker
  container's rw mount, and gcc's habit of quoting offending source lines
  made `#include "trace_*.json"` an agent-readable channel into ground truth;
  issue #61 closed it with a scratch mount, M8 with mount-free jobs.)
- **Research slots landed as primitives, not pipelines** — syscall
  dependency slices (`dep_slice` in event/files divergences) and the
  function-mode scout probe (`experiment_function`: one emulated call, no
  case persisted) remain; the repair directive and topology digest went
  with the engine in M8 S7.
- **Refused ideas are recorded, not re-argued** — ideas from the agentic-RE,
  decompilation and code-RL literature that would break judge-only
  acceptance, the entropy policy, first-divergence-only feedback, the trust
  model or the scope guardrails below are listed in
  `docs/rejected-ideas.md`, each with its evidence and an explicit "reopen
  only if" condition. A proposal matching an entry starts from that
  condition.
- **Original timeouts stay skipped, memory faults reject at spec;
  fault-or-not does not enter `field: crash` (yet)** — `tools/crash_census.py` (2026-10-09) ran the gate's own
  64-case draw (fuzz + 109-A scouts) over all 108 function slots: under
  true-signature specs the originals fault on 0/6912 cases; under a
  mistyped spec (every param i32), 2736/6912, all pointer-as-i32 reads, all
  deterministic. A fault-or-not compare would therefore preserve no genuine
  original behavior on today's corpus. It would only turn spec artifacts
  into behavior rejects, which is the wrong stage for them. Reopen when a
  seed has a genuine reachable fault under its true spec.
  `tests/test_crash_census.py::test_ref_specs_never_fault_originals` is a
  SAMPLED tripwire for that (one pinned draw, default ranges), not proof:
  a fault outside the draw passes it (codex P2 on #140). That change ships
  with the
  suppressed-crash negative test and routes accept→reject flips through
  the 3B re-grade (#112).
  **Fault-thinning closed by a spec-stage skip floor:** skip-starvation caught only
  *total* skipping. A hand-declared all-i32 `scale_buf` spec thinned to its
  n≤0 cases (junk pointer never dereferenced), and a `return 0;` stub was
  ACCEPTED on all 12 `scale_buf` slots with fresh seeds. Now, when at
  least one case survives (total faulting stays `skip-starvation`), any
  MEMORY fault of the original on an agent-declared case is a
  `stage: spec` reject naming the faulting case, before the model is
  compiled. A memory fault does not prove a typing error: a correctly typed
  i32 index ranged past a table faults the same way. That spec is still
  rejected (its declared domain exceeds what the original handles), so the
  `detail` offers both remedies, retyping as `buffer_i32`/`cstring` or
  narrowing the range, never a pointer-only hint. Memory faults are classified by unicorn errno
  (READ/WRITE/FETCH × UNMAPPED/PROT/UNALIGNED, `calling.MEM_FAULT_ERRNOS`),
  not message prose: only UNMAPPED says "Invalid memory", and a pointer
  ranged into the read-only static image (`UC_ERR_WRITE_PROT`) bypassed a
  prose match (`tests/test_crash_census.py::test_mistyped_spec_stub_rejected`
  pins both). Scope: the floor closes FAULT-thinned rounds only; a declared
  range that never reaches the dereference (e.g. `n` in [-10, 0]) is the
  general range-coverage limit, not this floor's. Timeouts stay skipped: correct-typed
  `sum_range` over a declared full-i32 range times out on 39/64 cases and
  must still be accepted (`tests/test_validate_function.py::test_skip_floor_ignores_timeouts`;
  the real-binary timeout is pinned by `test_true_spec_faults_on_declared_range`).
  Harness scout cases (109-A) do not count, as for the #100 floor.
- **Scope guardrails observed** — x86-64 static ELFs only, ≤6 register
  integer args (no stack args, no structs/floats), no multi-arch, packing, or
  symbolic equivalence; no branch coverage (explicitly cut).

## Verification & maintenance

This document is verified against the delivered codebase. Latest rewrite:
2026-10-11 (M8 S7: engine, MCP server, memory cache and dogfood driver
removed; checkers, tasks and regrade on Warranted). It rots by default —
policy: any PR that changes behavior described here must update this file
in the same commit; a dated claim a reviewer can falsify from `src/` is a
doc bug, file it.
