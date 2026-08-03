# Transfer benchmark protocol (phase 2C)

Measuring cross-task reasoning: does family memory reduce cost-to-accept on
later slots of a seed family, at unchanged gate strictness?

**Families:** `rot13` (argv/program) and `check` (stdin/program).
**Slot chain:** O0-sym → O1-sym → O2-sym for each seed.
**Two runs per family, fresh `.reschema` per run:**

| run | cache | slot 0 | slot 1+ |
| --- | --- | --- | --- |
| UNPRIMED (baseline) | empty; per-slot isolation | 6 probes + 1 submission | 6 probes + 1 submission per slot |
| PRIMED | filled from slot 0 (shared root) | 6 probes + 1 submission | 0 probes + 1 submission (cached verified source) |

Two protocol disciplines (pinned by tests, both required for the numbers to mean
what they say):

1. **Probe sets must include an accepting case** (e.g. the check family's
   known pre-image `txy"od`). A family whose recorded cases are all rejections
   lets an always-reject model complete the whole protocol and get cached as a
   verified_fact — every family in the protocol proves the seed accepts
   *something*.
2. **UNPRIMED isolates memory per slot.** A shared root leaks slot 0's
   auto-written verified_fact into later openings, so "cold baseline" stops
   being cold. Slot-local memory roots (see test driver) keep unprimed slots
   truly memory-free at their open time.

A live-agent rerun that ignores either discipline produces Φ measurements
against a different protocol than the CI wiring guarantees.

**Metric:** `E = accepted * exp(-(0.15 * max(0, probes-1) + 0.4 * (subs-1)))`
per slot; `phi = mean((E_slot - E_baseline_slot0) / (1 - E_baseline_slot0))`
over later slots (uniform weights — no difficulty multipliers until dogfood data
supports them). Expected on the reference agent now: perfect transfer ⇒
`phi == 1.0`, trajectory `[exp(-0.75), 1.0, 1.0]`.

**CI wiring:** `tests/test_transfer_protocol.py` — runs this protocol
deterministically and pins baseline-flat and primed-P2 trajectories for both
families.

**Live-agent reruns (dogfood):** same slot chains with a REAL agent and two
fresh `.reschema` mounts; log `E` per slot from `status`. Friction observed in
the Phi trajectory is the live-agent signal; regressions in the reference
baseline mean instrumentation changed, not the agent.
