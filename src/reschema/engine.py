"""Task state: recorded traces, experiments, ledger. On-disk under .reschema/tasks/<task_id>.

Ledger writes are atomic (temp file + os.replace) but otherwise uncoordinated:
single-process (or out-of-band serialized) access is assumed.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
from pathlib import Path

from .driver import podrun
from .driver.spec import Param
from .exec.canonical import canonicalize
from .exec.recorder import record
from .validate.function import N_FUZZ, validate_function
from .validate.program import compile_model, hidden_input_stream, replay_against

# plan said parents[1]; that lands at src/ — engine.py sits at src/reschema/, so root is parents[2]
# ponytail: correct for src-layout dev runs; pip-installed this lands under
# site-packages (upgrade: platformdirs/importlib.resources when packaging matters)
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
        man = load_manifest()
        self.meta = next((t for t in man if t["task_id"] == task_id), None)
        if self.meta is None:
            raise KeyError(
                f"unknown task_id: {task_id}; available: {[t['task_id'] for t in man]}"
            )
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
        tmp = self._path(".ledger.json.tmp")
        tmp.write_text(json.dumps(led, indent=2))
        os.replace(tmp, self._path("ledger.json"))  # same-dir rename is atomic


# ponytail: manifest-driven input-space deferred
STDIN_DRIVEN = {"check", "filewrite"}  # seed names fed via stdin; others take argv
STDIN_BYTES_DRIVEN = {"filewrite"}  # stdin in the RAW byte domain (binary-safe seeds)


def _hidden_modes(seed: str) -> tuple:
    if seed in STDIN_BYTES_DRIVEN:
        return ("stdin-bytes",)
    return ("stdin",) if seed in STDIN_DRIVEN else ("argv",)


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
    modes = _hidden_modes(store.meta["seed"])
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


def _fn_meta(store: TaskStore, func: str) -> dict:
    fn = store.meta["functions"]
    if func not in fn:
        raise KeyError(
            f"unknown function {func!r} for task {store.meta['task_id']}; "
            f"available: {sorted(fn)}"
        )
    return fn[func]


def open_function_task(store: TaskStore, func: str) -> dict:
    from .disasm.slice import disasm_function

    f = _fn_meta(store, func)
    return {
        "task_id": store.meta["task_id"],
        "function": func,
        "address": hex(f["addr"]),
        "disasm": disasm_function(store.meta["binary"], f["addr"], f["size"]),
    }


def experiment_function(
    store: TaskStore, func: str, params: list[dict], case: dict
) -> dict:
    """Ground truth: call the real function once, forward the whole trace.

    JSON contract: cstring `mem` values come back from the driver as raw bytes and
    are hexified here — same plain-hex convention as stdin_hex/stdout_hex on program
    traces, no prefixes. buffer_i32 mem (list[int]) passes through untouched.
    INPUT side mirrors this: a cstring case value crossing the JSON boundary is a
    hex string, decoded with bytes.fromhex before the driver call; bytes pass through
    unchanged for internal callers (driver tests call this with bytes directly)."""
    from .driver.calling import call_original

    try:
        ps = [Param.from_json(p) for p in params]
    except KeyError as e:
        # Spec misuse, not a missing entity — normalize so the MCP taxonomy
        # (KeyError = not_found) never 404s a malformed param decl.
        raise ValueError(e.args[0]) from e
    for p in ps:
        if p.kind != "cstring" or p.name not in case:
            continue
        v = case[p.name]
        if isinstance(v, str):
            try:
                case[p.name] = bytes.fromhex(v)
            except ValueError as e:
                raise ValueError(
                    f"cstring case value for {p.name!r} must be hex: {e}"
                ) from e
        elif not isinstance(v, (bytes, bytearray)):
            raise ValueError(  # noqa: TRY004 — MCP taxonomy maps ValueError → "spec"
                f"cstring case value for {p.name!r} must be a hex str or bytes, "
                f"got {type(v).__name__}"
            )
    t = call_original(store.meta["binary"], _fn_meta(store, func)["addr"], ps, case)
    t["mem"] = {
        k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
        for k, v in t["mem"].items()
    }
    return t


def submit_function(
    store: TaskStore,
    func: str,
    params: list[dict],
    c_source: str,
    seed: int | None = None,
    n_fuzz: int = N_FUZZ,
) -> dict:
    # ret:"void" is agent-supplied and unverifiable from the spec: the validator floors it
    # at >=1 memory-channel param (scalar-only void compares {}=={}, a no-op would pass).
    # Beyond the floor it's the same trust class as declared directions.
    led = store.ledger()
    try:
        ps = [Param.from_json(p) for p in params]
    except (KeyError, ValueError) as e:
        # Malformed spec = a rejection like any other failed validation; the ledger
        # must count it — never die before the accounting (or inside the fuzz loop).
        led["submissions"] += 1
        led["rejections"] += 1
        store.save_ledger(led)
        return {"accepted": False, "reason": "spec", "detail": str(e)}
    # ponytail: agent-controlled cost (fresh Qiling VM per case) — clamp runaway budgets
    n_fuzz = min(n_fuzz, 4 * N_FUZZ)
    v = validate_function(
        store.meta["binary"],
        _fn_meta(store, func)["addr"],
        func,
        ps,
        c_source,
        store.dir / f"{func}.so",
        seed=seed,
        n_fuzz=n_fuzz,
    )
    led["submissions"] += 1
    if not v.ok:
        led["rejections"] += 1
        store.save_ledger(led)
        return {"accepted": False, "divergence": v.divergence}
    # Newest accepted source wins: a re-accept also passed validation, so replace.
    existing = next(
        (f for f in led["accepted"] if isinstance(f, dict) and func in f), None
    )
    if existing is not None:
        existing[func] = c_source
    else:
        led["accepted"].append({func: c_source})
    # Audit trail (parallel to "accepted" so compose's {func: source} shape is
    # untouched): the EFFECTIVE fuzz seed (fresh entropy included) + final budget.
    led.setdefault("audit", {})[func] = {"seed": v.seed, "n_fuzz": n_fuzz}
    store.save_ledger(led)
    return {"accepted": True}


def compose(store: TaskStore) -> tuple[bool, str]:
    """Compile each accepted source as its OWN translation unit, then link with a
    generated main stub into one program — inside the level-B podman worker
    (agent sources never touch the host toolchain).

    Per-TU, not text-concat: each source was validated standalone (sum_range's accepted
    model embeds clamp_i32 while clamp_i32 itself is accepted → one TU redefines it).
    Linkage rule for agents: a helper used by only one function MUST be declared
    `static` — internal linkage means no cross-TU collisions and the language dedups
    it per unit. A duplicate EXTERNALLY-visible symbol across TUs fails at ld and is
    mapped to a structured, actionable reject below.
    """
    led = store.ledger()
    entries = [next(iter(f.items())) for f in led["accepted"] if isinstance(f, dict)]
    if not entries:
        return False, "#error nothing accepted"
    sources = {
        f"{name}.compose": src
        for name, src in [*entries, ("composed_main", "int main(void){return 0;}\n")]
    }
    try:
        r = podrun.run_worker(
            {
                "mode": "compile-link",
                "sources": sources,
                "objects": [*sources],
                "out": "composed",
            },
            store.dir,
        )
    except RuntimeError as e:
        return False, f"infra: {e}"
    if "stage" in r:
        return False, f"infra: {r['detail']}"
    if not r["ok"]:
        syms = sorted(
            set(re.findall(r"multiple definition of [‘'`](\w+)", r["stderr"]))
        )
        if syms:
            names = ", ".join(f"'{s}'" for s in syms)
            return False, (
                f"duplicate symbol {names} across accepted sources: "
                f"declare single-function helpers static\n{r['stderr']}"
            )
    return r["ok"], r["stderr"]
