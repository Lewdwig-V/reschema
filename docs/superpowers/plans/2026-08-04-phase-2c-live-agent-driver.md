# Phase 2C Live-Agent Transfer Driver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tools/dogfood/` — the scripted driver that runs the transfer protocol with a live agent (opencode v1 + Gemma 4) per the approved spec.

**Architecture:** Slot-runner atom (`slot.py`: layout → spawn → supervise → one JSONL) composed by a thin TOML-driven campaign runner (`driver.py`); harness adapters behind `AgentRunner` (`runners/base.py`, `runners/opencode_v1.py`); statistics in `measure.py`; CI-safe via `FakeRunner` only.

**Tech Stack:** Python 3.12 stdlib only (tomllib, dataclasses, concurrent.futures, subprocess); pytest; ruff-clean.

**Spec:** `docs/superpowers/specs/2026-08-04-phase-2c-live-agent-driver-design.md`
**Deviation from spec:** campaign files are TOML via stdlib `tomllib`, not YAML (pyyaml is not a project dependency; won't be added for two config files).

---

### Task 1: Package skeleton + core dataclasses + AgentRunner interface

**Files:**
- Create: `tools/__init__.py`, `tools/dogfood/__init__.py`, `tools/dogfood/runners/__init__.py`
- Create: `tools/dogfood/runners/base.py`
- Modify: `pyproject.toml` (add `pythonpath` under pytest ini)
- Test: `tests/dogfood2c/test_types.py` (create `tests/dogfood2c/__init__.py` empty)

pytest needs the repo root on `sys.path` to import `tools.*`. Exact edit to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 1: failing test**

```python
# tests/dogfood2c/test_types.py
from tools.dogfood.runners.base import AgentOutcome, RunnerConfig, SlotSpec


def test_slot_spec_shapes():
    s = SlotSpec(
        family="rot13", condition="primed", slot="gcc-O0-sym",
        slot_index=0, rep=1, task_id="rot13::gcc-O0-sym",
    )
    assert s.condition in ("primed", "unprimed")
    assert s.slot in s.task_id


def test_agent_outcome_kinds():
    o = AgentOutcome(exit_kind="eof", returncode=0, transcript_tail="done")
    assert o.exit_kind in ("eof", "exit", "timeout", "error")
```

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_types.py -q`
Expected: FAIL — `ModuleNotFoundError: tools`

- [ ] **Step 3: minimal implementation**

`tools/dogfood/runners/base.py`:

```python
"""Driver-side types + the harness-adapter interface (spec §runners/base)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class SlotSpec:
    """One live-agent run of one corpus slot in one condition."""

    family: str          # seed name, e.g. "rot13"
    condition: str       # "primed" | "unprimed"
    slot: str            # e.g. "gcc-O1-sym"
    slot_index: int      # 0..2 position in the chain
    rep: int
    task_id: str         # "<family>::<slot>"

    @property
    def slot_id(self) -> str:
        return f"{self.family}-{self.condition}-{self.slot}-r{self.rep}"

    @property
    def result_stem(self) -> str:
        """SINGLE owner of the result-file naming rule: primed chains share
        slot_id across their 3 slots, so later slots disambiguate by index."""
        return (
            f"{self.slot_id}-s{self.slot_index}"
            if self.condition == "primed"
            else self.slot_id
        )


@dataclass
class RunnerConfig:
    model: str
    endpoint: str | None       # OpenAI-compatible base URL (run-header evidence)
    sandbox: Path              # empty session cwd (agent cannot see the repo)
    run_root: Path             # slot's RESCHEMA_HOME
    mcp_server_args: list[str] = field(default_factory=list)
    max_turns: int | None = None


@dataclass
class AgentOutcome:
    exit_kind: str   # "eof" | "exit" | "timeout" | "error"
    returncode: int | None
    transcript_tail: str  # last ~50 lines


class AgentRunner(Protocol):
    """Harness adapter surface. Core code sees nothing harness-shaped."""

    def prepare(self, cfg: RunnerConfig) -> None: ...
    def spawn(self, prompt: str) -> None: ...
    def wait(self) -> AgentOutcome: ...   # must return promptly after kill()
    def kill(self) -> None: ...
    def exited(self) -> bool: ...  # agent process finished (conservative False ok)
```

`__init__.py` files: empty.

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_types.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools pyproject.toml tests/dogfood2c && uv lock
git commit -m "2C: dogfood driver skeleton + AgentRunner interface"
```

---

### Task 2: prompt.py — neutral template + neutrality lint test

**Files:**
- Create: `tools/dogfood/prompt.py`
- Test: `tests/dogfood2c/test_prompt.py`

- [ ] **Step 1: failing test**

```python
# tests/dogfood2c/test_prompt.py
import hashlib

from tools.dogfood.prompt import FORBIDDEN_TERMS, render, template_hash


def test_render_is_neutral_and_mentions_only_the_task():
    p = render("rot13::gcc-O1-sym")
    assert "rot13::gcc-O1-sym" in p
    low = p.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"prompt coaches the agent: {term!r}"


def test_template_hash_stable_and_tied_to_content():
    assert template_hash() == hashlib.sha256(render("X").encode()).hexdigest()
```

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_prompt.py -q`
Expected: FAIL — no `tools.dogfood.prompt`

- [ ] **Step 3: minimal implementation**

```python
"""The ONE neutral task prompt. Protocol words are contamination: a prompt
that coaches probe/submission strategy is solver scaffolding (#63 class) and
invalidates the free-run measurement. The forbidden list is lint-tested."""

from __future__ import annotations

import hashlib

FORBIDDEN_TERMS = [
    "primed", "unprimed", "cache", " φ", "phi", "transfer protocol",
    "benchmark", "protocol", "hidden", "6 probes", "submissions",
]

_TEMPLATE = """You are given exactly one binary-analysis task: "{task_id}".

Work only through the tools provided to you. Open the task, learn its
contract, and work until the engine accepts your model. Your ledger is your
record of what you have tried.

When the task is accepted, stop. Do not take any further actions.
"""


def render(task_id: str) -> str:
    return _TEMPLATE.format(task_id=task_id)


def template_hash() -> str:
    return hashlib.sha256(render("X").encode()).hexdigest()
```

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_prompt.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/prompt.py tests/dogfood2c/test_prompt.py
git commit -m "2C: neutral task prompt + neutrality lint"
```

---

### Task 3: measure.py — efficiency + φ statistics (pure functions)

**Files:**
- Create: `tools/dogfood/measure.py`
- Test: `tests/dogfood2c/test_measure.py`

Engine constants for E come from `reschema.engine.E_ALPHA/E_BETA` — the driver imports the judge, never re-implements it.

- [ ] **Step 1: failing test**

```python
# tests/dogfood2c/test_measure.py
import pytest

from tools.dogfood.measure import phi_family, slot_efficiency


def test_slot_efficiency_matches_reference_arithmetic():
    # 6 probes + 1 submission, accepted -> exp(-0.75) (protocol §4 reference point)
    e = slot_efficiency(True, 6, 1)
    assert e == pytest.approx(0.4723665, abs=1e-6)
    assert slot_efficiency(True, 0, 1) == pytest.approx(1.0)   # primed reuse
    assert slot_efficiency(False, 6, 2) == 0.0                 # unaccepted


def test_phi_family_median_and_flat_check():
    recs = [
        {"condition": "primed", "slot_index": 1, "accepted": True, "n_exp": 0, "n_sub": 1},
        {"condition": "primed", "slot_index": 2, "accepted": True, "n_exp": 0, "n_sub": 1},
        {"condition": "unprimed", "slot_index": 0, "accepted": True, "n_exp": 6, "n_sub": 1},
        {"condition": "unprimed", "slot_index": 1, "accepted": True, "n_exp": 6, "n_sub": 1},
        {"condition": "unprimed", "slot_index": 2, "accepted": True, "n_exp": 6, "n_sub": 1},
    ]
    r = phi_family(recs)
    assert r["unprimed_flat"] is True
    assert r["phi_median"] == pytest.approx(1.0)
    assert r["n_deltas"] == 2  # one per later slot
```

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_measure.py -q`
Expected: FAIL — no module

- [ ] **Step 3: minimal implementation**

```python
"""Statistics over slot JSONL records. The only place numbers are computed;
slot/driver code stays dumb."""

from __future__ import annotations

import math
import statistics

from reschema.engine import E_ALPHA, E_BETA

FLAT_EPS = 1e-3  # "materially non-flat" threshold (protocol §5)


def slot_efficiency(accepted: bool, probes: int, subs: int) -> float:
    """E = accepted * exp(-(alpha*max(0,probes-1) + beta*(subs-1))) — engine's formula."""
    return (
        math.exp(-(E_ALPHA * max(0, probes - 1) + E_BETA * max(0, subs - 1)))
        if accepted
        else 0.0
    )


def phi_family(records: list[dict]) -> dict:
    """Median headroom recovery over later slots vs slot-0 unprimed baseline."""
    up = [r for r in records if r["condition"] == "unprimed" and r["accepted"]]
    pr = [r for r in records if r["condition"] == "primed" and r["accepted"]]
    up_e = {r["slot_index"]: slot_efficiency(True, r["n_exp"], r["n_sub"]) for r in up}
    base0 = next((v for k, v in up_e.items() if k == 0), None)
    headroom = 1.0 - base0 if base0 is not None else None
    deltas, phis = [], []
    if headroom:
        for r in pr:
            if r["slot_index"] == 0:
                continue
            e = slot_efficiency(True, r["n_exp"], r["n_sub"])
            deltas.append(
                {
                    "slot_index": r["slot_index"],
                    "primed_e": e,
                    "unprimed_e": up_e.get(r["slot_index"]),
                    "delta": e - up_e.get(r["slot_index"], 0.0),
                }
            )
            phis.append((e - base0) / headroom)
    flat = len({round(v, 6) for v in up_e.values()}) == 1 or (
        up_e and max(up_e.values()) - min(up_e.values()) < FLAT_EPS
    )
    return {
        "phi_median": statistics.median(phis) if phis else None,
        "phi_iqr": (
            (statistics.quantiles(phis, n=4)[2] - statistics.quantiles(phis, n=4)[0])
            if len(phis) >= 4
            else None
        ),
        "unprimed_flat": flat,
        "unprimed_traj": [up_e[k] for k in sorted(up_e)],
        "deltas": deltas,
        "n_deltas": len(deltas),
    }
```

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_measure.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/measure.py tests/dogfood2c/test_measure.py
git commit -m "2C: efficiency + φ statistics over slot records"
```

---

### Task 4: slot.py — root layout + lifecycle + guards + JSONL emission

**Files:**
- Create: `tools/dogfood/slot.py`
- Test: `tests/dogfood2c/fakes.py`, `tests/dogfood2c/test_slot.py`

`FakeRunner` (lives in tests — test double, not shipped tooling). Semantics that make the slot loop observable: `spawn()` performs the scripted agent ACTIVITY (ledger writes happen at spawn, so run_slot's poll loop sees them); `wait()` is process teardown only and returns immediately after `kill()`.

```python
# tests/dogfood2c/fakes.py
import json
import time

from tools.dogfood.runners.base import AgentOutcome


class FakeRunner:
    """Scripted AgentRunner: the ledger state is written at spawn (the
    agent's whole scripted life), or never (hang) — kill() wakes wait()."""

    def __init__(self, script: dict):
        self.script = script
        self.cfg = None
        self._killed = False

    def prepare(self, cfg):
        self.cfg = cfg
        self._killed = False

    def spawn(self, prompt):
        if self.script.get("ledger") is not None:
            import os

            os.environ["RESCHEMA_HOME"] = str(self.cfg.run_root)  # read by TaskStore
            from reschema.engine import TaskStore

            st = TaskStore(self.script["task_id"])
            st._path("ledger.json").write_text(json.dumps(self.script["ledger"]))

    def exited(self):
        return self._killed or self.script.get("ledger") is not None

    def wait(self):
        while not self._killed:
            if self.script.get("ledger") is not None:
                return AgentOutcome(exit_kind="eof", returncode=0,
                                    transcript_tail="fake")
            time.sleep(0.05)
        return AgentOutcome(exit_kind="timeout", returncode=-9,
                            transcript_tail="killed")

    def kill(self):
        self._killed = True
```

The failure class this models: a hung agent writes NO ledger activity, so the run_slot guard loop (ledger polling + deadlines) is what trips — matching the opencode adapter's kill path. Note the script dict grows a required `"task_id"` key matching the SlotSpec's.

- [ ] **Step 1: failing tests**

```python
# tests/dogfood2c/test_slot.py
import json

from tools.dogfood.slot import SlotGuard, layout_root, run_slot
from tools.dogfood.runners.base import SlotSpec

from .fakes import FakeRunner


def _spec(cond="unprimed", idx=0):
    return SlotSpec(family="rot13", condition=cond, slot="gcc-O0-sym",
                    slot_index=idx, rep=1, task_id="rot13::gcc-O0-sym")


def test_unprimed_layout_is_cold_and_copied_corpus(tmp_path, stub_corpus):
    root = layout_root(_spec(), tmp_path / "runs", stub_corpus)
    assert (root / ".reschema/corpus/manifest.json").exists()
    assert not (root / ".reschema/memory").exists()  # cold at slot open


def test_primed_chain_shares_memory_root(tmp_path, stub_corpus):
    a = layout_root(_spec("primed", 0), tmp_path / "runs", stub_corpus)
    b = layout_root(_spec("primed", 1), tmp_path / "runs", stub_corpus)
    assert a == b  # same root: slot 0's verified_fact must be visible at slot 1


def test_run_slot_accepted_emits_jsonl(tmp_path, stub_corpus):
    script = {"task_id": "rot13::gcc-O0-sym",
              "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3}}
    out = run_slot(_spec(), campaign_dir=tmp_path / "runs",
                   runner=FakeRunner(script), corpus_source=stub_corpus,
                   guards=SlotGuard(timeout_s=30, probe_ceiling=5), poll_s=0)
    rec = json.loads(out.read_text())
    assert rec["outcome"] == "accepted" and rec["n_exp"] == 3
    assert rec["E"] > 0 and rec["slot_id"].endswith("r1")


def test_run_slot_timeout_is_typed_abort(tmp_path, stub_corpus):
    out = run_slot(_spec(), campaign_dir=tmp_path / "runs",
                   runner=FakeRunner({"task_id": "rot13::gcc-O0-sym",
                                      "hang": True, "ledger": None}),
                   corpus_source=stub_corpus,
                   guards=SlotGuard(timeout_s=1, probe_ceiling=99), poll_s=0)
    assert json.loads(out.read_text())["outcome"] == "aborted: timeout"
```

`tests/dogfood2c/conftest.py`:

```python
import json

import pytest


@pytest.fixture
def stub_corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps([{"task_id": "rot13::gcc-O0-sym"}]))
    return d
```

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_slot.py -q`
Expected: FAIL — no `tools.dogfood.slot`

- [ ] **Step 3: minimal implementation**

```python
"""One live-agent run of one corpus slot (spec §slot.py)."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .measure import slot_efficiency
from .prompt import render, template_hash
from .runners.base import AgentRunner, RunnerConfig, SlotSpec

POLL_S = 5  # ledger poll cadence (spec said 30s; tighter for test speed — config below)


@dataclass
class SlotGuard:
    timeout_s: int = 2700      # 45 min
    probe_ceiling: int = 30
    poll_s: int = 30


def layout_root(spec: SlotSpec, runs_dir: Path, corpus_source: Path) -> Path:
    """Primed chains share one root across slots; unprimed gets a fresh root
    per slot — memory-cold-by-filesystem, the CI isolation invariant."""
    chain = (
        f"{spec.family}-primed-r{spec.rep}"
        if spec.condition == "primed"
        else f"{spec.family}-{spec.condition}-{spec.slot}-r{spec.rep}"
    )
    root = runs_dir / chain
    corp = root / ".reschema/corpus"
    if not corp.exists():
        corp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(corpus_source, corp)
    (root / ".reschema/tasks").mkdir(parents=True, exist_ok=True)
    return root


def _read_ledger(root: Path, task_id: str) -> dict:
    p = root / ".reschema/tasks" / task_id.replace("::", "__") / "ledger.json"
    return json.loads(p.read_text()) if p.exists() else {}


def run_slot(
    spec: SlotSpec,
    *,
    campaign_dir: Path,
    runner: AgentRunner,
    corpus_source: Path,
    guards: SlotGuard | None = None,
    poll_s: int | None = None,
    run_header: dict | None = None,
) -> Path:
    guards = guards or SlotGuard()
    poll = poll_s if poll_s is not None else guards.poll_s
    root = layout_root(spec, campaign_dir, corpus_source)
    out = campaign_dir.parent / "results" / f"{spec.result_stem}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    cfg = RunnerConfig(
        model=(run_header or {}).get("model", "unknown"),
        endpoint=(run_header or {}).get("endpoint"),
        sandbox=root / "sandbox", run_root=root,
    )
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    run_header = run_header or {}
    preflight = getattr(runner, "preflight", None)
    if preflight is not None:
        try:
            run_header = {**run_header, **preflight(cfg)}
        except RuntimeError as e:
            rec = {
                "slot_id": spec.slot_id, "family": spec.family,
                "condition": spec.condition, "slot": spec.slot,
                "slot_index": spec.slot_index, "rep": spec.rep,
                "outcome": "infra-error", "abort_reason": str(e), "E": 0.0,
                "n_exp": 0, "n_sub": 0, "accepted": False, "wall_s": 0.0,
                "run_header": run_header,
            }
            out.write_text(json.dumps(rec) + "\n")
            return out
    runner.prepare(cfg)
    started = time.monotonic()
    runner.spawn(render(spec.task_id))

    outcome, abort = "aborted: agent-exit", None
    while True:
        led = _read_ledger(root, spec.task_id)
        probes = led.get("probes", 0)
        print(f"[{spec.result_stem}] {time.monotonic()-started:.0f}s probes={probes}", flush=True)  # heartbeat
        if "program" in led.get("accepted", []):
            outcome = "accepted"
            break
        if runner.exited():  # natural exit without acceptance: typed below
            outcome = "aborted: agent-exit"
            break
        if time.monotonic() - started > guards.timeout_s:
            runner.kill()
            outcome, abort = "aborted: timeout", "wall-clock guard"
            break
        if probes > guards.probe_ceiling:
            runner.kill()
            outcome, abort = "aborted: probe-ceiling", f"probes>{guards.probe_ceiling}"
            break
        time.sleep(poll)
    res = runner.wait()  # after kill() or natural exit; must return promptly
    led = _read_ledger(root, spec.task_id)
    accepted = "program" in led.get("accepted", [])
    if accepted:
        outcome = "accepted"
    subs, probes = led.get("submissions", 0), led.get("probes", 0)
    rec = {
        "slot_id": spec.slot_id,
        "family": spec.family,
        "condition": spec.condition,
        "slot": spec.slot,
        "slot_index": spec.slot_index,
        "rep": spec.rep,
        "outcome": outcome,
        "abort_reason": abort,
        "E": slot_efficiency(accepted, probes, subs),
        "n_exp": probes,
        "n_sub": subs,
        "accepted": accepted,
        "wall_s": round(time.monotonic() - started, 1),
        "run_header": {
            **(run_header or {}),
            "prompt_sha256": template_hash(),
            "agent_exit": res.exit_kind,
        },
    }
    out.write_text(json.dumps(rec) + "\n")
    return out
```

Contract note for the implementer: `runner.wait()` MUST return promptly once `kill()` has been issued (opencode adapter: process-group SIGKILL, then a short `communicate`). Natural agent exit without acceptance breaks via `runner.exited()` as `aborted: agent-exit` — the loop never waits out a guard for a dead process.

- [ ] **Step 4: run, watch tests pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_slot.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/slot.py tests/dogfood2c/{fakes,conftest,test_slot}.py
git commit -m "2C: slot runner — layout, guards, JSONL emission"
```

---

### Task 5: runners/opencode_v1.py — session config + process lifecycle

**Files:**
- Create: `tools/dogfood/runners/opencode_v1.py`
- Test: `tests/dogfood2c/test_opencode_v1.py`

No real opencode in CI: the adapter takes `binary=` (default `"opencode"`); tests point it at a stub shell script that sleeps/exits on demand.

- [ ] **Step 1: failing test**

```python
# tests/dogfood2c/test_opencode_v1.py
import json
import time

from tools.dogfood.runners.base import RunnerConfig
from tools.dogfood.runners.opencode_v1 import OpenCodeV1Runner


def _cfg(tmp_path):
    return RunnerConfig(model="gemma4", endpoint="http://lan:11434/v1",
                        sandbox=tmp_path / "sb", run_root=tmp_path / "root")


def test_session_config_allowlists_exactly_five_tools(tmp_path):
    r = OpenCodeV1Runner(binary="/bin/true")
    r.prepare(_cfg(tmp_path))
    cfg = json.loads((tmp_path / "sb/opencode.json").read_text())
    tools = cfg["agent"]["tools"]
    assert all(v is False for k, v in tools.items() if k != "reschema_*")
    assert cfg["mcp"]["reschema"]["type"] == "local"
    assert cfg["model"].endswith("gemma4")


def test_kill_makes_wait_return(tmp_path):
    r = OpenCodeV1Runner(binary="/bin/sleep")
    cfg = _cfg(tmp_path)
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    r.prepare(cfg)
    r.spawn("p")
    r.kill()
    assert r.wait().exit_kind in ("timeout", "exit")


def test_preflight_reports_endpoint_facts(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    fake = {"models": [{"id": "gemma4"}], "version": "fake-stack-1.0"}
    monkeypatch.setattr(r, "_post", lambda path, payload: (200, fake))
    info = r.preflight(_cfg(tmp_path))
    assert info["model"] == "gemma4" and info["digest"] == "fake-stack-1.0"
```

Note: Task 5's adapter asserts the allowlist shape generically (whatever key namespace opencode v1 uses for MCP tools — recorded as `"reschema_*"` wildcard); the exact v1 config keys (`agent.tools`, `mcp.<name>.type:"local"`) are pinned by this test, so a future config-format change fails loudly, not silently.

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_opencode_v1.py -q`
Expected: FAIL — no adapters module

- [ ] **Step 3: minimal implementation**

```python
"""opencode v1 adapter: writes the sandbox config, spawns `opencode run`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from .base import AgentOutcome, RunnerConfig

TRANSCRIPT = "transcript.log"


class OpenCodeV1Runner:
    def __init__(self, binary: str = "opencode"):
        self.binary = binary
        self._p: subprocess.Popen | None = None
        self._cfg: RunnerConfig | None = None

    def preflight(self, cfg: RunnerConfig) -> dict:
        """Endpoint liveness + run-header facts; raises on a dead endpoint
        (slot maps that to a typed infra-error, no rep consumed)."""
        base = (cfg.endpoint or "").rstrip("/")
        status, models = self._post(base, "/models", None)
        status2, _ = self._post(base, "/chat/completions",
                                {"max_tokens": 1, "model": cfg.model})
        if status != 200 or status2 != 200:
            raise RuntimeError(f"endpoint unavailable: {status}/{status2}")
        return {"model": cfg.model, "endpoint": cfg.endpoint,
                "digest": models.get("version", "unknown")}

    def _post(self, base: str, path: str, payload) -> tuple[int, dict]:
        """urllib json POST to base+path; (status, decoded body or {})."""
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            base + path,
            data=_json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read() or b"{}")
        except Exception:
            return 0, {}

    def prepare(self, cfg: RunnerConfig) -> None:
        (cfg.sandbox).mkdir(parents=True, exist_ok=True)
        (cfg.sandbox / "opencode.json").write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "model": f"local/{cfg.model}",
            "provider": {"local": {"npm": "@ai-sdk/openai-compatible",
                                   "options": {"baseURL": cfg.endpoint}}},
            "agent": {"tools": {
                "bash": False, "edit": False, "write": False, "read": False,
                "glob": False, "grep": False, "webfetch": False, "task": False,
                "skill": False, "todowrite": False, "question": False,
                "reschema_*": True,
            }},
            "mcp": {"reschema": {
                "type": "local",
                "command": ["uv", "run", "python", "-m", "reschema.mcp.server"],
                "environment": {"RESCHEMA_HOME": str(cfg.run_root)},
            }},
        }, indent=2))
        self._cfg = cfg

    def spawn(self, prompt: str) -> None:
        cfg = self._cfg
        out = open(cfg.sandbox / TRANSCRIPT, "wb")
        self._p = subprocess.Popen(
            [self.binary, "run", prompt],
            cwd=cfg.sandbox, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group: kill reaches children
        )

    def wait(self) -> AgentOutcome:
        p = self._p
        rc = p.wait() if p else None
        tail = ""
        tp = self._cfg.sandbox / TRANSCRIPT
        if tp.exists():
            tail = "\n".join(tp.read_text(errors="replace").splitlines()[-50:])
        kind = "eof" if rc == 0 else ("timeout" if rc and rc < 0 else "exit")
        return AgentOutcome(exit_kind=kind, returncode=rc, transcript_tail=tail)

    def kill(self) -> None:
        if self._p and self._p.poll() is None:
            os.killpg(self._p.pid, signal.SIGKILL)
