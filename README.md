# ReSchema

[![DeepWiki](https://img.shields.io/badge/DeepWiki-Lewdwig--V%2Freschema-blue.svg?logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==)](https://deepwiki.com/Lewdwig-V/reschema)

> **Status: experimental.** Today ReSchema runs against its own synthetic
> corpus, where ground truth is known by construction. Pointing it at
> arbitrary real-world binaries is the goal.

ReSchema is a reasoning-with-verification harness for reverse engineering.
It is built around world-model construction: an agent should hold an explicit,
checkable model of the world it reasons about. The world is a binary and
the model is executable C. An LLM agent probes an unknown binary through a
strictly-gated game engine, building a C world-model that must
reproduce observed behavior byte-for-byte.

The harness is the judge. The agent does not grade its own work.

## Why

LLM agents are eager reverse engineers and often hallucinate. Give one a
disassembly and it will narrate behavior it never observed.

ReSchema makes every claim mechanically falsifiable. The agent can probe,
hypothesize, and submit, but only emulated ground truth can accept. A wrong
answer comes back as a structured rejection with an actionable
divergence. It is never a traceback or a silent pass.

Use it to point an agent at an unknown binary and get a C reimplementation
that is verified, or to benchmark how honest an agent is when the
test suite responds.

## The two levels

- **Level A (whole program).** Traces are recorded under emulation
  ([qiling](https://github.com/qilingframework/qiling)), canonicalized, and
  replayed against the agent's compiled C model. Comparison is byte-exact on
  stdout, stderr, and exit code. It also checks the write-family syscall shape,
  which is the (fd, byte-count) skeleton of `write` and `writev` calls. A hidden-test gate
  replays fresh inputs drawn with per-submission unguessable entropy to prevent
  hardcoded lookup tables.
- **Level B (per function).** This level evaluates one function at a time. It uses a disassembly slice
  bounded by the ELF symbol table, a driver that calls the original
  function under emulation with fuzzed inputs, and a differential comparison
  of return value and memory effects against the agent's natively-compiled
  `.so`. Accepted functions then compose per translation unit. Each validated
  source is its own TU, linked into one program.

Negative tests are first-class. Every gate ships with the
attack it must repel. These include wrong-spec flattery where a model matches the declared
signature but not the behavior, overfitting where memorized probe answers are caught
by fresh hidden draws, and event-splits where output is chunked into different-sized
syscall writes than the original. It also tests a void model whose only divergence from the
original is garbage left in the return register.

## Corpus

Synthetic seeds with perfect ground truth: 4 seeds × 2 compilers × 3
optimization levels × 2 symbol variants = **48 build slots**, recorded via a
manifest. Function addresses and sizes are captured before stripping.

| seed | input mode | exercises |
| ---- | ---------- | --------- |
| `rot13` | argv | simple transform |
| `check` | stdin text | password check |
| `calc` | argv | multi-function; the level-B showcase |
| `filewrite` | stdin raw bytes | writing a file (`out.bin`) |

Toolchain matrix: gcc and clang at O0, O1, O2, each with symbols (`sym`) and
stripped.

## Measured so far

A scripted agent driving only the 5 MCP tools repairs a wrong level-B `sum_range` model
to acceptance in exactly one rejection round on `calc::gcc-O2-sym`. It gets
all three functions of the `calc` binary accepted on the `-O1` slot with a
clean per-TU compose and link. These results are pinned in CI (`tests/test_dogfood.py`). The transfer benchmark's reference-agent run is
also CI-pinned. No live-agent benchmark numbers exist yet.

## What a rejection looks like

The feedback signal is the primary value. A real `submit_model` verdict:

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

The gate compares byte-exact data. Hex is authoritative and the `*_decoded` fields are
human previews. It reports the first divergence only. Prying the whole
recorded corpus out of the harness is priced in submissions, and the hidden
gate prevents overfitting to the revealed prefix.

## Trust model

This is adversarial-by-design tooling. Agent-authored C must be compiled and
executed in a protected environment. Containment per level:

- **Level A (qiling replay)**: every record runs against a fresh, empty
  rootfs holding only a copy of the binary. Qiling emulates syscalls in
  userspace, so any host-mutating file operation a guest issues resolves inside that scratch directory and is discarded after the run.
  `files_written` is scraped from the same rootfs.
- **Level B (differential function fuzzing)**: agent C compiles and executes
  only inside a one-shot rootless **podman** container. It uses `--network none`,
  `--read-only`, 1g memory, 128 pids, and fork-per-case. Model segfaults or hangs
  become structured rejects.

The toolchain is pinned. One image
(`localhost/reschema-toolchain:1`, debian trixie + gcc + clang) builds the
entire corpus matrix and every model compile for both levels. Guest
binaries carry identical toolchain and libc encodings on every machine. Host
`gcc` is not used. Podman is a dependency for
any build, validate, or test flow. A missing image or podman binary is an
actionable structured refusal.

Python orchestration and the qiling/unicorn emulator run on the host. These are contained by the
scratch rootfs and the per-record timeout, but are trusted third-party
code. The image supply chain is not contained beyond integrity. The base image is a pinned tag and digest in the `Containerfile`. This attests image identity but does not make sandbox
claims about what the image contains.

## Getting started

**Prerequisites:** Linux on x86-64, Python 3.12, [uv](https://docs.astral.sh/uv/), and rootless
[podman](https://podman.io/).

```bash
# step zero: the pinned toolchain image (once per machine/toolchain change)
podman build -t localhost/reschema-toolchain:1 -f Containerfile .
uv sync                                    # python deps: qiling 1.4.6, mcp 2.x
uv run python -m reschema.corpus.generate  # builds the 60-slot corpus
uv run pytest -q                           # full suite (~2 min; emulation is the cost)
```

Skipping the image build is the most common setup failure. The first compile
refuses with a message naming the `podman build` line above.

Wire the stdio server into any MCP-capable agent:

```json
{ "mcpServers": { "reschema": {
    "command": "uv", "args": ["run", "python", "-m", "reschema.mcp.server"],
    "cwd": "/path/to/reschema" } } }
```

Then the agent drives the loop with 5 tools:

1. `corpus_build` → task ids like `rot13::gcc-O2-sym`
2. `task_open` → the task contract plus where this seed takes input
3. `experiment` → probe: run an input against the recorded case and read the canonical trace
4. `submit_model` → hand over C source; the gate compiles and replays recorded and
   hidden inputs. `accepted: false` comes back with a structured divergence to repair
5. `status` → the task ledger showing accepted
   sources, submission counts, and audit seeds

Accepted functions compose per-TU into a compiled program. A `submit_model` on
a program task is the level-A verdict on reusing them.

## Layout

```
src/reschema/
  corpus/generate.py   # seeds → 60-slot build matrix + manifest
  exec/                # qiling recorder, canonicalizer (rules v2.1)
  validate/            # program gate, function gate
  driver/              # call driver (SENTINEL trap), param specs, input gen
  disasm/analyze.py    # symtab-size-exact disasm slices + capstone facts
                       # behind task_open (arity/callees/returns)
  memory.py            # per-family deduction cache (.reschema/memory/<seed>.jsonl)
  engine.py            # TaskStore, ledger, submit_*, compose (single-process)
  mcp/server.py        # thin 1:1 tool wrapper over the engine
tools/dogfood/         # phase-2C live-agent transfer driver (not in the package)
```

(The `SENTINEL` trap is how a level-B call ends under emulation: the driver
writes a planted return address on the stack and stops when RIP lands there,
which is when the original function returns.)

## Documentation

- `ARCHITECTURE.md` - canonical current-state description, including the scope
  guardrails and decision records.
- `docs/benchmark-protocol.md` - the cross-task transfer benchmark (methods;
  CI-pinned reference run, live-agent protocol pending).
- `docs/roadmap.md` - phase order beyond the current milestone.

For agents working on this repo, `AGENTS.md` holds the contributor conventions
(TDD rules, trust model, entropy policy).

## Contributing

MIT licensed. Contributions welcome. Keep the gates strict: every acceptance
path must ship with the negative test that attacks it. Pre-commit hooks
(`pre-commit install`, see `.pre-commit-config.yaml`) run ruff and friends.
```
