# ReSchema — Design Spec

**Date:** 2026-07-29
**Status:** Approved (brainstorming complete), pending user spec review
**Codename:** reschema

## 1. Goal

A reasoning-with-verification harness for reverse engineering & decompilation, in the spirit of "Schema"-style test-driven scaffolding: instead of letting an LLM reason in free text, the harness forces it to maintain its current understanding of a binary as an **executable program**, and strictly rejects any hypothesis that fails to reproduce recorded ground truth. Reasoning becomes code; every claim must compile and beat the transition tests.

The harness wraps two task granularities:

- **Level A (whole binary):** the agent maintains a C world-model that must replay every recorded execution trace of the original binary.
- **Level B (single function):** the agent maintains a C reimplementation of one function that must match the original machine code under differential testing.

Accepted level-B functions compose into the level-A whole-program model. **One growing C artifact is both the agent's reasoning state and the end product.**

### Distant goal (guides interface choices, not scope)

Hand the harness a foreign-architecture binary (e.g., an old game ROM) and obtain a natively compilable, emulation-free recreation. Consequences for v1: the world-model language is C from day one; the execution substrate must be multi-arch capable in principle (qiling/unicorn), even though v1 only exercises x86-64.

## 2. Core principles

1. **The judge is code, not vibes.** The harness is a strict pre-commit hook for reasoning: tools return accept (with metrics) or reject (with a structured diff). The agent cannot self-approve.
2. **Physics experiments.** The agent may run the binary with chosen inputs at will (`experiment`) — hypotheses must survive past *and* fresh experiments.
3. **Hidden-test replay.** Passing the recorded traces is necessary but not sufficient: the harness evaluates the model on freshly generated inputs never shown to the agent, preventing fit-to-trace overfitting.
4. **Python harness, C artifact.** Agent-facing world-models are always C; harness internals are Python (Rust bridge via PyO3/maturin later if profiling demands it).

## 3. Task model A — trace-grounded whole-binary loop

**Task** = (binary, input-space spec, seed).

- **Record (ground truth):** qiling runs the binary once per input case, capturing the trace:
  - argv / stdin digest
  - stdout, stderr (byte-exact)
  - exit code
  - files written (path + content digest)
  - syscall-level event log (name, args, result) — our "recorded state transitions"
- **Trace canonicalization:** before storage and before any diff, a sanitizer in `exec/` normalizes values that legitimately differ between two correct implementations: heap/stack addresses from `brk`/`mmap` → ordinal placeholders (`ADDR_0`, `ADDR_1`, … per allocation site), ephemeral file descriptors → ordinal placeholders, absolute host paths → task-relative paths. Byte-exact comparison applies to the *canonical* trace.
- **Experiment:** agent calls `experiment(task, input)` → recorded trace for that input. This is the probing mechanism for forming hypotheses.
- **Submit:** agent calls `submit_model(task, c_source)`:
  1. Harness compiles the C model (gcc, pinned flags).
  2. Model is replayed under the identical qiling harness on **all** recorded inputs.
  3. Observable effects must match the recorded traces byte-exact. First divergence → **reject** with a structured diff: `{input_case, first_diverging_event_index, expected, actual}`.
  4. Full pass on recorded traces → **hidden-test replay**: harness generates fresh inputs from the task's input-space spec (seeded), replays, requires byte-exact match.
  5. Both pass → accept, report metrics.
- **Metrics:** trace-replay % (recorded), hidden-test pass %, and input-space coverage (how many distinct observable behaviors the corpus of cases exercises, approximated by branch coverage reached by the recorded inputs).
- **Flakiness:** ground-truth recording is run twice per case; any mismatch flags the task as unusable. Corpus seeds are deterministic, so tests in CI are stable.

## 4. Task model B — function-level loop

- **Extraction:** functions come from corpus metadata (synthetic corpus keeps symbols; stripped handling later via qiling/angr recovery — out of v1 scope).
- **Presentation:** `task_open` returns the function's disassembly (objdump/capstone slice) plus calling-context metadata (signature guess, known callees).
- **Experiment (function mode):** the harness builds a tiny driver that loads the *original* binary and invokes the real function inside qiling with agent-chosen args; returns return value + observable memory writes + emitted syscalls.
- **Param spec is part of the hypothesis.** Pointer parameters can't be fuzzed blindly (a `char *buf` with no bounds meta = instant SEGV). With each `submit_model`, the agent declares a param spec — per-parameter kind (`scalar`/`buffer`/`string`/`out-param`), length relationship (`length_param`), direction (`in`/`out`/`in_out`), and range constraints. The harness generates inputs strictly from this spec and checks memory only where the spec grants access. If the agent's spec under-describes the real input space, the (enshrined, seed-varied) fuzz campaign is what catches it — an under-specified signature is simply a hypothesis that fails slower.
- **Submit + differential validation:**
  1. Agent's C function compiles into a shared object (against the ABI-pinned header template provided by `task_open`).
  2. **Split execution paths:** the model runs *natively* via ctypes (fast); only the original binary runs inside qiling. Fuzzing at N≥100 inputs stays practical.
  3. Return values, memory writes inside spec-declared bounds, and emitted syscalls must match for every input; first mismatch → reject with `{input, expected, actual}`.
  4. Pass → **accepted function**, entered into the task ledger.
