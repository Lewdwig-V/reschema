# ReSchema — Aspirational Roadmap

Living document. Records the intended phase order after the current issue PRs
land; **phases 2+ are deliberately vague** — each gets brainstormed → specced →
planned → broken into issues only when it becomes the active phase. A phase's
prerequisite is that the one before it holds.

## Phase 1.5 — Engine Hardening & Spec Alignment

Close the MVP-sweep issues (raised 2026-08-01, ISSUE-01 … ISSUE-10, P0–P2):

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
per-task performance alone. Details TBD at brainstorm.

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