```

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_opencode_v1.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/runners/opencode_v1.py tests/dogfood2c/test_opencode_v1.py
git commit -m "2C: opencode v1 runner adapter"
```

---

### Task 6: driver.py — TOML campaigns, pool, resume, infra-streak

**Files:**
- Create: `tools/dogfood/driver.py`, `tools/dogfood/campaigns/smoke.toml`, `tools/dogfood/campaigns/floor.toml`
- Test: `tests/dogfood2c/test_driver.py`

- [ ] **Step 1: failing tests**

```python
# tests/dogfood2c/test_driver.py
import json

from tools.dogfood.driver import expand_campaign, run_campaign

SMOKE = """
[[chains]]
family = "rot13"
slots = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]
reps = 1
"""


def test_expand_campaign_counts_and_conditions(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    specs = expand_campaign(cfg)
    assert len(specs) == 2 * 3          # 2 conditions × 3 slots
    assert {s.condition for s in specs} == {"primed", "unprimed"}


def test_run_campaign_resumes_by_existing_jsonl(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    from .fakes import FakeRunner

    def mk():
        return FakeRunner({"ledger": {"accepted": ["program"],
                                      "submissions": 1, "probes": 3}})

    done = tmp_path / "out/rot13-unprimed-gcc-O0-sym-r1.jsonl"
    done.parent.mkdir(parents=True)
    done.write_text(json.dumps({"slot_id": "x"}) + "\n")
    rc = run_campaign(cfg, runner_factory=mk, corpus_source=stub_corpus,
                      pool_size=2, out_dir=tmp_path / "out")
    assert rc == 0
    assert len(list((tmp_path / "out").glob("*.jsonl"))) == 6
    assert json.loads(done.read_text())["slot_id"] == "x"  # untouched: skipped
```

