# Phase 2C — Live-Agent Transfer Driver (design)

Date: 2026-08-04. Status: spec, pending review.

## Goal

Run the transfer benchmark protocol ([docs/benchmark-protocol.md](../../benchmark-protocol.md))
with a LIVE agent instead of the scripted reference agent, producing the first
reportable φ measurements (median + spread over ≥5 paired runs per family),
with a driver that survives overnight unattended operation.

Non-goals: solver scaffolding/coaching of the agent; solver-skill work (#63
stays deferred); changes to the engine, gates, or metric.

## Decisions of record (from the brainstorm)

| Fork | Decision |
| --- | --- |
| Driver mechanics | Scripted driver harness (no manual sessions) |
| First agent harness | opencode headless (v1), behind a pluggable `AgentRunner` |
| Harness adapter discipline | Core never sees harness flags; adapters carry config template, spawn cmd, prompt delivery, transcript/outcome location. Future targets: opencode v2, Claude Code, Codex, Pi |
| Tool surface | Harness-enforced allowlist = exactly the 5 reschema MCP tools; session cwd = empty sandbox; the MCP server runs repo-side. Agent physically cannot read the repo (`corpus/seeds/*.c` is ground truth in-tree) |
| Operating rules | Free-run, actuals recorded (neutral task prompt; NO probe/submission script — the agent experiments "like a physicist"; prompt neutrality is a lint-tested property) |
| Campaign strategy | Smoke run (1 family × 2 conditions × 3 slots × 1 rep) as a manual gate, then floor campaign (2 families × 2 conditions × 3 slots × 5 reps) as the reportable number |
| Agent under test | Gemma 4 on the 4090 box, OpenAI-compatible endpoint over LAN; run header pins the checkpoint digest (phase-4 fine-tuning will change the weights — checkpoint, not name, is the comparable unit) |
| Slot architecture | Slot-runner CLI atom + thin campaign runner (no Make, no monolith) |

## Architecture

New top-level `tools/dogfood/` — benchmark tooling, NOT part of the
`reschema` package (the harness itself is untouched).

```
tools/dogfood/
  slot.py        # atomic slot runner
  driver.py      # campaign runner: plan, pool, resume, watchdog
  prompt.py      # the one neutral prompt template (hash recorded in run header)
  measure.py     # status/ledger scrape, E, φ statistics, report renderer
  runners/
    base.py      # AgentRunner interface
    opencode_v1.py
  campaigns/
    smoke.yaml   # 1 family × 2 cond × 3 slots × 1 rep
    floor.yaml   # 2 families × 2 cond × 3 slots × 5 reps
tests/dogfood2c/ # CI-safe tests (fakes; no LLM, no podman, no endpoint)
docs/benchmark-results/2c/<campaign-id>/{run_header.json, slots/*.jsonl, report.md}
```

### slot.py — the atom

Input: one slot spec `{family, condition: primed|unprimed, slot_index, rep}`.

1. **Preflight**: endpoint liveness (`/v1/models` + 1-token completion);
   the serving stack's version/digest command output captured verbatim into
   the run header; failure → `infra-error`, no rep consumed.
2. **Root layout**: `RESCHEMA_HOME=.dogfood/runs/<campaign>/<family>-<condition>-repN/`.
   Corpus binaries+manifest copied in (real copies — symlink sharing reopens
   deletion race classes fixed earlier). Memory layout carries the protocol:
   - primed: one shared memory root across the chain's 3 slots (slot 0's
     acceptance auto-writes the verified_fact later slots open with)
   - unprimed: fresh root per slot → cold memory at slot open (CI wiring's
     isolation invariant, enforced by filesystem, not monkeypatching)
3. **Spawn**: neutral prompt; allowlisted session (5 tools), empty cwd;
   engine/MCP server runs against the repo/RESCHEMA_HOME.
4. **Supervise**: poll the slot root's `ledger.json` every 30s (read-only;
   driver is harness-side, no MCP round-trip needed). End on: `program`
   accept marker, guard trip, or agent exit.
5. **Emit ONE atomic JSONL record**: `{slot_id, family, condition, slot_index,
   rep, outcome, E, n_exp, n_sub, wall_s, abort_reason?, run_header}`.
   outcome ∈ `accepted | aborted: timeout | aborted: probe-ceiling |
   aborted: agent-exit | infra-error`.

