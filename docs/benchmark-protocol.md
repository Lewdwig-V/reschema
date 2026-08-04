# Transfer benchmark protocol

How ReSchema measures cross-task reasoning: does what the harness remembers
about a seed reduce the cost of solving *later, harder-looking* builds of that
same seed — without making the judge any softer?

Reader's guide: this is a methods document. Section 4 states plainly what is
proven today (instrumentation only) and what is not (any live-agent transfer).

## 1. Question and hypothesis

The engine caches two kinds of memory per seed family (all build slots of one
seed): `verified_fact` entries written by the harness when a gate accepts, and
agent notes promoted on their submission's acceptance. A later slot of the
family sees these at `task_open`.

**Hypothesis (H1):** a primed family — one whose deduction cache was filled
by solving its first slot — reaches acceptance on subsequent slots at lower
agent cost than a cold baseline, at unchanged gate strictness.

**Null (H0):** priming yields no cost reduction on later slots (φ ≤ 0, metric
below), or any observed gain is explained by instrumentation rather than by
the agent using the memory.

**What would disconfirm what:** φ low on a live agent while the CI reference
run holds = the agent failed to use memory (the interesting negative result,
and a coaching/tooling signal). φ moving on the *reference* run = the
instrumentation changed; any numbers taken against the new wiring belong to a
different protocol and are not comparable to earlier runs. Both outcomes are
reportable; confusing them is not.

## 2. Metric

Per slot:

```
E = accepted · exp( −(α·max(0, probes−1) + β·(submissions−1)) ),  α=0.15, β=0.40
```

with counter values read from the task ledger at `status` (an unaccepted slot
scores 0). Per family, φ ("phi", the only spelling) is the mean headroom
recovered on slots after the first:

```
φ = mean over slots k≥1 of (E_primed[k] − E_unprimed[0]) / (1 − E_unprimed[0])
```

φ=1 means primed slots scored a perfect 1.0; φ=0 means primed exactly matched
the slot-0 baseline; φ<0 means memory made things worse.

**Why exponential, why these counts:** E is a score in (0,1], and exp decay
keeps it there for any non-negative cost while making early effort dominate —
the regime the harness cares about (flail early, cheap convergence late).
Wall-clock is excluded on purpose: CI/podman jitter is flake, not skill.
Probes and submissions are discrete, ledger-adjacent, and agent-controlled.

**Why β ≈ 2.7×α:** a submission spends the hidden gate's information (fresh
inputs are drawn per submission) plus a compile+replay round; a probe spends
one emulated run. The 2.7× ratio is a chosen prior for "submissions are the
expensive act", **not a measured value** — it and α/β are provisional until
live-run data argues for a specific revision.

**Why `max(0, probes−1)` but an unguarded `(submissions−1)`:** probes can
legitimately be 0 — a primed slot submits the cached verified source without
re-probing, and the guard prevents that from becoming a *reward* above 1.0.
Submissions cannot be 0 on an accepted slot by construction (E is only
nonzero when `accepted`), so the guard would be dead code. The asymmetry is
designed, not accreted.

**E=1.0 for zero probes, defended:** this sits deliberately against the
harness's honesty premise. In every other corner the harness distrusts claims
made without evidence — here the evidence already exists: the cached source
passed the gate once, and the cached entry is *harness-written*, not
agent-asserted. Re-probing a verified fact is pure waste, and E rewards not
wasting. The premise holds because *producing* the fact required the gate;
*reusing* it did not.

**The normalizer's assumption:** φ divides by headroom at *slot 0 of the
unprimed run*, not each slot's own unprimed score. Those are equivalent only
when the unprimed trajectory is flat. CI pins flatness for the deterministic
reference agent; a live agent's unprimed trajectory will not be flat. The
protocol therefore: (a) keeps slot-0 normalization for cross-run
comparability, and (b) requires reporting the full unprimed trajectory. If a
live unprimed run departs materially from flat, φ is not interpretable —
report per-slot deltas (E_primed[k] − E_unprimed[k]) alongside instead.

## 3. Design