- [ ] **Step 2: run, watch it fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_driver.py -q`
Expected: FAIL

- [ ] **Step 3: minimal implementation**

```python
"""Campaign orchestration: TOML plan -> slot specs -> bounded pool -> JSONL."""

from __future__ import annotations

import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .runners.base import AgentRunner, SlotSpec
from .slot import SlotGuard, run_slot

INFRA_STREAK_ABORT = 3


def expand_campaign(cfg_path: Path) -> list[SlotSpec]:
    doc = tomllib.loads(Path(cfg_path).read_text())
    specs = []
    for chain in doc["chains"]:
        for rep in range(1, chain.get("reps", 5) + 1):
            for cond in ("unprimed", "primed"):
                for i, slot in enumerate(chain["slots"]):
                    specs.append(SlotSpec(
                        family=chain["family"], condition=cond, slot=slot,
                        slot_index=i, rep=rep,
                        task_id=f"{chain['family']}::{slot}",
                    ))
    return specs


def run_campaign(
    cfg_path: Path,
    *,
    runner_factory: "callable[[], AgentRunner]",
    corpus_source: Path,
    pool_size: int = 4,
    out_dir: Path,
    guards: SlotGuard | None = None,
    poll_s: int | None = None,
) -> int:
    specs = expand_campaign(cfg_path)
    todo = [s for s in specs if not (out_dir / f"{s.result_stem}.jsonl").exists()]
    run_header = {  # env-driven at campaign start; recorded in every slot record
        "model": os.environ.get("RESCHEMA_2C_MODEL", "gemma4"),
        "endpoint": os.environ.get("RESCHEMA_2C_ENDPOINT"),
    }
    infra_streak = 0

    def one(spec):
        nonlocal infra_streak
        out = run_slot(spec, campaign_dir=out_dir.parent / "runs",
                       runner=runner_factory(), corpus_source=corpus_source,
                       guards=guards, poll_s=poll_s, run_header=run_header)
        import json
        o = json.loads(out.read_text())["outcome"]
        infra_streak = infra_streak + 1 if o == "infra-error" else 0
        if infra_streak >= INFRA_STREAK_ABORT:
            raise RuntimeError("endpoint infra-error streak: aborting campaign")
        return out

    with ThreadPoolExecutor(max_workers=pool_size) as ex:
        list(ex.map(one, todo))
    return 0