- **ABI determinism:** corpus seeds and function-mode header templates use explicit-width types (`stdint.h`), documented struct layout (`__attribute__((packed))` where layout matters), and `__attribute__((sysv_abi))`; struct alignment/padding is never left to compiler drift across the matrix slots.
- **Composition:** accepted functions assemble into the whole-program model, which must then pass the level-A gate on the full binary's traces. The composition step is how B feeds A.

## 5. MCP server surface

Thin wrapper over a plain Python validation engine (keeps a standalone/batch driver possible later).

| Tool | Signature | Returns |
|---|---|---|
| `corpus_build` | `(seed_ids, matrix)` → task set | task IDs + manifest |
| `task_open` | `(task_id)` | metadata, disasm slice, input-space spec |
| `experiment` | `(task_id, input)` | recorded trace |
| `submit_model` | `(task_id, c_source)` | accept + metrics, or reject + diff |
| `status` | `(task_id)` | replay %, coverage, accepted functions, hidden-test readiness |

State (traces, accepted functions, ledgers) lives on disk under a `.reschema/` work dir per task — the MCP server is restartable without losing task state.

## 6. Synthetic corpus

- **Seeds:** small deterministic C programs written by hand for the repo: string transforms, crackme-style license checkers, tiny state machines, format parsers. No library randomness; seeded RNG only.
- **Matrix:** {gcc, clang} × {-O0, -O1, -O2} × {stripped, unstripped}.
- **Held-out set:** a few crackmes.one binaries, untouched until the harness is stable — the transfer reality-check that the mechanism isn't overfit to our own generator.

## 7. Repo layout

```
src/reschema/corpus/     seed programs + generator + manifest
src/reschema/exec/       qiling recorder: run + trace capture + trace canonicalizer
src/reschema/driver/     function-mode driver: native ctypes model exec, qiling original exec
src/reschema/validate/   compile model, replay, diff (program + function mode)
src/reschema/disasm/     capstone / pyelftools slicing
src/reschema/mcp/        MCP tool server (thin wrapper)
tests/                   golden traces + negative tests
docs/superpowers/specs/  this spec
.reschema/<task_id>/     runtime task state (traces, ledger) — gitignored
```

## 8. Errors & guardrails

- Compile failure of a submission = reject with compiler output verbatim.
- Run timeout on model replay = reject with the timed-out case.
- Ground-truth double recording mismatch = task flagged flaky/unusable.
- Byte-exact diffs always apply to *canonicalized* traces; sanitizer rules are versioned in the repo (a rule change = corpus re-record).
- Function-mode models execute natively as a throughput decision, **contained**: agent C compiles and runs only inside one-shot rootless podman containers (spec 2026-08-02-level-b-containment); missing container runtime hard-fails (no native fallback). Corpus and both levels' model compiles all run inside the one pinned toolchain image — ambient host toolchains are out of the trust surface.
- v1 scope: x86-64 ELF produced by our own compile matrix. Dynamic-library resolution beyond libc, anti-emulation tricks, packing, multi-process targets, and symbolic (angr-style) equivalence are **explicit non-goals for v1**.

## 9. Testing

- Golden traces: hand-written C seeds with known behavior → recordings snapshot-tested.
- **Negative tests are first-class:** intentionally-wrong models must be rejected; a model that hardcodes expected outputs must fail hidden-test replay.
- Dogfood loop: the harness is driven through its own MCP interface in an integration test.

## 10. Stack

- Python 3.12+, `uv` for env/build, pytest.
- `qiling` (unicorn) — execution + trace capture.
- `capstone`, `pyelftools` — disassembly / ELF slicing.
- `gcc` + `clang` (system) — corpus matrix + model compilation.
- stdlib `ctypes` — native execution of function-mode models (no new dependency).
- Official MCP Python SDK — tool server.
- Later (deferred): Rust bridge via PyO3/maturin; angr symbolic checks; multi-arch corpus (arm/mips) via qiling.

## 11. MVP success criteria

1. Agent (Claude Code via MCP) completes a level-B task on an `-O2` corpus function: proposal → experiments → accepted after at least one rejection-then-repair cycle.
2. Accepted functions for one whole seed binary compose into a model passing the level-A hidden-test replay.
3. Harness proves its strictness in CI: all negative tests reject.

## 12. Explicit non-goals (v1)

Packed/protected binaries, anti-emulation countermeasures, multi-arch beyond x86-64, symbolic equivalence engines, performance optimization, pretty CLI/TUI, multi-agent orchestration.

## Descoped for v1

- Program-mode input-space spec field — input space stays implicit in the hidden-stream generator.
- `status` replay-%/coverage/readiness fields — v1 status reports recorded-case count + ledger only.
- `corpus_build(seed_ids, matrix)` signature — v1 builds the full fixed seed set, no filtering.
- §5 tool-table exact signatures — the shipped 5 tools match in behavior, not in every literal parameter name; see `mcp/server.py`.
