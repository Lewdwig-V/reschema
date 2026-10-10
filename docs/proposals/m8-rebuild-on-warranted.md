# Proposal: rebuild ReSchema on Warranted (M8)

**Status:** proposed; needs review of the three decisions below before any code moves.
**Date:** 2026-10-10.
**Milestone:** [Warranted M8](https://github.com/Lewdwig-V/warranted/blob/main/docs/roadmap.md#m8--reschema-rebuilt-on-warranted).

## Goal

ReSchema depends on a pinned Warranted release and keeps only the
reverse-engineering task: corpus generation, ground-truth recording,
canonicalisation, the replay and differential-fuzz checkers, disassembly facts,
and task presentation. Warranted owns task state, the ledger and accounting,
gate enforcement and private checker data, contained execution, and verified
versus unverified memory.

**Completion evidence** (from the Warranted roadmap): ReSchema's gate regression
tests pass against the Warranted-backed implementation, with the same accept and
reject decisions on recorded cases. The isolation regressions, including the
scratch-mount and host-write cases, still hold. A live-agent campaign completes
through the CLI with full accounting.

**Rule** (also from the roadmap): anything that only makes sense for reverse
engineering stays in ReSchema. If an M7 interface does not fit, revise the
interface in Warranted before 1.0, rather than adding a ReSchema-specific path.

## What moves where

| ReSchema today | On Warranted |
| --- | --- |
| `engine.TaskStore`, `ledger.json` counters, journal, `rejected_sources` | Project ledger, run accounting, submission records (`Project`, `RunStatus`) |
| `audit` seeds, binary digest, toolchain ID (#146) | Check evidence: `draw_seed` records, `jobs.json` (image ID, input digests) |
| `submit_program` / `program_gate` | `ProgramReplay` checker |
| `submit_function` / `validate_function` | `FunctionFuzz` checker |
| `driver/podrun.py` | `CheckContext.run_job` with `JobLimits` |
| `experiment` MCP tool | `experiment` host-mediated `Operation`, charged in `probe` units |
| Flail guard (`_flail_verdict`, `DUP_*`) | `DuplicateGuard(exact_repeats=2, near_repeats=3, near_edit_floor=24, near_edit_percent=6, window=8)` with `_norm_source` as the normalisation |
| `memory.py` family cache | `MemorySpec(scope=<seed>)`: verdict facts on accept, promoted `notes.json` |
| MCP server, `task_open` payloads, tool docstrings | `task.md` and task inputs in the workspace; a ReSchema worker image |
| `tools/dogfood/` opencode driver and campaigns | `CampaignSpec` over `TaskSpec`s, Warranted model adapters |
| `regrade.py` | Re-runs a checker over recorded check evidence (rebuilt last) |

Stays in ReSchema unchanged: `corpus/`, `exec/` (recorder and canonicaliser),
`disasm/`, `driver/calling.py` and `driver/spec.py`, `validate/scout.py`, the
hidden-input grammars, and the divergence and repair payloads (as checker
feedback).

## Fit findings

These are the points where today's ReSchema does not map one to one. Three need
a decision (D1–D3); the rest have a proposed answer.

### D1. Level A runs the agent's model under qiling in the host process

`validate/program.replay_against` compiles the model in the toolchain
container, then records it with qiling **on the host**. That is ReSchema's
documented level-A trust model, but Warranted's checker contract is stricter:
checkers are trusted host code, and untrusted code runs only through `run_job`.
Qiling is an emulator, not a security boundary.

**Proposal:** run the model's replay in a job. Add a checker image (the toolchain
image plus pinned qiling and the recorder) and send the model binary, the
canonicaliser and the case list as job files. Ground truth for the corpus binary
is ReSchema's own build, so it keeps recording on the host as it does now.
Cost: one container per submission for the model replay, plus the qiling
start-up inside it.

*Alternative:* keep host-side qiling and record it as a deliberate exception in
Warranted's enforcement doc. That weakens an invariant M8 exists to inherit, so
I don't recommend it.

### D2. Function accepts are building blocks; a Warranted task has one acceptance

A ReSchema slot accepts functions one by one and completes only when the program
model is accepted (`task_complete`, #103). In Warranted a submission runs all of
the task's required checks, and an accepted submission ends the run. A single
task cannot accept a function and keep going.

**Proposal:** one `TaskSpec` per function (`<slot>::fn::<name>`, check
`function-fuzz`) and one per program (`<slot>::program`, check `program-replay`),
all sharing the seed's memory scope. An accepted function records a verified
fact holding its params and source. Later tasks of the same family, including
that slot's program task, see it in `context/`. A campaign orders a slot's
function tasks before its program task. Program acceptance remains the only
"task complete"; function acceptance becomes a completed task of its own kind,
so the #103 false-completion class cannot arise.

*Alternative:* make function checking a host operation (`check_function`, charged
in its own unit) inside the program task. That keeps today's single-task
interaction, but operations cannot write verified memory, so function facts would
reach other tasks only as unverified notes. It would also need a new Warranted
interface: operation-backed facts.

### D3. The recorded stage replays the agent's experiments

Today `submit_program` replays every trace the agent recorded with `experiment`,
then the hidden suite. In Warranted a checker sees the candidate, the task
inputs and private files, but not the run's operation evidence, and M7's
contract is that anything the worker recorded stays a hint.

**Proposal:** the candidate nominates its recorded cases (`cases.json`: argv and
stdin, at most 16, like the mystery fixture). The checker double-records each
case on the corpus binary itself, then replays the model on them, then on
`HIDDEN_N` hidden cases drawn from `draw_seed`. `experiment` remains how the
agent learns, and its traces are host-observed evidence, but the gate no longer
depends on them. This also removes the re-grade's "recorded cases changed"
failure mode, because the recorded set is part of the candidate.

Cost: ground truth for nominated cases is recorded at every submission, not
once per experiment. If that proves slow, the fix is a checker-side cache keyed
by binary digest and case. That is ReSchema code, not a Warranted change.

### Proposed answers to the remaining gaps

- **Jobs take flat files and no mounts.** `podrun` mounts `src/` read-only and
  imports `reschema.driver.native_worker`. Under `run_job`, the job receives
  `native_worker.py` and `spec.py` as files, and the worker imports `spec`
  locally. The job wrapper runs `python`, which Debian's image does not provide:
  add `python-is-python3` to the `Containerfile` (a new image tag through the
  #154 GHCR flow).
- **Image pinning.** `run_job` requires a digest or a local image ID, which is
  exactly the #146 pin. Warranted's `jobs.json` records the image per job, so
  the audit `toolchain` field becomes redundant once the port lands.
- **Seeds.** `draw_seed()` returns a 64-bit int; today's program seeds are
  128-bit hex strings (`secrets.token_hex(16)`). The hidden grammars take a
  string, so pass `str(seed)`. Entropy per draw drops from 128 to 64 bits, which
  is still far beyond what a worker can search.
- **Limits.** Feedback is capped at 64 KiB (`FEEDBACK_LIMIT`), and an oversized
  payload is a checker fault, so cap divergence payloads (dependency slices, event
  windows) explicitly. Facts are capped at 16 per verdict and 16 KiB in total
  (`FACTS_LIMIT`), so a large accepted program source may not fit: store the
  source in the fact when it fits, otherwise its digest. Job output is capped at
  256 KiB per stream, which is ample for 4×`N_FUZZ` function cases.
- **Memory applicability.** `MemorySpec.depends` names task files whose bytes
  define the family. The corpus binaries differ per slot, so they cannot be the
  dependency. Add a small `family.json` input (seed name, function names,
  canonicaliser version), so a canonicaliser bump marks old facts stale. This
  replaces the rule that `CANONICALIZER_VERSION` changes force a corpus
  re-record, for memory purposes.
- **Efficiency metric.** E is computed from Warranted accounting: probes become
  `probe` operation units, submissions are Warranted submissions. The θ_E vector
  goes into the domain version, which is the natural home for #114's
  `METRIC_EPOCH`.
- **Agent surface.** The five MCP tools become workspace files and commands:
  `task.md` (today's tool docstrings, rewritten as instructions), the disassembly
  slice, `abi_template` and signature guess as task inputs, and
  `experiment`/`submit` helpers in the worker image. The worker image is the
  toolchain image plus those helpers, published through the same GHCR flow.
- **Live runs.** Warranted's adapters (local endpoint, OpenRouter, litellm)
  replace opencode, and `RunConfig.max_steps` replaces the dogfood step budget.
  2C results are not comparable across this change: the port starts a new metric
  epoch.

## Parity before porting

The completion evidence is "the same accept and reject decisions on recorded
cases". So the first slice captures those decisions from the *current* gates:
a golden fixture of (slot, unit, candidate source, params, cases, seed) → verdict
and stage, covering every accept path and negative test in `test_engine*.py`,
`test_validate_*.py`, `test_hidden.py` and `test_regrade.py`. Each later slice
replays the fixture through the new checkers. A decision that differs is a bug
unless D3 explains it (nominated cases instead of all experiments), in which
case the fixture records both.

To keep that target fixed, gate changes are frozen during the port: #109-A/C
(#120, #122), and anything else touching `validate/`. So is work on the parts
being replaced: #113 and #114 (re-land on Warranted's ledger), #128 and #129
(the agent runner), and #88 (MCP prompts).

## Slices

Each slice is one PR that keeps CI green. Old and new paths coexist until S7.

| # | Slice | Depends on |
| --- | --- | --- |
| S0 | Warranted v0.3.0 tagged ([warranted#75](https://github.com/Lewdwig-V/warranted/pull/75)) | — |
| S1 | Golden decision fixture from the current gates | — |
| S2 | Pin `warranted @ git+…@v0.3.0`; `Containerfile` gains `python-is-python3` (and the checker image, if D1 is accepted); `reschema.domain` skeleton with `name`, `version`, `worker_image`, `sources` | S0 |
| S3 | `FunctionFuzz` checker over `run_job`, passing the golden function cases | S1, S2 |
| S4 | `ProgramReplay` checker (D1, D3), passing the golden program cases | S1, S2 |
| S5 | `experiment` operation; `TaskSpec` builders per slot (D2), memory, guard; mystery-style tests for leaked private inputs, forged verdicts and bypassed budgets | S3, S4 |
| S6 | Worker image, `task.md`, `experiment`/`submit` helpers; a scripted-model campaign through the CLI | S5 |
| S7 | Remove `engine` ledger code, `podrun`, the MCP server and `tools/dogfood`; port `regrade` onto check evidence | S6 |
| S8 | Live-agent campaign through the CLI with full accounting (M8 completion) | S7 |

Any Warranted interface change found on the way (for example operation-backed
facts, if D2's alternative wins) lands in Warranted first and ReSchema pins the
next tag.

## Decisions needed

1. **D1:** move the model's qiling replay into a job (recommended), or record
   host-side emulation as an exception?
2. **D2:** one task per function plus one per program (recommended), or function
   checks as an operation inside one program task?
3. **D3:** the recorded stage replays worker-nominated cases re-recorded by the
   checker (recommended), or keep replaying all experiments, which needs a new
   Warranted interface to give checkers operation evidence?