```

Note: primed chains run their 3 slots sequentially within one worker task in the real flow (chain-shared root); the driver-level version above maps each slot flush independently — for the implementation, group primed chains: a primed chain becomes ONE worker task running its slots in order in the shared root; unprimed slots are independent. Keep this note when writing the final code: primed-chain grouping is REQUIRED correctness (slot 1+ reads slot 0's verified_fact). The `expand_campaign` shape stays; `run_campaign` coalesces `primed` specs of the same family+rep into one sequentially-run task.

`tools/dogfood/campaigns/smoke.toml`:

```toml
[[chains]]
family = "rot13"
slots = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]
reps = 1
```

`tools/dogfood/campaigns/floor.toml`:

```toml
[[chains]]
family = "rot13"
slots = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]
reps = 5

[[chains]]
family = "check"
slots = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]
reps = 5
```

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/test_driver.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/driver.py tools/dogfood/campaigns tests/dogfood2c/test_driver.py
git commit -m "2C: campaign runner — TOML plans, pool, resume, infra-streak"
```

---

### Task 7: measure.render_report + end-to-end integration test

**Files:**
- Modify: `tools/dogfood/measure.py` (add `render_report`)
- Create: `tests/dogfood2c/test_integration.py`
- Create: `docs/benchmark-results/2c/.gitkeep`
- Modify: `.gitignore` (add `.dogfood/`)

