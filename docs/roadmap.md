# ReSchema — Aspirational Roadmap

Living document. Records the intended phase order after the current issue PRs
land; **phases 2+ are deliberately vague** — each gets brainstormed → specced →
planned → broken into issues only when it becomes the active phase. A phase's
prerequisite is that the one before it holds.

## Architecture direction: two agents, not one monolith

Steering note (2026-08, from external review): solve **decompilation for the
foreign architecture** and **refactoring for the current architecture** as
separate agents, not one stitched pipeline.

- **Reverse-engineering agent** — what the current harness trains and judges:
  foreign-arch binary → behavior-faithful world-model C (canonical traces,
  hidden tests, differential fuzz as its truth source).
- **Refactoring agent** — takes a verified-faithful world-model and rewrites
  it into idiomatic, current-arch C (structure, naming, idiom, perf), with
  correctness re-verified against the same judge. Composition of the two is the
  product; conflating them inside one model/solver is the failure mode.

Phases 2+ shape the judge/coaching toward the RE agent's solve rate; the
refactoring agent rides on the same verified substrate once it exists.

## Phase 1.5 — Engine Hardening & Spec Alignment

Close the MVP-sweep issues (raised 2026-08-01, ISSUE-01 … ISSUE-10, P0–P2):

- **Level-B podman containment** (ISSUE-11 — mandatory worker, no native exec)
- Canonicalizer v2 (FD ordinals, host-path stripping)
- File-writing seed + `files_written` recording/validation
- `task_open` ABI header, signature guess, known-callee extraction
- `corpus_build` targeted builds; `status` replay % / hidden-test readiness
- Tool descriptions carry the submission contract
- Program-path ledger accounting
- Phase-0 calibration: divergence/acceptance payload improvements

**Exit:** all P0–P2 sweep issues closed; corpus and validators match spec.

## Phase 2 — In-Context Memory & Cross-Task Reasoning

Agent retains lessons across tasks; harness measures transfer rather than
per-task performance alone. Brainstorm settled 2026-08-02; decomposed into
subphases 2A/2B (specced together so accounting and memory schemas don't get
rebuilt), with 2C benchmarking on top. Design decisions of record:

- **Family keying, not fingerprints:** memory keys on `{seed, function}` from
  the manifest — deterministic family linkage across slots (O0→O2 → stripped,
  gcc→clang) with zero false-match risk. Structural CFG fingerprints are the
  explicit upgrade path when new binaries arrive without a manifest family
  (phase 3 material).
- **Two-tier provenance:** harness-computed *verified facts* (written on gate
  acceptance only; 100% truth) vs agent-declared *unverified_hypothesis*
  annotations (promoted when the noted submission is accepted).
- **Cost-shaped reward:** efficiency $E_i = \mathbb{I}(\text{accepted}) \cdot
  e^{-(\alpha \cdot \max(0, N_\text{exp}-1) + \beta \cdot (N_\text{sub}-1))}$
  with α=0.15, β=0.40. No wall-clock term (CI/podman jitter is flake, not
  skill); probe & submission counts are discrete and already ledger-adjacent.
- **Per-family JSONL storage:** `.reschema/memory/<seed>.jsonl`, same
  single-process atomic-write discipline as the ledger — no SQLite until a
  real query need appears.
- **Strictness invariant:** the hidden gate stays the only judge; memory must
  never relax acceptance semantics, only accelerate reaching them.

### 2A — Telemetry & reference benchmark (ISSUE-2A)
- Ledger probes counter (experiment accounting, both program and function
  paths) next to the submissions/rejections counters.
- `status` gains `efficiency`: `{E, n_exp, n_sub, alpha, beta}` computed from
  ledger state alone.
- Deterministic reference-agent harness: scripted known-good submits for seed
  functions across family triplets, producing reproducible E trajectories as
  the falsifiable baseline for 2B.
**Exit:** E is measurable and reproducible in CI for one full family run.

### 2B — Deduction cache & task_open injection (ISSUE-2B)
- `.reschema/memory/<seed>.jsonl` schema: `verified_fact` entries auto-written
  on acceptance (fn, params spec, accepted source, audit seed); `unverified
  hypothesis` notes via `submit_model(notes=[...])`, promoted on the noted
  submission's acceptance.
- `task_open` injects family-matched cache entries for `{seed, function}`
  slots, provenance-tagged.
**Exit:** blind-agent rerun against 2A's reference baseline shows fewer
submissions/probes to accept on later family slots, with hidden-gate
strictness unchanged.

### 2C — Transfer benchmark protocol
Prime-vs-unprimed family runs (O0→O1→O2→stripped): Φ trajectory per family
with the reference agent in CI as the wiring proof, and a documented dogfood
protocol to measure the same with a live agent (the way ISSUE-08/09/10
feedback was measured). Runs once after 2B lands, before phase 3 consumption.

**Explicitly deferred:** CFG/fingerprint keying, SQLite store, wall-clock
score term — each only when a named trigger revives it.

## Phase 3 — Adversarial Self-Play & Curriculum Generation

Harness generates new tasks/attacks from observed agent failures; a curriculum
hardens both agent and verifier over time. Details TBD at brainstorm.

## Phase 4 — Preference Harvesting & Weight-Level RSI

Harvest preference pairs from verified outcomes; explore weight-level updates
as the escalation beyond in-context memory. Last on purpose: least reversible,
needs the strongest verifier and the richest data pipeline. Details TBD.

## Why this order

Dependencies force it:

- **1.5 first** — every later phase consumes the verifier's judgement; a soft
  judge corrupts memory, curriculum, and preferences alike.
- **2 before 3** — curriculum only compounds if the agent can carry lessons
  between tasks.
- **3 before 4** — preference data is worth harvesting once self-play produces
  diverse, verified trajectories; weight updates are the riskiest step and
  deserve the most mature signal.
