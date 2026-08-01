# ReSchema

Reasoning-with-verification harness for reverse engineering, in the spirit of
[Schema](https://arxiv.org/abs/2409.09247)-style world-model construction: an LLM
agent probes an unknown binary through a strictly-gated game engine, building a
growing C "world-model" that must reproduce observed behavior byte-for-byte.

The harness is the judge. The agent never gets to grade its own homework:

- **Level A (whole program)** — traces are recorded under emulation
  ([qiling](https://github.com/qilingframework/qiling)), canonicalized, and
  replayed against the agent's compiled C model: stdout/stderr/exit byte-exact
  plus write-family syscall *shape*. A hidden-test gate replays fresh inputs
  (per-submission unguessable entropy) so hardcoded lookup tables fail.
- **Level B (per function)** — symtab-bounded disassembly slices, a driver that
  calls one emulated function with fuzzed inputs (differential `ret`/`mem`
  comparison against the agent's native `.so` rebuild), and per-TU composition
  of validated functions into a compiled program.

Chef's kiss requirement: negative tests are first-class. Every gate ships with
the attack it must repel (wrong-spec flattery, overfitting, event-splits, void
function differing only in register residue...).

## Corpus

Synthetic seeds with perfect ground truth: `rot13` (argv), `check` (stdin,
password), `calc` (multi-function) × gcc/clang × O0/O1/O2 × sym/stripped = 36
build slots, recorded via a manifest (addresses + sizes captured pre-strip).

## Quickstart

```bash
uv sync                                # python 3.12, qiling 1.4.6, mcp 2.x
uv run python -m reschema.corpus.generate   # builds the 36-slot corpus
uv run pytest -q                       # 95 tests (includes qiling emulation)
```

Point an MCP client at the stdio server:

```bash
uv run python -m reschema.mcp.server   # tools: corpus_build, task_open,
                                       #   experiment, submit_model, status
```

## Trust model

This is adversarial-by-design tooling: the engine compiles agent-authored C
(`gcc`) and then **executes it** — natively via `ctypes` (level B) and under
qiling with rootfs `/` (level A), where guest file syscalls pass through to host
paths as the harness user. That is deliberate (the harness must observe real
behavior); run submissions you don't trust inside a container or VM.

## Layout

```
src/reschema/
  corpus/generate.py   # seeds → 36-slot build matrix + manifest
  exec/                # qiling recorder, canonicalizer (rules v1)
  validate/            # program gate, function gate
  driver/              # call driver (SENTINEL trap), param specs, input gen
  disasm/slice.py      # symtab-size-exact disassembly slices
  engine.py            # TaskStore, ledger, submit_*, compose (single-process)
  mcp/server.py        # thin 1:1 tool wrapper over the engine
```

Design doc: `docs/superpowers/specs/2026-07-29-reschema-design.md` (includes a
"Descoped for v1" section). Implementation plan: `docs/superpowers/plans/`.

MIT licensed. Contributions welcome — keep the gates strict.