- [ ] **Step 1: failing test**

```python
# tests/dogfood2c/test_integration.py
import json

from tools.dogfood.driver import run_campaign
from tools.dogfood.measure import render_report
from tools.dogfood.slot import SlotGuard

from .fakes import FakeRunner


def test_mini_campaign_end_to_end(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[chains]]\nfamily="rot13"\nslots=["gcc-O0-sym","gcc-O1-sym"]\nreps=1\n')

    def mk():
        return FakeRunner({"ledger": {"accepted": ["program"],
                                      "submissions": 1, "probes": 4}})

    rc = run_campaign(cfg, runner_factory=mk, corpus_source=stub_corpus,
                      pool_size=2, out_dir=tmp_path / "res",
                      guards=SlotGuard(timeout_s=30), poll_s=0)
    assert rc == 0
    md = render_report(tmp_path / "res", family="rot13", out_dir=tmp_path / "res")
    text = md.read_text()
    assert "unprimed" in text and "primed" in text
    assert "φ" in text or "phi" in text
```

Also dynamic add to `measure.py` needs test for render:

```python
# in tests/dogfood2c/test_measure.py (append)

def test_render_report_writes_markdown(tmp_path):
    (tmp_path / "rot13-unprimed-gcc-O0-sym-r1.jsonl").write_text(
        '{"slot_id":"a","family":"rot13","condition":"unprimed","slot_index":0,'
        '"rep":1,"outcome":"accepted","E":0.5,"n_exp":6,"n_sub":1,
        "accepted":true,"wall_s":1.0,"run_header":{}}\n')
    md = render_report(tmp_path, family="rot13", out_dir=tmp_path)
    assert "rot13" in md.read_text()
```

