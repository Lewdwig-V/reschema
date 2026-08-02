# System Architecture Overview

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

# Component Boundaries & Data Flow

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

## mcp/server.py — 5 tools, dispatch only
`corpus_build(seed_ids?, matrix?)`, `task_open`, `experiment`, `submit_model`,
`status`. No business logic; errors are mapped to `{error: not_found|spec|internal}`
dicts. A contract test pins the term coverage of every tool description.

## engine.py — task state, gates, ledger
`TaskStore` persists per-task state under `.reschema/tasks/<task_id>/`:
canonical case traces (`trace_<label>.json`) and `ledger.json`
(accepted entries, submissions/rejections counters, `audit` seeds, and a
capped `recent` submission journal). Ledger writes are atomic
(temp + `os.replace`); single-process access is assumed.

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
  ledger/manifest only. Probe accounting lives on both experiment paths
  (program-path records and function-path cases), stored as a lazy
  `probes` ledger key.
- **_abi_template / open_function_task** emit a compile-ready header skeleton
  plus the capstone-derived facts and a labeled signature guess.

## exec/recorder.py — ground-truth records
Each `record(binary, argv, stdin)` copies the binary into a fresh empty
rootfs, hooks `read/write/writev/open/openat/creat/close/brk/mmap/exit_group`
ENTER+EXIT, and returns
`{argv, stdin_hex, stdin_sha256, stdout(hex), stderr(hex), exit_code,
files_written, events}`. `files_written` is a post-run scrape of the rootfs
(real fs semantics for truncate/rename/append); crashes/timeouts produce
`exit_code: -1` plus a trailing fault event. Ground truth is always
double-recorded (`_record_stable`) before storage.

## exec/canonical.py — canonicalize (CANONICALIZER_VERSION = "2.1")
Address-shaped tokens → `ADDR_<n>`; argv[0] → basename; fds returned by
write-intent opens → `FD_<n>` by first sighting (fds 0/1/2 keep ABI literals,
read-only opens never enter the table); absolute host paths in string args →
`PATH_<n>`. The corpus manifest directory carries a `canonicalizer_version`
sidecar; `engine.load_manifest` hard-refuses a version mismatch with a
rebuild hint.

## validate/program.py — program gate
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

## driver/ — marshaling, original calls, container boundary
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
  (batched corpus/model compile jobs).

## corpus/generate.py — 48-slot seed matrix
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

## disasm/ — task_open facts
`analyze.function_insns` is the single loader (elftools + capstone detail).
`analyze.analyze_function(binary, functions)` returns per-function
`{arity_guess, returns_hint, callees, labeled}`: arity from pure-read SysV
arg-register families in the preamble plus pass-through credit at the first
resolved call; returns-hint from an eax write in a ret-terminated block or a
leading accumulator-init; callees from direct `call rel32` targets resolved
against manifest addresses. Validated against the full corpus matrix by test;
labeled a guess in payload.

# Key Architectural Decisions

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

# Critical Constraints & Deviations

Where the delivered code intentionally departs from the original specs/plans:

- **Level B contained end-execution, not end-compile only** — the original
  design recorded "function-mode models execute natively (ctypes) as a
  throughput decision… no additional sandboxing" as a guardrail; the shipped
  engine deletes ctypes from the engine process and runs every compile and
  native call inside mandatory podman containers instead.
- **Rootfs narrowing** — qiling was originally run with rootfs `/`; a
  demonstrated host-write leak (guest `remove()`/`mkdir()` touching host
  paths) moved every record and `call_original` to a fresh empty rootfs.
- **Host gcc removed everywhere** — original flow compiled models on the
  host for both levels; delivered code compiles only inside the toolchain
  image (also resolving the dirfd-ABI flakiness across host/CI glibc
  versions). A later host `strip` residue in corpus builds was likewise moved
  into the image — no host binary tools remain in the corpus artifact path.
- **Corpus shape** — planned 36 slots (3 seeds); shipped 48 with a
  `filewrite` seed, a `files_written` gate, and a `stdin-bytes` hidden domain
  for it. `corpus_build(seed_ids, matrix)` targeting exists with merge and
  prune semantics the plan lacked and briefly descoped.
- **Canonicalizer** — was planned as v1 (ADDR ordinals + argv basename);
  shipped v2.1 with FD and PATH ordinals plus the enforced version stamp.
- **task_open grew a contract surface** — descoped in the original spec
  (calling context, ABI header): shipped with disasm slice, known callees,
  labeled signature guess, and ABI template.
- **status** — descoped to recorded count + ledger in the plan; shipped as
  `status_snapshot` with readiness/coverage/recent journal. The stub
  `replay_pct: 100` still exists on program accepts; a real replay-% metric
  remains undefined and unshipped.
- **Program-path ledger accounting** — implementation initially left
  counters untouched and stacked duplicate accept markers; shipped with
  symmetric counters, idempotent markers, and a `recent` journal.
- **qiling coupling** — the code carries 1.4.6-specific adjustments in
  `exec/recorder.py` and `driver/calling.py` (hook arg shapes, exit-code
  defaults, `QlErrorBase` repr recursion, loader stack API); the dependency
  is therefore bounded `qiling>=1.4.6,<1.5`, and a
  `pillow>=12.3.0` override is forced over `python-fx`'s `pillow<11` cap to
  clear transitive CVEs (the image/TUI paths of python-fx/asciimatics are
  never exercised).
- **Tool table** — five shipped tools match the spec's §5 table in behavior,
  not in every literal parameter name; `compose()` is deliberately not
  exposed.
- **Scope guardrails observed** — x86-64 static ELFs only, ≤6 register
  integer args (no stack args, no structs/floats), no multi-arch, packing, or
  symbolic equivalence; no branch coverage (explicitly cut).

# Verification Audit

This document has been verified line-by-line against the delivered codebase as of 2026-08-02 (commit `ad06b37`).
