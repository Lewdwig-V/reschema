# ReSchema

Reasoning-with-verification harness for reverse engineering, in the spirit of
[Schema](https://arxiv.org/abs/2409.09247)-style world-model construction: an LLM
agent probes an unknown binary through a strictly-gated game engine, building a
growing C "world-model" that must reproduce observed behavior byte-for-byte.

The harness is the judge. The agent never gets to grade its own homework:

## Why

LLM agents are eager reverse engineers and shameless hallucinators: give one a
disassembly and it will confidently narrate behavior it never observed.
ReSchema exists to make every claim **mechanically falsifiable**. The agent can
probe, hypothesize, and submit — but only emulated ground truth can accept.
Wrong answer, loud structured rejection, actionable divergence.

Use it to point an agent at an unknown binary and get out a C reimplementation
you are allowed to believe, or to benchmark how honest an agent is when the
test suite writes back.

- **Level A (whole program)** — traces are recorded under emulation
  ([qiling](https://github.com/qilingframework/qiling)), canonicalized, and
  replayed against the agent's compiled C model: stdout/stderr/exit byte-exact
  plus write-family syscall *shape*. A hidden-test gate replays fresh inputs
  (per-submission unguessable entropy) so hardcoded lookup tables fail.
- **Level B (per function)** — symtab-bounded disassembly slices, a driver that
  calls one emulated function with fuzzed inputs (differential `ret`/`mem`
  comparison against the agent's native `.so` rebuild), and per-TU composition
  of validated functions into a compiled program.

Non-negotiable: negative tests are first-class. Every gate ships with the
attack it must repel (wrong-spec flattery, overfitting, event-splits, void
function differing only in register residue...).

## Corpus

Synthetic seeds with perfect ground truth: `rot13` (argv), `check` (stdin,
password), `calc` (multi-function), `filewrite` (stdin bytes → `out.bin` file) ×
gcc/clang × O0/O1/O2 × sym/stripped = 48 build slots, recorded via a manifest
(addresses + sizes captured pre-strip).

## Trust model

This is adversarial-by-design tooling: the engine compiles agent-authored C
(`gcc`) and then **executes it**. Execution is contained differently per level:

- **Level A (qiling)**: every record runs against a fresh, **empty rootfs**
  holding only a copy of the binary. Qiling emulates syscalls in userspace, so
  any host-mutating file op the guest issues (open/unlink/rename/mkdir/...)
  resolves inside that scratch dir and is discarded with it. Guest file writes
  cannot touch the host fs by construction (`files_written` is scraped from the
  record rootfs — capture and containment are the same mechanism). Static ELF
  guests need nothing else from a rootfs.
- **Level B (differential function fuzzing)**: agent C compiles and executes
  ONLY inside a one-shot rootless **podman** container (`--network none`,
  `--read-only`, 1g memory, 128 pids, fork-per-case). Model segfaults/hangs
  become structured rejects, not harness death.

**Toolchain is pinned, not ambient:** one image
(`localhost/reschema-toolchain:1`, debian trixie + gcc + clang) builds the
*entire* corpus (gcc+clang × opt matrix) and **every** model compile for both
levels. Host `gcc`/`clang`/glibc versions are irrelevant — guest binaries carry
identical toolchain/libc encodings on every machine (this is what keeps qiling
behavior deterministic). Mandatory, no fallback: a missing image hard-fails
with the build command (`podman build -t localhost/reschema-toolchain:1 -f Containerfile .`).

Residuals that are *not* contained: CPU within the emulation timeout, and image
supply-chain (pin your base image digest if that matters to you).

## How to use

```bash
uv sync                                    # python 3.12, qiling 1.4.6, mcp 2.x
uv run python -m reschema.corpus.generate  # builds the 48-slot corpus
uv run pytest -q                           # full suite (includes emulation)
```

Wire the stdio server into any MCP-capable agent (Claude Code, opencode, Codex):

```json
{ "mcpServers": { "reschema": {
    "command": "uv", "args": ["run", "python", "-m", "reschema.mcp.server"],
    "cwd": "/path/to/reschema" } } }
```

Then the agent drives the loop with 5 tools, no harness knowledge required:

1. `corpus_build` → task ids like `rot13::gcc-O2-sym`
2. `task_open` → recorded ground-truth cases (program mode) or disassembly slice
   (function mode via `function=`), plus input mode
3. `experiment` → probe: run a recorded/trace case (or the call driver with an
   agent-declared param spec on a function) and read the canonical trace
4. `submit_model` → hand over C source; the gate compiles, replays recorded +
   hidden inputs (program) or fuzz-compares against the original (function).
   `accepted: false` comes back with a structured divergence to repair
5. `status` → ledger: accepted functions, submission/rejection counts

Accepted functions compose per-TU into a compiled program; a `submit_model` on
a program task is the level-A verdict on reusing them.

## Layout

```
src/reschema/
  corpus/generate.py   # seeds → 48-slot build matrix + manifest
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
