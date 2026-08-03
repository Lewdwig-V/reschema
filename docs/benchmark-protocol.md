# Transfer benchmark protocol (phase 2C)

Measuring cross-task reasoning: does family memory reduce cost-to-accept on
later slots of a seed family, at unchanged gate strictness?

**Families:** `rot13` (argv/program) and `check` (stdin/program).
**Slot chain:** O0-sym → O1-sym → O2-sym for each seed.
**Two runs per family, fresh `.reschema` per run:**

| run | cache | slot 0 | slot 1+ |
| --- | --- | --- | --- |
| UNPRIMED (baseline) | empty | 6 probes + 1 submission | 6 probes + 1 submission per slot |
| PRIMED | filled from slot 0 | 6 probes + 1 submission | 0 probes + 1 submission (cached verified source) |

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