### driver.py — the campaign runner

- Expands `campaign.yaml` into slot specs; pool size 4 (32 cores; each slot =
  one opencode session + qiling emulation + podman compiles).
- Resume = filesystem truth: a slot with a `slots/<id>.jsonl` on disk is
  skipped. Driver crash costs ≤ pool-size in-flight slots; rerun resumes.
- Heartbeat log line per in-flight slot per minute (visible aliveness for a
  human checking on the overnight run).
- Endpoint-flap guard: 3 consecutive `infra-error` slots → campaign aborts.

### runners/base.py

```python
class AgentRunner(Protocol):
    def prepare(self, workdir: Path, cfg: RunnerConfig) -> None: ...
    def spawn(self, prompt: str, timeout_s: int) -> SpawnHandle: ...
    def wait(self) -> AgentOutcome: ...  # exit reason + transcript path
```

`opencode_v1.py` writes the session config (tool allowlist, MCP server block,
provider base-url/model from env, empty sandbox cwd) and invokes `opencode
run`. Nothing opencode-shaped crosses `base.py`; a v2/Claude/Codex/Pi adapter
is a NEW FILE, never an edit to driver/slot.

### prompt.py — neutral template

Contains: the task id, "open it with the tools provided", "work until the
task is accepted; your ledger (`status`) is your record". Contains NO: probe
or submission counts, strategy words, protocol vocabulary ("φ", "primed",
"memory", "cache", "transfer"). A CI lint test asserts the forbidden-term
list; the template's sha256 goes into every run header.

### measure.py

- φ per family: median over later slots' (k≥1) headroom recoveries across
  reps, IQR reported; unprimed trajectory always printed — materially non-flat →
  per-slot deltas section (protocol §5).
- Abort table always beside φ: a φ from 20%-aborted slots is a different
  datum and must read as one.
- Report: `report.md` per campaign + the raw JSONL alongside (numbers
  recomputable).

## Guards (typed, never a hang)

| Guard | Default | Trip → outcome |
| --- | --- | --- |
| slot wall-clock | 45 min | kill process group, `aborted: timeout`, E=0 |
| probe ceiling | 30 | kill, `aborted: probe-ceiling`, E=0 |
| agent exit w/o acceptance | — | `aborted: agent-exit` + transcript tail (50 lines) |
| endpoint preflight | 2 checks | `infra-error` (no rep budget consumed) |
| campaign infra-streak | 3 consecutive | campaign abort, report so far |

## Testing

- **Fakes at the seam**: `FakeRunner` (scripted: accept-after-N / hang /
  exit-nonzero; writes directly into the slot's ledger root). No LLM anywhere
  in CI.
- Slot tests: cold/primed layout invariants, corpus copy, each guard class,
  JSONL schema.
- Driver tests: plan expansion, resume-skip, infra-streak abort, heartbeat.
- measure tests: φ stats over synthetic JSONL; non-flat → deltas section;
  prompt-neutrality lint.
- Integration (CI): mini-campaign, pool=2, stub corpus, FakeRunner
  end-to-end incl. `report.md` rendering.
- Manual gates (NOT CI, off-repo-machine endpoint): smoke campaign checklist
  (checkpoint digest pinned, transcripts readable, trajectories sane) before
  the floor campaign is allowed to be "the number".

## Reporting artifacts

- `docs/benchmark-results/2c/<campaign-id>/` as above.
- After the floor campaign: a live-run addendum in
  `docs/benchmark-protocol.md` §4/§6 recording the first live numbers and
  marking the reference-agent trajectories' role (wiring only) as already
  stated.

## Risks / open items

- opencode v1 vs v2 config syntax for agent tool allowlists may differ; the
  v2 adapter is deferred work, not a v1 blocker.
- A free-running Gemma 4 may fail to solve ANY slot in the smoke run — that
  is a finding about the agent, not the harness; smoke gate exists precisely
  to learn this before burning the floor campaign.
- Endpoint availability (gaming PC asleep/off-LAN) is the single largest
  overnight hazard; preflight + infra-streak abort bounded the blast radius.