**Families:** `rot13` (argv-fed program) and `check` (stdin-fed program).
**Slot chain per family:** `gcc-O0-sym → gcc-O1-sym → gcc-O2-sym` — same
source, changing binary: the natural ladder for testing whether memory keyed
on `{seed, function}` survives recompilation. (Cross-compiler and
stripped-slot legs are plausible extensions, deliberately out of this
protocol's scope.)

**Conditions:** two runs per family, each with its own fresh `.reschema/`
state (task dirs, ledger, memory roots).

| run | deduction cache | slot 0 | slots 1+ |
| --- | --- | --- | --- |
| UNPRIMED (baseline) | empty; memory isolated per slot | 6 probes + 1 submission | 6 probes + 1 submission per slot |
| PRIMED | filled by slot 0's acceptance (shared root) | 6 probes + 1 submission | 0 probes + 1 submission (cached verified source) |

**Controls (each removing a named confound):**

1. **Every family's probe set contains an accepting case.** For `check` that
   is `txy"od`, the known password pre-image (its djb2-5381 hash equals the
   constant embedded in the binary). *Confound removed:* an always-reject
   model could otherwise "solve" a family whose recorded cases are all
   rejections — and worse, get cached as a `verified_fact` for later slots.
   `tests/test_transfer_protocol.py::test_check_family_rejects_always_nope_attack`
   demonstrates the attack and the control.
2. **UNPRIMED isolates memory per slot.** Slot-local memory roots keep each
   unprimed slot cold at its `task_open`. *Confound removed:* a shared memory
   root leaks slot 0's auto-written `verified_fact` into later openings,
   contaminating the "cold" baseline and inflating it toward the primed run.

The CI wiring pins both controls plus the coldness invariant (each unprimed
slot-local root holds exactly its own post-hoc entry). Any live rerun that
ignores a control produces φ against a different protocol — report it as
such, not as a failure of the numbers here.

## 4. What is currently verified

`tests/test_transfer_protocol.py` runs this protocol deterministically with
a scripted reference agent and pins, for both families: a flat unprimed
trajectory `[exp(−0.75), exp(−0.75), exp(−0.75)]`, a primed trajectory
`[exp(−0.75), 1.0, 1.0]`, and φ = 1.0.

**Be exact about what that means:** with the reference agent these numbers
are arithmetic, not findings. The protocol tells the reference agent to
submit the cached verified source with zero probes on primed slots, and
`exp(0) = 1`; slot 0 pays `0.15·5 + 0.4·0 = 0.75` in both conditions.
φ = (1.0 − 0.4724)/(1 − 0.4724) = 1.0 *by construction*. The CI test is a
tautology check on the instrumentation — it proves the metric plumbing
computes what it is specified to compute, that memory priming reaches later
slot openings, and that the cold baseline stays cold. All worth pinning in
CI; none of it is a measurement of cross-task reasoning.

**No live-agent measurement exists yet.** (`tests/test_dogfood.py` exercises
the harness end-to-end over MCP, but it does not run this protocol against a
real agent.) Section 6's rerun procedure is where the actual result lives.

## 5. Threats to validity

- **Instrumentation drift.** The reference-agent trajectory is the protocol's
  control channel: if CI's pinned `[exp(−0.75), 1.0, 1.0]` moves, the metric
  or the memory wiring changed, not any agent. Live numbers are only
  comparable to other numbers taken under the same pinned reference run.
- **Non-flat live baselines** (§2 normalizer): report the full unprimed
  trajectory; a material departure from flat switches reporting to per-slot
  deltas.
- **n=1, no variance.** Production gates draw fresh entropy per submission
  (hidden inputs, fuzz seeds), so live runs are non-deterministic and a
  single paired run yields a point estimate with unknown noise. The dogfood
  floor is **≥5 paired runs per family**; report median φ and the spread, not
  one number.
- **Agent-mix confound.** Model, prompt, and tool surface are all treatment
  variables. Anything that changes how the agent solves (model upgrade,
  prompt edit, added skill/scaffolding, tool-description change) is a
  configuration change: it belongs in the run header, and numbers across
  configuration changes are not comparable without rerun.
- **Corpus reproducibility.** Builds run inside the pinned toolchain image to
  minimize machine variance, but byte-reproducibility of corpus binaries
  across machines is unverified. Record the manifest hash and
  `canonicalizer_version` with every live result.

## 6. Reproduction

**Reference run (CI):** `uv run pytest -q tests/test_transfer_protocol.py`.
Deterministic; this is the wiring proof only (§4).

**Live-agent run (dogfood):**

1. Fresh `.reschema/` per condition. UNPRIMED additionally resets (or
   relocates) `.reschema/memory/` between slots so every slot opens cold;
   PRIMED shares one memory root across the chain.
2. Drive the slot chains above through the 5 MCP tools only.
3. Per slot, record from `status`: `E`, `n_exp` (probes), `n_sub`
   (submissions), and the raw trajectory; per run, the condition and memory
   root layout.
4. Run header (required for the number to mean anything): agent model and
   version, full prompt, exposed tool set, turn/token limits, whether the
   agent had repository access.
5. ≥5 paired runs per family (§5); compare φ medians, and eyeball the
   unprimed trajectories for flatness before trusting φ.

The expected live signature under H1: primed trajectories rising after slot
0 with fewer probes; friction relative to the reference run's flat 1.0s *is*
the live-agent signal.