- [ ] **Step 2: run, watch both fail**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/ -q`
Expected: FAIL — no render_report

- [ ] **Step 3: minimal implementation**

Append to `tools/dogfood/measure.py`:

```python
import json
from pathlib import Path


def _load(results_dir: Path, family: str) -> list[dict]:
    return [
        json.loads(p.read_text().splitlines()[0])
        for p in sorted(results_dir.glob(f"{family}-*.jsonl"))
    ]


def render_report(results_dir: Path, *, family: str, out_dir: Path) -> Path:
    recs = _load(results_dir, family)
    accepted = [r for r in recs if r.get("accepted")]
    aborted = [r for r in recs if r.get("outcome", "").startswith("aborted")]
    stats = phi_family(accepted)
    lines = [
        f"# 2C live-agent campaign — {family}",
        "",
        f"- slots: {len(recs)} total, {len(accepted)} accepted, {len(aborted)} aborted",
        f"- φ median: {stats['phi_median']}  IQR: {stats['phi_iqr']}",
        f"- unprimed trajectory: {stats['unprimed_traj']} (flat: {stats['unprimed_flat']})",
        "",
        "## per-slot deltas" if not stats["unprimed_flat"] else "## primed headroom recoveries",
        "",
        "*reference-agent trajectories are instrument-wiring checks (protocol §4); these rows are the measurement.*",
    ]
    md = out_dir / "report.md"
    md.write_text("\n".join(lines) + "\n")
    return md
