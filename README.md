# ReSchema

> **Status: experimental.** Today ReSchema runs against its own synthetic
> corpus, where ground truth is known by construction. Pointing it at
> arbitrary real-world binaries is the goal, not the current state.

ReSchema is a reasoning-with-verification harness for reverse engineering,
built around world-model construction: an agent should hold an explicit,
checkable model of the world it reasons about. Here the world is a binary and
the model is executable C. An LLM agent probes an unknown binary through a
strictly-gated game engine, building a growing C "world-model" that must
reproduce observed behavior byte-for-byte.

The harness is the judge. The agent never gets to grade its own homework.

## Why

LLM agents are eager reverse engineers and shameless hallucinators: give one a
disassembly and it will confidently narrate behavior it never observed.

ReSchema makes every claim **mechanically falsifiable**. The agent can probe,
hypothesize, and submit — but only emulated ground truth can accept. A wrong
answer comes back as a loud, structured rejection with an actionable
divergence — never a traceback, never a silent pass.

Use it to point an agent at an unknown binary and get out a C reimplementation
you are allowed to believe, or to benchmark how honest an agent is when the
test suite writes back.

## The two levels

- **Level A (whole program).** Traces are recorded under emulation
  ([qiling](https://github.com/qilingframework/qiling)), canonicalized, and
  replayed against the agent's compiled C model. Comparison is byte-exact on
  stdout/stderr/exit code, plus the write-family syscall *shape* — the
  (fd, byte-count) skeleton of `write`/`writev` calls. A hidden-test gate
  replays fresh inputs drawn with per-submission unguessable entropy, so
  hardcoded lookup tables fail.
- **Level B (per function).** One function at a time: a disassembly slice
  (bounded by the ELF symbol table), a driver that calls the original
  function under emulation with fuzzed inputs, and a differential comparison
  of return value and memory effects against the agent's natively-compiled
  `.so`. Accepted functions then compose per translation unit (each validated
  source is its own TU, linked into one program).

Non-negotiable: **negative tests are first-class.** Every gate ships with the
attack it must repel — wrong-spec flattery (a model matching the declared
signature but not the behavior), overfitting (memorized probe answers, caught
by fresh hidden draws), event-splits (chunking output into different-sized
syscall writes than the original), a void model whose only divergence from the
original is garbage left in the return register, and more.

## Corpus

Synthetic seeds with perfect ground truth: 4 seeds × 2 compilers × 3
optimization levels × 2 symbol variants = **48 build slots**, recorded via a
manifest (function addresses and sizes captured before stripping).

| seed | input mode | exercises |
| ---- | ---------- | --------- |
| `rot13` | argv | simple transform |
| `check` | stdin text | password check |
| `calc` | argv | multi-function; the level-B showcase |
| `filewrite` | stdin raw bytes | writing a file (`out.bin`) |

Toolchain matrix: gcc and clang at O0, O1, O2, each with symbols (`sym`) and
stripped.

## Measured so far

Anecdotal but real, and pinned in CI (`tests/test_dogfood.py`): a scripted
agent driving only the 5 MCP tools repairs a wrong level-B `sum_range` model
to acceptance in exactly one rejection round on `calc::gcc-O2-sym`, and gets
all three functions of the `calc` binary accepted on the `-O1` slot with a
clean per-TU compose+link. The transfer benchmark's reference-agent run is
also CI-pinned (see `docs/benchmark-protocol.md`); **no live-agent benchmark
numbers exist yet.**

## What a rejection looks like

The value proposition is the feedback signal. A real `submit_model` verdict:

```json
{
  "accepted": false,
  "reason": "io-mismatch",
  "stage": "recorded",
  "divergence": {
    "argv": ["hello"],
    "expected": {
      "stdout": "75727979620a",
      "stderr": "",
      "exit_code": 0,
      "stdout_decoded": "uryyb\n",
      "stderr_decoded": ""
    },
    "actual": {
      "stdout": "68656c6c6f0a",
      "stderr": "",
      "exit_code": 0,
      "stdout_decoded": "hello\n",
      "stderr_decoded": ""
    }
  }
}
```

The gate compares byte-exact (hex is authoritative; the `*_decoded` fields are
human previews), and reports the **first** divergence only — prying the whole
recorded corpus out of the harness is priced in submissions, and the hidden
gate backstops overfitting to the revealed prefix.

## Trust model

This is adversarial-by-design tooling: agent-authored C must be compiled and
executed *somewhere* — never on the unprotected host. Containment per level:

- **Level A (qiling replay)**: every record runs against a fresh, **empty
  rootfs** holding only a copy of the binary. Qiling emulates syscalls in
  userspace, so any host-mutating file op a guest issues (open/unlink/rename/
  mkdir/...) resolves inside that scratch dir, discarded after the run.
  (`files_written` is scraped from the same rootfs — capture and containment
  are one mechanism.)
- **Level B (differential function fuzzing)**: agent C compiles and executes
  ONLY inside a one-shot rootless **podman** container (`--network none`,
  `--read-only`, 1g memory, 128 pids, fork-per-case). Model segfaults/hangs
  become structured rejects, not harness death.

**The toolchain is pinned, not ambient.** One image
(`localhost/reschema-toolchain:1`, debian trixie + gcc + clang) builds the
entire corpus matrix and every model compile for both levels, so guest
binaries carry identical toolchain/libc encodings on every machine. Host
`gcc` is never in the picture. Podman is therefore a **hard dependency for
any build, validate, or test flow** — a missing image or podman binary is an
actionable structured refusal, never a silent fallback.

What still runs on the host: python orchestration (engine, ledger,
canonicalization) and the qiling/unicorn emulator itself — contained by the
scratch rootfs and the per-record timeout, but ultimately trusted third-party
code like any other dependency. Also not contained: image supply chain (pin
your base-image digest if that matters to you).

## Getting started

**Prerequisites:** Linux on x86-64 (the emulation/containment stack assumes
it), Python 3.12, [uv](https://docs.astral.sh/uv/), and rootless
[podman](https://podman.io/).

```bash
# step zero: the pinned toolchain image (once per machine/toolchain change)
podman build -t localhost/reschema-toolchain:1 -f Containerfile .
uv sync                                    # python deps: qiling 1.4.6, mcp 2.x
uv run python -m reschema.corpus.generate  # builds the 48-slot corpus
uv run pytest -q                           # full suite (~2 min; emulation is the cost)
```

Skipping the image build is the number-one setup failure: the first compile
refuses with a message naming the `podman build` line above.

Wire the stdio server into any MCP-capable agent (Claude Code, opencode, Codex):

```json
{ "mcpServers": { "reschema": {
    "command": "uv", "args": ["run", "python", "-m", "reschema.mcp.server"],
    "cwd": "/path/to/reschema" } } }
```

Then the agent drives the loop with 5 tools, no harness knowledge required:

1. `corpus_build` → task ids like `rot13::gcc-O2-sym`
2. `task_open` → the task contract (program mode, or a function's disassembly
   slice via `function=`), plus where this seed takes input (argv vs stdin)
3. `experiment` → probe: run an input against the recorded/trace case (or call
   one function with an agent-declared param spec) and read the canonical trace
4. `submit_model` → hand over C source; the gate compiles, replays recorded +
   hidden inputs (program) or fuzz-compares against the original (function).
   `accepted: false` comes back with a structured divergence to repair
5. `status` → the task ledger (the persistent per-task record of accepted
   sources, submission/rejection counts, audit seeds)

Accepted functions compose per-TU into a compiled program; a `submit_model` on
a program task is the level-A verdict on reusing them.

## Layout

```
src/reschema/
  corpus/generate.py   # seeds → 48-slot build matrix + manifest
  exec/                # qiling recorder, canonicalizer (rules v2.1)
  validate/            # program gate, function gate
  driver/              # call driver (SENTINEL trap), param specs, input gen
  disasm/slice.py      # symtab-size-exact disassembly slices
  disasm/analyze.py    # capstone facts behind task_open (arity/callees/returns)
  memory.py            # per-family deduction cache (.reschema/memory/<seed>.jsonl)
  engine.py            # TaskStore, ledger, submit_*, compose (single-process)
  mcp/server.py        # thin 1:1 tool wrapper over the engine
```

(The `SENTINEL` trap is how a level-B call ends under emulation: the driver
writes a planted return address on the stack and stops when RIP lands there —
exactly when the original function returns.)

## Documentation

- `ARCHITECTURE.md` — canonical current-state description, including the scope
  guardrails and decision records.
- `docs/benchmark-protocol.md` — the cross-task transfer benchmark (methods;
  CI-pinned reference run, live-agent protocol pending).
- `docs/roadmap.md` — aspirational phase order beyond the current milestone.

For agents working on this repo, `AGENTS.md` holds the contributor conventions
(TDD rules, trust model, entropy policy).

## Contributing

MIT licensed. Contributions welcome — keep the gates strict: every acceptance
path must ship with the negative test that attacks it. Pre-commit hooks
(`pre-commit install`, see `.pre-commit-config.yaml`) run ruff and friends.
