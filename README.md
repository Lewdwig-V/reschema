# ReSchema

[![DeepWiki](https://img.shields.io/badge/DeepWiki-Lewdwig--V%2Freschema-blue.svg?logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==)](https://deepwiki.com/Lewdwig-V/reschema)

> **Status: experimental.** Today ReSchema runs against its own synthetic
> corpus, where ground truth is known by construction. Pointing it at
> arbitrary real-world binaries is the goal.

ReSchema is a reasoning-with-verification harness for reverse engineering.
It is built around world-model construction: an agent should hold an explicit,
checkable model of the world it reasons about. The world is a binary and
the model is executable C. An LLM agent probes an unknown binary through
host-mediated experiments, building a C world-model that must reproduce
observed behavior byte-for-byte. ReSchema is a domain on
[Warranted](https://github.com/Lewdwig-V/warranted), which owns task state,
the ledger and accounting, containment and memory; ReSchema keeps the reverse
engineering: corpus, ground truth, canonicalisation and the two checkers.

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
  `.so`. Each function is its own task; an accepted function becomes a
  verified fact that the family's later tasks (its program task included)
  see in their memory.

Negative tests are first-class. Every gate ships with the
attack it must repel. These include wrong-spec flattery where a model matches the declared
signature but not the behavior, overfitting where memorized probe answers are caught
by fresh hidden draws, and event-splits where output is chunked into different-sized
syscall writes than the original. It also tests a void model whose only divergence from the
original is garbage left in the return register.

## Corpus

Synthetic seeds with perfect ground truth: 5 seeds × 2 compilers × 3
optimization levels × 2 symbol variants = **60 build slots**, recorded via a
manifest. Function addresses and sizes are captured before stripping.

| seed | input mode | exercises |
| ---- | ---------- | --------- |
| `rot13` | argv | simple transform |
| `check` | stdin text | password check |
| `calc` | argv | multi-function; the level-B showcase |
| `filewrite` | stdin raw bytes | writing a file (`out.bin`) |
| `pkfmt` | stdin raw bytes | TLV packet parser (magic, version bounds, checksum) |

Toolchain matrix: gcc and clang at O0, O1, O2, each with symbols (`sym`) and
stripped.

## Measured so far

A scripted model drives one slot (`rot13::gcc-O2-sym`: a function task, then
the program task) through the `warranted` CLI to two acceptances, with the
function's verified fact reaching the program task and full per-run
accounting (`tests/test_cli_campaign.py`, golden). No live-agent benchmark
numbers exist yet.

## What a rejection looks like

The feedback signal is the primary value. A real `program-replay` verdict:

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

This is adversarial-by-design tooling. Agent-authored C is compiled and run
only inside Warranted jobs: a fresh rootless **podman** container per job
with no network, a read-only root, private `/work` and `/tmp` holding only
the job's files, UID 1000 with no capabilities, and memory and pid limits.
A job's output is the model's claim about itself; the checker judges it on
the host against ground truth the job never sees.

- **Level A (program replay)**: ground truth is the corpus binary
  double-recorded under qiling on the host, each record against a fresh,
  empty rootfs. The model is compiled and replayed under qiling inside one
  job (decision D1 in `docs/proposals/m8-rebuild-on-warranted.md`).
- **Level B (differential function fuzzing)**: the original function runs
  under qiling on the host; the model compiles to a `.so` and is called
  fork-per-case inside a job. Model segfaults or hangs become structured
  rejects.

The worker itself runs in Warranted's sandbox from the same image and never
holds the corpus binary: it probes it through the host's `experiment`
operations.

The toolchain is pinned. One image (`localhost/reschema-toolchain:1`, debian
trixie + gcc + clang + the pinned qiling + the worker helpers) builds the
corpus matrix, runs every checker job and hosts the worker, pinned by its
image ID. Host `gcc` is not used. Podman is a dependency for any build,
check, or test flow; a missing image or podman binary is a refusal naming
the build command.

Python orchestration and the qiling/unicorn emulator run on the host for
ground truth. These are contained by the scratch rootfs and the per-record
timeout, but are trusted third-party code. The image supply chain is not
contained beyond integrity: the base image is a pinned tag and digest in the
`Containerfile`.

## Getting started

**Prerequisites:** Linux on x86-64, Python 3.12, [uv](https://docs.astral.sh/uv/), and rootless
[podman](https://podman.io/).

```bash
# step zero: the pinned toolchain image (once per machine/toolchain change):
# pulls CI's published build from GHCR, else runs the podman build below
tools/toolchain_image.sh   # fallback: podman build -t localhost/reschema-toolchain:1 -f Containerfile .
uv sync                                    # python deps: qiling 1.4.6, warranted v0.4.0
uv run python -m reschema.corpus.generate  # builds the 60-slot corpus
uv run pytest -q -n auto                   # default suite (~45 s)
```

Skipping the image build is the most common setup failure. The first compile
refuses with a message naming the `podman build` line above.

An agent works a slot as Warranted tasks: one per function, then one for the
whole program (`reschema.tasks`).

### Live campaign

`python -m reschema.campaign write` lays out a campaign in a new or empty
`--out` directory for corpus slots (each
slot's function tasks, then its program task) after a full corpus rebuild
(about 10s; it replaces the manifest and binaries, never a past project's
pinned task files), and prints the commands below with the project's `--allow`
flags filled in. Run configs live in `campaigns/` (local Qwen on Ollama, GLM
on OpenRouter via litellm; `campaigns/README.md` has the smoke slots and the
key file). Keep campaigns, projects and keys under `results/`, which is
gitignored:

```bash
uv run python -m reschema.campaign write --out results/smoke-qwen \
    --config campaigns/run-qwen-local.toml \
    rot13::gcc-O2-sym check::gcc-O1-stripped calc::gcc-O2-sym
uv run warranted init results/smoke-qwen/project \
    --domain reschema.domain:ReSchemaDomain \
    --allow check=80 --allow model=3200 --allow probe=2560 --allow tool=3200
uv run warranted campaign run results/smoke-qwen/project results/smoke-qwen/campaign.toml
uv run warranted campaign report results/smoke-qwen/project smoke-qwen
uv run python -m reschema.regrade results/smoke-qwen/project  # re-judge under today's checkers
```

Each task's `task.md` is the worker's instructions. In the sandbox the worker
probes with `experiment` (a whole-program run) and `experiment-function` (one
call of the original function), and hands over its model with `submit`; a
rejection comes back as structured feedback to repair.

## Layout

```
src/reschema/
  corpus/generate.py   # seeds → 60-slot build matrix + manifest
  exec/                # qiling recorder, canonicalizer (rules v2.1)
  validate/            # program gate (hidden suite, compare), function gate
  driver/              # call driver (SENTINEL trap), param specs, native worker
  disasm/analyze.py    # symtab-size-exact disasm slices + capstone facts
                       # behind the function task inputs (arity/callees/returns)
  checks/              # Warranted checkers: function-fuzz, program-replay
  operations.py        # experiment / experiment_function host operations
  tasks.py             # TaskSpecs per slot, task.md, campaign files
  campaign.py          # `python -m reschema.campaign write`: a live campaign
  domain.py            # ReSchemaDomain: checkers, operations, guard fingerprint
  regrade.py           # re-run current checkers over recorded check evidence
  toolchain.py         # the pinned image's ID; the corpus build's container run
tools/worker/          # experiment / experiment-function / submit (in the image)
campaigns/             # live run configs (Ollama, OpenRouter), smoke slots
```

(The `SENTINEL` trap is how a level-B call ends under emulation: the driver
writes a planted return address on the stack and stops when RIP lands there,
which is when the original function returns.)

## Related work

Agentic RE tooling, such as the MCP servers for IDA, Ghidra and Binary Ninja,
gives agents context, but none of it checks the agent's conclusions. Neural
decompilation work mostly judges output by recompilation or by fixed, shipped
test suites, and 2026 results show both can be passed while behavior
diverges. ReSchema's distinguishing feature is an **in-loop judge** with
emulated ground truth. Every program-mode submission is replayed against
fresh hidden inputs, and every function-mode submission is fuzzed against the
original on a fresh draw that the agent cannot pin. Agent C is compiled and
run only in contained jobs: natively for level B, under qiling for level A
(see "Trust model" above). The nearest published neighbors
are Decompile-Diverge (arXiv 2609.05370), which fuzzes from the original
binary to find divergences that test suites miss, and "Recompilation Is Not
Enough" (arXiv 2609.07201), which uses an exact-output gate on fixed tests. See
`docs/research/2026-10-agentic-re-harnesses/` for the survey; its figures are
largely author-reported preprints.

## Documentation

- `ARCHITECTURE.md` - canonical current-state description, including the scope
  guardrails and decision records.
- `docs/benchmark-protocol.md` - the cross-task transfer benchmark (methods;
  its pre-M8 reference runs went with the old engine, a new metric epoch
  starts on Warranted accounting).
- `docs/proposals/m8-rebuild-on-warranted.md` - the M8 port onto Warranted
  and its decisions (D1-D3).
- `docs/roadmap.md` - phase order beyond the current milestone.
- `docs/rejected-ideas.md` - ideas deliberately refused, with reasons and
  reopen conditions.
- `docs/research/` - research surveys behind the roadmap backlog (source
  material, with errata).
- [Long-horizon reasoning harness proposal](docs/proposals/long-horizon-reasoning-harness.md)
  - a proposed Lean-backed generalisation, with a separate implementation repository
    recommended; design discussion only.

For agents working on this repo, `AGENTS.md` holds the contributor conventions
(TDD rules, trust model, entropy policy).

## Contributing

MIT licensed. Contributions welcome. Keep the gates strict: every acceptance
path must ship with the negative test that attacks it. Pre-commit hooks
(`pre-commit install`, see `.pre-commit-config.yaml`) run ruff and friends.
```