```

`.gitignore` add line: `.dogfood/`

- [ ] **Step 4: run, watch it pass**

Run: `timeout -k 10 130 uv run pytest tests/dogfood2c/ -q`
Expected: PASS (all dogfood2c tests)

- [ ] **Step 5: commit**

```bash
git add tools/dogfood/measure.py tests/dogfood2c .gitignore docs/benchmark-results
git commit -m "2C: report renderer + end-to-end campaign test"
```

---

### Task 8: Docs sync + gate

**Files:**
- Modify: `README.md` (Layout block: add tools/dogfood line), `AGENTS.md` (Conventions: note tools/ is benchmark tooling; dogsfood tests live under tests/dogfood2c)
- Modify: `docs/roadmap.md` (2C entry: driver exists; smoke gate pending; floor campaign pending)

- [ ] **Step 1: edits**

README layout block append after `mcp/server.py` line:

```
tools/dogfood/       # phase-2C live-agent transfer driver (not in the package)
```

AGENTS.md conventions append bullet:

```
- `tools/dogfood/` is benchmark tooling (phase 2C), NOT shipped in the
  `reschema` package; its tests live in `tests/dogfood2c/` and must stay
  CI-safe (no LLM, no podman, no endpoint).
```

docs/roadmap.md §2C: add "Driver specced/planned (2026-08); smoke gate pending; floor campaign pending."

- [ ] **Step 2: gates**

```bash
uv run ruff check src tests tools
timeout -k 10 130 uv run pytest -q -n auto
```
Expected: ruff clean; full suite green within budget.

- [ ] **Step 3: commit + PR**

```bash
git add README.md AGENTS.md docs/roadmap.md
git commit -m "2C: document the driver in repo docs"
git push -u origin feat/2c-driver
gh pr create --title "Phase 2C: live-agent transfer driver (tools/dogfood)" --body "..."
```

PR body lists: spec link, deviation (TOML), the AgentRunner contract, what's CI-covered (FakeRunner end-to-end) vs what is manual-gate (real opencode+Gemma4 smoke), the primed-chain sequential constraint note, and the out-of-scope list (no engine/metric changes).

---

## Self-review notes (from plan author)

- Type consistency: `SlotSpec` carries `slot_id` property; `slot.py` and `driver.py/_result_name` share the primed-suffix convention — keep `_result_name` the single owner: move the suffix logic from `slot.py` into a helper `result_name(spec)` in `driver.measure`-free shared spot (`tools/dogfood/naming.py` if it saves a second definition; otherwise one private copy in `driver.py` imported by slot.py — decide at implementation, keep ONE owner).
- `poll_s` override exists because CI tests can't wait 30s; production default stays in `SlotGuard`.
- spec requirement "heartbeat log" → implemented in Task 6 as `one(...)` logging per completed slot; per-minute heartbeats per in-flight slot are a print in `run_slot`'s poll loop (add one line when implementing Task 4 step 3: `print(f"[{slot_id}] {elapsed:.0f}s probes={probes}", flush=True)`).
