"""Task state: recorded traces, experiments, ledger. On-disk under .reschema/tasks/<task_id>."""

from __future__ import annotations

import json
import random
import secrets
from pathlib import Path

from .driver.spec import Param
from .exec.canonical import canonicalize
from .exec.recorder import record
from .validate.function import N_FUZZ, validate_function
from .validate.program import compile_model, hidden_input_stream, replay_against

# plan said parents[1]; that lands at src/ — engine.py sits at src/reschema/, so root is parents[2]
ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / ".reschema" / "tasks"
MANIFEST = ROOT / ".reschema" / "corpus" / "manifest.json"

HIDDEN_N = 8  # distinct usable hidden inputs each submission must survive


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text())


def _record_stable(binary: str | Path, argv: list[str], stdin: bytes) -> dict | None:
    """Double-record ground truth; canonical trace, or None on flaky divergence."""
    a = canonicalize(record(binary, argv, stdin))
    return a if a == canonicalize(record(binary, argv, stdin)) else None


class TaskStore:
    def __init__(self, task_id: str):
        self.meta = next((t for t in load_manifest() if t["task_id"] == task_id), None)
        if self.meta is None:
            raise KeyError(f"unknown task_id: {task_id}")
        self.dir = TASKS / task_id.replace("::", "__")
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / name

    def record_case(self, label: str, argv: list[str], stdin: bytes) -> dict:
        """Record a ground-truth trace; double-record to catch flakiness."""
        a = _record_stable(self.meta["binary"], argv, stdin)
        if a is None:
            raise RuntimeError(f"task {self.meta['task_id']} flaky on {label}")
        (self._path(f"trace_{label}.json")).write_text(json.dumps(a))
        return a

    def recorded(self) -> list[dict]:
        return [
            json.loads(p.read_text()) for p in sorted(self.dir.glob("trace_*.json"))
        ]

    def ledger(self) -> dict:
        p = self._path("ledger.json")
        return (
            json.loads(p.read_text())
            if p.exists()
            else {"accepted": [], "submissions": 0, "rejections": 0}
        )

    def save_ledger(self, led: dict):
        self._path("ledger.json").write_text(json.dumps(led, indent=2))


# ponytail: 3-seed corpus, manifest-driven input-space deferred
STDIN_DRIVEN = {"check"}  # seed names fed via stdin; all others take argv input


def submit_program(store: TaskStore, c_source: str) -> dict:
    """Anti-hardcoding gate: replay recorded cases, then freshly-recorded hidden ones."""
    rec = store.recorded()
    model = store.dir / "model"
    ok, err = compile_model(c_source, model)
    if not ok:
        return {"accepted": False, "reason": "compile", "detail": err}
    v = replay_against(model, rec)
    if not v.ok:
        return {
            "accepted": False,
            "reason": v.reason,
            "stage": "recorded",
            "divergence": v.divergence,
        }
    modes = ("stdin",) if store.meta["seed"] in STDIN_DRIVEN else ("argv",)
    known = {(tuple(t["argv"][1:]), t["stdin_hex"]) for t in rec}
    # Hidden ground truth: fresh unguessable inputs (new entropy every submission),
    # double-recorded like stored cases. A flaky or crashing draw invalidates the
    # INPUT (redraw), never the model — a crash trace is not a behavior spec.
    rng = random.Random(f"hidden:{store.meta['task_id']}:{secrets.token_hex(16)}")
    fresh = []
    for (argv, stdin), _attempt in zip(
        hidden_input_stream(rng, modes), range(10 * HIDDEN_N), strict=False
    ):
        if len(fresh) == HIDDEN_N:
            break
        key = (tuple(argv), stdin.hex())
        if key in known:
            continue
        known.add(key)  # dedupes both against recorded cases and within the suite
        t = _record_stable(store.meta["binary"], argv, stdin)
        if t is None or t["exit_code"] == -1:
            continue
        fresh.append(t)
    if len(fresh) < HIDDEN_N:
        # Loud failure over vacuous pass: too few distinct usable hidden inputs.
        return {
            "accepted": False,
            "reason": "hidden-starvation",
            "detail": f"{len(fresh)}/{HIDDEN_N} usable hidden inputs after 80 draws",
        }
    v = replay_against(model, fresh)
    if not v.ok:
        return {
            "accepted": False,
            "reason": v.reason,
            "stage": "hidden",
            "divergence": v.divergence,
        }
    led = store.ledger()
    led["accepted"].append("program")
    store.save_ledger(led)
    return {"accepted": True, "replay_pct": 100}


DEFAULT_PARAM_SPECS: dict[str, list[dict]] = {}  # agent supplies; engine passes through


def open_function_task(store: TaskStore, func: str) -> dict:
    from .disasm.slice import disasm_function

    f = store.meta["functions"][func]
    return {
        "task_id": store.meta["task_id"],
        "function": func,
        "address": hex(f["addr"]),
        "disasm": disasm_function(store.meta["binary"], f["addr"], f["size"]),
    }


def experiment_function(store: TaskStore, func: str, params: list[dict], case: dict) -> dict:
    """Ground truth: call the real function once, forward the whole trace."""
    from .driver.calling import call_original

    ps = [Param.from_json(p) for p in params]
    return call_original(store.meta["binary"], store.meta["functions"][func]["addr"], ps, case)


def submit_function(
    store: TaskStore,
    func: str,
    params: list[dict],
    c_source: str,
    seed: int | None = None,
    n_fuzz: int = N_FUZZ,
) -> dict:
    # ret:"void" is agent-supplied and unverifiable from the spec: a mis-declared void
    # with read-only mem can validate a no-op — same trust class as declared directions.
    ps = [Param.from_json(p) for p in params]
    v = validate_function(
        store.meta["binary"],
        store.meta["functions"][func]["addr"],
        func,
        ps,
        c_source,
        store.dir / f"{func}.so",
        seed=seed,
        n_fuzz=n_fuzz,
    )
    led = store.ledger()
    led["submissions"] += 1
    if not v.ok:
        led["rejections"] += 1
        store.save_ledger(led)
        return {"accepted": False, "divergence": v.divergence}
    if not any(isinstance(f, dict) and func in f for f in led["accepted"]):
        led["accepted"].append({func: c_source})
    store.save_ledger(led)
    return {"accepted": True}


def compose(store: TaskStore) -> tuple[bool, str]:
    """Concatenate accepted function sources; compile-check as one program model."""
    led = store.ledger()
    src = "\n".join(next(iter(f.values())) for f in led["accepted"] if isinstance(f, dict))
    if not src:
        return False, "#error nothing accepted"
    ok, err = compile_model(
        src + "\n#include <stdio.h>\nint main(void){return 0;}\n",
        store.dir / "composed",
    )
    return ok, err
