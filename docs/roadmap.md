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

### Research-derived slots (RE-literature sweep, 2026-08)

Four ideas from 2025–26 neural-decompile research, refactored to ReSchema
primitives (assessment in this branch's worktree). Each lands where its
dependency already lives; none expands settled scope.

- **Two-pass repair directive** (Skeleton→Skin refactored): Level B
  rejection-repair prompts split abstract bit-logic repair (fixed-width ints,
  no semantic naming) from later idiomatic-type annotation. Coaching-context
  only — zero validator code. **Slot: 2B** `task_open` injection payload.
- **Syscall dependency slice** (backward slicing refactored): `divergence`
  payloads gain the fd/buffer-linked backward syscall chain
  (`open → read → write[FAIL]`), sliced from the recorded Qiling trace — no
  disassembly-level slicing. Cuts repair-context tokens per rejection.
  **Slot: 2B**, ships alongside cache injection (both are reject-context
  quality for E).
- **Call topology digest** (graph grounding refactored): `verified_fact`
  JSONL entries may carry a small call-graph digest (depth, callee list) so
  stripped slots map `fn_0x…` back to family names via topology, not
  symtab. **Slot: 2B**, optional field in the JSONL schema.
- **Single-input probe** (micro-assertion refactored): Level B `experiment`
  gains a `single_input` mode — one ctypes run against the original slice
  before a full campaign — keeping early-hypothesis N_exp cheap. 2A's probe
  accounting already covers both paths. **Slot: 2B** Level B tooling.

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

### 2C — Transfer benchmark protocol (COMPLETE)
Prime-vs-unprimed family runs (O0→O1→O2→stripped): Φ trajectory per family
with the reference agent in CI as the wiring proof, and a documented dogfood
protocol to measure the same with a live agent (the way ISSUE-08/09/10
feedback was measured). Runs once after 2B lands, before phase 3 consumption.
Driver for the live-agent reruns shipped as `tools/dogfood/` (PR #75;
invocation in docs/benchmark-protocol.md §6).

Three published, integrity-audited floors under `docs/benchmark-results/2c/`
(gemma4:26b via ollama), one per prompt/tool-surface configuration
(protocol §5's A/B/C family):

- **config A** (2026-08-16): rot13 φ median −0.463 — memory *hurt*;
  protocol-literacy failure, surfaced issues #91–#95.
- **config B** (2026-08-18, + priming UX #91–93, flail guard #95, per-slot
  transcripts #94): priming arrives (four E=1.0 late-reuse slots; renderer
  rot13 φ median 0.0), but transcript audit exposes ~46% of `agent-exit` as
  **false completions** (function accept misread as task completion, #103);
  two corpus-tainted records caught by review and rerun clean after #104.
- **config C** (2026-08-18, + `task_complete` completion framing #103):
  **false-completion class at zero**; program accepts 37/60, rot13 primed
  chains 15/15, 12 accepted late-reuse slots. Released as **v0.2.0**.

Dogfooding-closed issues #91–#100–#103–#104 all landed with negative tests;
the live smoke also exposed and closed a gate soundness hole (#100, vacuous
function specs passing on one-point comparisons).

**Explicitly deferred:** CFG/fingerprint keying, SQLite store, wall-clock
score term, solver-assisted seed generation (config-D candidate — a
measurement-governance decision, not an engineering one), basic-block
interval tasks, eBPF/host trace recording, VMP/packed-target handling —
each only when a named trigger revives it (the phase-3 tier curve is the
nominated trigger for the last four).

## Phase 3 — Adversarial Self-Play & Curriculum Generation

Harness generates new tasks/attacks from observed agent failures; a curriculum
hardens both agent and verifier over time. Brainstormed and decomposed
2026-08-18 (five-frame divergent pass, traps pruned on record): **three
circuits plus a measurement-governance floor**, each with an
experiment-or-instrument entry point before any curriculum machinery ships.

**Pruned in brainstorm (do not resurrect without a named trigger):**
demoting verified-fact provenance (softens the trust contract the priming
claim rests on), stale-fact sabotage tasks (teaches agents to distrust the
measured mechanism), quit/permdeath taxes (distorts E's calibrated
economics), plaza-style pass-rate pricing (premature for n=5 floors).

**Entered from 2C evidence (2026-08):**

- **Verifier-first prerequisite (ISSUE-109):** coverage-guided function-mode
  differential fuzzing — uniform `gen_inputs` provably cannot reach
  magic-value branches in function mode, so a wrong-branch model can pass
  the gate. Per the §1.5 doctrine (a soft judge corrupts everything
  downstream), this lands *before* curriculum work start.
- **Obfuscation-tier corpus curve as the curriculum-design input:** one
  clean-semantics seed built in attack tiers (xor-strings → CFF-ish
  transforms → env-probe) measures where the engine's walls actually are
  (double-record determinism, canonicalization, hidden-gate sampling),
  replacing debate with boundary data. Its outcome is the named trigger for
  the deferred adaptations above (BB-interval tasks, eBPF recording,
  config-D solver hints).

### 3A — Self-play engine (curriculum supply)

Ledger rejected `c_source` → **compile-clean + deterministic-double-record
filter** in the existing pinned podman image → survivors registered as new
corpus slots. Provenance is perfect (the harness owns the build),
information parity holds by construction (the solver wrote the source — no
insider info possible), and the failure supply self-renews with every
rejection. Boss-synthesis capstones splice the last-three failure classes
into one slot requiring a clean clear for tier promotion; synthetic cheater
gate-bypasses compile into a permanent negative-regression garden; slots
priced by measured pass-rate with half-life culling.

**Load-bearing risk:** the juiciest failure residue is UB-laden — compile to
nondeterminism poisons ground truth. The filter's yield decides viability
before any engine work. **Entry (#111):** offline fodder-yield
experiment — mine `rejected_sources` (the ledger failure store that
prerequisite review required first, #116; pre-store history comes from
floor transcripts), compile+double-trace every rejected source, report
keep-rates per failure class.

### 3B — Judge-integrity circuitry (verifier hardener)

Every verifier upgrade (seed policy, #109 coverage guidance, canonicalizer
bump) **re-grades the last K accepted sources** under xdist-isolated
RESCHEMA_HOME; an accept→reject flip halts curriculum. **Adjudication
defaults to "judge regressed"** unless independent deterministic evidence
(coverage delta, canonicalizer diff, N-fold fresh-entropy reproduction)
proves the old accept was flattery — anything less lets a newly-blind judge
relabel its own regression and silently delete the tripwire. A pinned
prior-release judge (v0.2.0) cross-grades a sample per cycle as the
co-evolution tripwire; the known-attack autoimmune battery runs per-PR in
CI (softness unmergeable, not per-cycle archaeology); every generated attack
carries a coverage delta (#109 machinery). Adjudicated flattery pairs are
the highest-information preference data Phase 4 will ever see.

**Load-bearing risk:** the frozen-anchor sample rate vs CI budget.
**Entry (#112):** the isolated re-grade job + one xdist test —
recompile last K ledger accepts in the pinned image, re-run the hidden
gate, emit verdict-diff JSON. Measurement first, adjudication only after
its output is trusted. **Delivered:** `python -m reschema.regrade`
(ARCHITECTURE.md "regrade.py"). It covers function accepts too, replayed
with their audit seed so a flip isolates the judge change. Program accepts
replay the audit `hidden_seed` by default, `--fresh` for new entropy.
Next: run it over real dogfood ledgers and feed the flips to
adjudication.

### 3C — Honesty boundary (the generator's sense organ)

One pure chokepoint, **`digest()`**, converts ledger failure records into
canonicalized antigen fragments (ADDR-ordinals + structured divergence
payload; fuzz seeds, entropy draws, gate traces, whole originals stripped)
— schema keyed to `CANONICALIZER_VERSION` so a digest change forces a
curriculum re-record under the same rule as canonicalizer v2. The generator
knows nothing else; the insider ban is enforced by bandwidth, not by rule a
future generator could break. Task proposals carry a derivation certificate
(fragment IDs + public artifacts) and pass an engine-side **parity auditor**
(privileged lane vs public 5-tool lane, harness-fixed budget like the
N_FUZZ floor) that voids non-reproducible edges with a structured reject;
admission writes a provenance-complete unit — Phase 4's eligibility filter.

**Load-bearing risk:** the public-lane budget IS the definition of
"legitimately obtainable", and parity verdicts under fresh entropy are
nondeterministic — pinned auditor seeds or margin-banded verdicts required
before the auditor is trustworthy. **Entry (#113):** `digest()` plus
one snapshot-pin test (seeds/entropy/traces never survive a fragment);
route the family-memory writer through it so enforcement exists before the
auditor does.

### 3D — Metric-epoch governance (measurement invariance)

The metric parameter vector θ_E = (α, β, N^exp_min, HIDDEN_N) —
(0.15, 0.40, 1 probe baseline, HIDDEN_N=8) — is pinned behind a
`METRIC_EPOCH` constant with the same immutability rules as
`CANONICALIZER_VERSION`: immutable within an epoch, zero in-flight tweaks,
explicit epoch transitions. Ledgers and `status` stamp `metric_epoch`;
benchmark aggregation **refuses φ across mixed epochs** without
re-baselining or an explicit override flag. An epoch transition re-baselines
against the frozen reference anchor suite and reports ΔE_drift first-class
(normalized cross-epoch transfer Φ* = Φ − ΔE_drift). Without this,
cross-epoch transfer is an uncalibrated moving target — a higher E could
mean a more forgiving penalty curve, not better deductive generalization.

**Entry (#114):** the epoch constant + ledger/status stamping + the
mixed-epoch φ refusal in `tools/dogfood/measure.py`. Drift suite lands when
the first transition actually looms — premature before then.

### Phase-3 ordering

3D's stamps are cheap and guard everything downstream of measurement; 3A's
fodder experiment decides whether the engine exists at all; 3B's re-grade
job starts measuring once #109 lands (it parasitizes the coverage
machinery); 3C's chokepoint precedes the first generated task ever written.
Curriculum machinery proper starts only when each circuit's entry
experiment has returned its boundary data.

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

## Backlog — lessons from REA (2026-10)

Cross-project review against [morluto/rea](https://github.com/morluto/rea)
(an agent RE *toolkit*: broad, cooperative, evidence-labelled, but no
mechanical judge). The doc-facts guard (`tests/test_doc_facts.py`) landed
directly; the rest are unscheduled candidates, each tied to the phase whose
invariant it serves. None relaxes the judge or widens v1 scope.

- **Accept payloads state what they do not prove.** REA's rule: `unknown`,
  `truncated`, `skipped` and `unsupported` never aggregate to `pass`. We
  already fail loudly on starvation, but several limits live only in prose:
  compose is linkage-only, level B never compares syscalls, the batch
  syscall scan does not follow callees, a finite grammar-aligned hidden draw
  cannot guarantee every overfit is rejected. Candidate: a constant,
  snapshot-pinned `limitations` list on program/function accepts (truth-only,
  like `TASK_INCOMPLETE_NOTE`). Serves the 3C honesty boundary.
- **MCP prompts for the prompt-as-a-skill gap (#88).** REA ships six MCP
  `prompts` with argument completion against session state
  (`docs/mcp-prompts.md` there) — a working reference for moving procedural
  coaching out of payload fields. Must stay optional: tool contracts remain
  the judge's only interface.
- **Verdicts commit their inputs.** REA reuses snapshots/replays only when
  target bytes, operation, provider, profile and plan digest all match. Our
  canonicalizer stamp guards the corpus, but ledger accepts do not appear to
  commit the binary sha256, toolchain image digest, or validator/gate
  version — a toolchain or gate change leaves old accepts silently "valid".
  Candidate: stamp them on `audit` entries and have `status`/benchmark
  aggregation flag stale accepts. Natural sibling of 3D `METRIC_EPOCH`, and a
  prerequisite for trusting the 3A rejected-sources supply across versions.
- **Stratify hidden draws by outcome class.** REA's obligation ledger closes
  a claim only when positive/negative/malformed case kinds are all covered.
  Analogue: require each hidden round to include draws landing in every
  outcome class observed in the recorded cases (distinct exit codes, empty
  vs non-empty stderr, files written or not), so an always-`"bad magic"`
  stub on `pkfmt` is rejected structurally rather than probabilistically.
  Must keep fresh per-submission entropy; starvation of a class is a loud
  reject, as today. Serves 3B judge integrity.
- **Declared nondeterminism before real-world binaries.** REA compares
  captures against a caller-declared `partial_order` or `finite_traces`
  spec with an explicit, bounded `ignore_fields` set, never promotes
  timestamps to ordering evidence, and returns `unknown` for truncated
  captures. Canonicalizer rules suffice for static single-threaded ELFs;
  growing them for threaded/nondeterministic targets will not scale. ADR
  first, no code — it touches the scope guardrails.
- **Environment preflight (`doctor`).** REA's readiness verifier separates
  a broken runtime from an analysis failure. Our 2C reports already have to
  teach readers that post-preflight endpoint death reads as
  `aborted: agent-exit`. Candidate: a structured preflight (podman present,
  image digest matches `Containerfile`, corpus built, canonicalizer stamp
  current) surfaced through `status` with no task id — keeps the tool table
  at five — and reused by the dogfood driver's abort classification.

Considered, not adopted: REA's "complete results by default" contract —
recorded in [rejected-ideas.md](rejected-ideas.md) §3. Decompiler-backed
context is parked below as speculative post-1.0.

## Backlog — research survey (2026-10)

From the October 2026 survey of agentic RE harnesses, neural decompilation,
evaluation/verification, and adjacent code-RL harness design. Sources were
mostly read as abstracts or secondary summaries and are largely
author-reported, unreplicated 2026 preprints: re-check figures against the
full papers before citing externally. Ranked by judge value — what makes the
judge harder to fool first, context and tooling next, Phase 4 hygiene last.
The ideas the survey refused are recorded, with reasons and reopen
conditions, in [rejected-ideas.md](rejected-ideas.md).
The report and the four research notes behind it are kept in
[research/2026-10-agentic-re-harnesses/](research/2026-10-agentic-re-harnesses/README.md),
including errata where they disagree with the code.

### P0 — know how strong the judge is

- **Gap: level B never compares inputs on which the original faults.**
  `validate/function.py` skips every fuzz case where the *original* crashes
  ("a crash is not a behavior spec"), so a model that silently "fixes" the
  original's crash is never compared on that input — exactly the
  crash-absence divergence the 2026 literature measures (Decompile-Diverge,
  arXiv 2609.05370). The `crash` field only catches a *model* that crashes.
  Measure before changing the gate: many current skips are plausibly
  harness artifacts (e.g. register-junk pointers from all-i32 sketches)
  rather than real original behavior. Order: log per-slot `skipped` rates
  (already on verdicts) → classify which faults are deterministic under
  poison buffers → ADR on whether a boolean fault-or-not comparison enters
  `field: crash`. Any accept→reject flip routes through the 3B re-grade job
  (#112) with the "judge regressed" default, and ships with the negative
  test AGENTS.md requires: a model that suppresses the original's
  deterministic crash.
  **Measured (`tools/crash_census.py`, 2026-10-09; pins in
  `tests/test_crash_census.py`):** over all 108 function slots × the gate's
  own 64-case draw (fuzz + 109-A scouts, so out-of-range scout needles
  included), true-signature specs fault the original on **0/6912** cases.
  A mistyped spec (every param i32, ret i32) faults on 2736/6912 (48
  slots), every one an `Invalid memory read` from a pointer declared i32,
  and 0 of those are nondeterministic on re-run. So on today's corpus every
  skip is a spec artifact; crash-absence divergence is unexercisable until a
  seed has a genuine reachable fault. The mistyped spec is NOT the
  agent-facing `_abi_template` for void functions (that one gives them a
  memory channel, codex P2 on #140); it is what an agent can declare by
  hand, and for non-void pointer functions it coincides with the template.
  **Decided (ARCHITECTURE.md ADR "Original timeouts stay skipped, memory
  faults reject at spec"):** no
  fault-or-not compare until a genuine-fault seed exists. Reopen trigger:
  `test_ref_specs_never_fault_originals` (every reference spec × every
  slot) fails. That test is a SAMPLED tripwire (one pinned draw, default
  ranges), not an exhaustive guard (codex P2 on #140).
- **Closed: skip floor (spec stage).** A hand-declared all-i32 `scale_buf`
  spec thinned to its n≤0 survivors (eax is 0 on every one), and a
  `return 0;` stub was ACCEPTED on all 12 `scale_buf` slots with fresh
  seeds (25–37 compared, 27–39 skipped). Design call taken: count only
  MEMORY faults (unicorn errno READ/WRITE/FETCH × UNMAPPED/PROT/UNALIGNED,
  the pointer-typing signature; prose matching missed PROT, so a pointer
  ranged into the read-only image bypassed it) at zero tolerance, on agent-declared cases only (109-A
  scouts excluded, as for the #100 floor); timeouts stay skipped, because
  correct-typed `sum_range` over a declared full-i32 range times out on
  39/64 cases (`test_true_spec_faults_on_declared_range`; positive control
  `test_skip_floor_ignores_timeouts`, stubbed: each real timeout costs 3s
  of the 120s budget). The reject is
  `stage: spec` (already in `DUP_NO_VERDICT_STAGES`: no fingerprint, no
  `rejected_sources` entry), runs before the model compiles, and its
  `detail` names the first faulting case and both remedies: retype a
  dereferenced scalar as `buffer_i32`/`cstring`, or narrow a range that
  admits values the original cannot handle (a correctly typed i32 index
  past a table faults the same way, so a pointer-only hint would mislead). Negative test: `test_mistyped_spec_stub_rejected` (strict xfail
  dropped). `audit[func]` now also persists `compared`, `skipped` and the
  accepted `params`. Past accepts are re-judged by the #112 re-grade
  (`python -m reschema.regrade`): pre-change entries hold only
  `{seed, n_fuzz}`, so it replays them with the family memory's
  `verified_fact` params (`params`, `audit_seed`, `n_fuzz`).
  Deterministic, including pre-#139 agent-seeded accepts (the effective
  seed was always recorded).
- **Closed: function mode let the agent pin its own fuzz draw.**
  `submit_model(function=…, seed=…)` used to forward the agent's seed to
  `validate_function`, so an agent could fix the 64-case draw and iterate
  first-divergence feedback against a known case set. `seed` is now gone
  from the MCP schema; a stray `seed` from an old client is dropped, so the
  engine draws fresh; tests pin determinism through
  `mcp/server.TEST_PINNED_SEED` (the `N_FUZZ` monkeypatch pattern); internal
  callers still pass `seed=`. The effective seed is still reported on the
  verdict and recorded in `audit`. Negative tests:
  `tests/test_seed_boundary.py`. Remaining follow-up: the ledger cannot tell
  which *earlier* function accepts used an agent-supplied seed (audit holds
  only the effective seed), so the 3B re-grade should treat every
  function-mode accept that predates this change as a re-grade candidate.
- **Mutation kill rate as the per-slot judge-strength metric.** Mutate each
  seed's reference C (operator flips, off-by-ones, constant changes, dropped
  branches, chunked vs coalesced writes), run mutants through the unchanged
  gate at `HIDDEN_N`/`N_FUZZ` with pinned mutant seeds in CI, report kill
  rate per slot. Turns 3B's known-attack battery into a measurement and
  gives #109 a falsifiable exit criterion ("magic-branch mutant kill rate
  moves from X to Y"). No published baseline exists for differential RE
  judges at these budgets. Reported in CI/benchmark artifacts only, never
  in an MCP response (rejected-ideas §4).

### P1 — harden inputs and feedback without touching acceptance

- **Offline hidden-input pool at `corpus_build`.** Coverage-guided fuzzing
  (optionally SymCC/angr as a *generator*) against the original binary
  builds a per-slot pool reaching hard branches (`pkfmt` magic, version
  bounds, FNV checksum); the online gate draws hidden inputs from the pool
  mixed with fresh mutations under the per-submission seed. Invariants:
  coverage is a sampling heuristic, never acceptance; the verdict stays
  differential replay; entropy stays fresh in which entries are drawn. Needs
  an ADR — ARCHITECTURE records "no branch coverage (explicitly cut)" and 2C
  deferred solver-assisted seed generation — stating harness-side input
  generation is neither agent-facing solver help nor coverage-as-acceptance.
  Cost lands at build time (mind the 120s suite budget). Extends #109 and
  the stratified-draws item above.
- **Adversary-model hackability audit per slot class.** An adversary model
  gets a fixed budget per slot class to produce a wrong-but-accepted model;
  the success rate becomes a first-class corpus metric (method of arXiv
  2606.16062). Gives 3A's synthetic-cheater garden a number.
- **Harness-rendered arithmetic in function divergences.** Function-gate
  divergences render `expected`/`actual` as a single truncated `str()`; add
  signed, unsigned and hex views of the *same single divergence*. Removes
  the most-cited practitioner hallucination (base/sign conversion) without
  revealing anything new. Payload-only.
- **Regression telemetry across resubmissions.** Record in the ledger when a
  resubmission fails a recorded case an earlier submission passed (the
  failure AutoDecompiler needed explicit machinery to suppress). Starts as
  harness-side telemetry in the ledger, surfaced only in benchmark/admin
  reports — not in `status`, which is agent-visible. Any agent exposure
  waits for 2C evidence.

### P2 — prepare the tool surface for real binaries (five tools unchanged)

- **ReF-style context in `task_open`.** Relabel jump targets in the capstone
  slice and expose referenced `.rodata` contents, consistent with the
  canonicalizer's ADDR-ordinal approach. `inferred` tier, never verdict
  input, explicit `unavailable` when absent — a lightweight precursor to the
  post-1.0 decompiler facets, under the same constraints.
- **Caller/callee pointer-usage hints for param specs.** Offsets and widths
  touched at caller and callee sites suggest pointer kinds (Idioms, ReSym)
  — coaching only. Feeds the named pointer-kind sketch follow-up that would
  make `pk_extract`/`pk_checksum` representable function tasks.
- **Origin-keyed provenance and hostile-text labelling.** Once strings,
  `stdout_decoded` previews and decompiler output come from attacker-chosen
  binaries, they are hostile text in the agent's context ("When Binaries
  Talk Back", arXiv 2607.12507: planted text drove 35/40 unsafe proposals).
  Label binary-derived fields by origin, keep hex authoritative, and count
  memory support per origin so repeated agent hypotheses never corroborate
  one another. Extends two-tier provenance and the 3C `digest()`
  chokepoint; precondition for real binaries and for the dogfood sandbox.
- **Perturbed twin slots.** Renamed/reordered seeds with identical
  semantics, plus a generated/public split, to separate transfer from
  recall in 2C. SRE-Bench finds optimization level and static linking
  matter little while scale and anti-analysis dominate — prioritize the
  obfuscation-tier curve over more compiler/O-level permutations.

### P3 — optional experiments

- **Failure-lesson memory tier.** ReasoningBank's gain came from memory
  distilled from *failures*. A third tier built from a rejected
  submission's divergence class, routed through `digest()` (no source, no
  entropy), tests this without touching `verified_fact`. Keep the
  `verified_on` slot tag on everything injected (stale memories mislead —
  MemSyco-Bench). Mind 2C config A, where memory hurt.
- **Phase 4 hygiene.** RLEF-style split: in-episode feedback from recorded
  cases, terminal reward from the fresh hidden gate. Replayable episodes by
  logging each drawn seed in the audit entry *after* the draw, never by
  pinning. Per-rollout `RESCHEMA_HOME` with a merge step instead of
  shared-state concurrency. Every reward stamped with `METRIC_EPOCH`. An
  optional P(accept) calibration score, kept separate from E.

### Further candidates (unranked)

Smaller items from the survey notes that the ranked list above did not
carry. Each is cheap or narrow, none touches acceptance, and each names the
note it came from.

- **MCP tool annotations.** Declare each tool's effects in its definition
  (re-mcp practice), so clients know what is safe to retry. Only `status` is
  read-only and idempotent. `experiment` is **state-changing**: it
  increments the ledger's `probes` counter, which E prices, and in program
  mode it persists a `trace_*.json` that later validation replays, so a
  retry changes both the gate's inputs and E. `submit_model` and
  `corpus_build` are state-changing. Audit `task_open` for writes before
  annotating it. *(tools note §6, corrected in review)*
- **Bounded tool output for real binaries.** Cursor pagination with hard
  caps on `task_open` disassembly slices and on the `status` ledger, and a
  batch form of `experiment` with per-item errors, before real binaries make
  slices large ("context rot", ReVa). Batch probes must still count as
  probes in E, one per item. *(tools note §1, §6)*
- **Gold-sanity check for generated tasks (3A).** Every generated
  function-mode spec or hidden input is validated against the harness-owned
  reference build before it becomes a task. Program-mode generation already
  has this through double-recording. Extend it so a vacuous spec (the #100
  class) is refused at generation time, not at submission (arXiv
  2606.16062). *(adjacent note §7)*
- **Independent-evaluator posture (3B / 2C governance).** Like TRACTOR
  (MIT Lincoln Laboratory publishes test batteries and scripts, and
  performers do not grade themselves), publish the hidden-gate code and
  per-slot judge-strength numbers so outsiders can re-grade results. Entropy
  stays fresh, so publishing the gate does not publish the draws.
  *(adjacent note §7)*
- **The two-pass directive re-verifies the second pass.** The 2B repair
  directive should say explicitly that the idiomatic ("skin") pass is
  re-submitted to the same level-B judge. The literature's main failure is
  fields, types and guards invented during readability passes (SK2Decompile,
  D-LiFT, Decompile-Diverge). *(neural note §6)*
- **Phase 4 preference-pair rule and memorization floor.** A preference
  pair's "chosen" side must pass the hidden gate at harvest time, under
  fresh entropy. Track unprimed-agent success per seed alongside primed
  runs, so gains from the cache or curriculum are not mistaken for skill
  (arXiv 2609.17236). Shaping terms, if any, reward only harness-drawn
  inputs, never agent-visible ones. *(neural note §6)*
- **2C reporting discipline.** Report primed vs unprimed φ with confidence
  intervals, and record failure to reject as a null result rather than
  weak support. DreamBench-SWE found no difference between memory
  pipelines. This supports 3D's refusal to aggregate across epochs.
  *(adjacent note §7)*
- **Phase 3 curriculum axes.** The literature's difficulty cliffs (about 30
  points from O0 to O3, and a 55–68% drop once struct types appear) suggest
  the axes optimization level → inlining → struct-*by-pointer* params →
  stripping. These stay inside v1's integer and pointer params
  (rejected-ideas §9). Weigh them against SRE-Bench's finding that scale and
  anti-analysis dominate; see the obfuscation-tier item above.
  *(neural note §6)*
- **Tool-description changes are regression-tested.** Any change to an MCP
  tool description or payload shape runs through the 2C driver as an
  agent-level eval, not only the contract test. The #103 false-completion
  fix was effectively such an experiment (Anthropic tool-writing guidance,
  SWE-agent interface ablations). *(adjacent note §7)*
- **Open check before external citation.** Read Decompile-Diverge (arXiv
  2609.05370) §method and compare its "bounded observable post-state
  digest" with level B's `{ret, mem}` comparison. Do this before calling
  them equivalent, or before claiming ReSchema is stricter.
  *(neural note §6, report)*

## Speculative — post-1.0

Not scheduled, not specced; recorded so the idea has a home when the
real-binary milestone arrives.

- **Decompiler facets in `task_open` (Ghidra/Hopper).** REA drives Hopper
  and a bring-your-own Ghidra (headless, read-only operations) behind a
  provider-neutral interface with deterministic provider selection and
  digest-exact snapshot reuse. Once targets stop shipping with a manifest
  and capstone slices stop being enough context, pseudocode, xrefs and
  recovered types could ride alongside the disasm slice. Constraints if it
  lands: `inferred`-tier context only, never verdict input; the provider
  runs inside the pinned toolchain image (or its own pinned image), never
  the host; provider identity and version are committed with the facet so a
  provider change is visible in the ledger; absent or failing providers
  degrade the facet to an explicit `unavailable`, not a silent omission.
- **External comparison corpus.** "Recompilation Is Not Enough" (arXiv
  2609.07201) reports 87.5% on 104 Coreutils binaries with an exact-output
  gate, using fixed tests and no hidden fresh inputs. Once real-binary scope
  opens, Coreutils is a candidate external benchmark, run under ReSchema's
  hidden gate for a like-for-like comparison. *(adjacent note §7)*
