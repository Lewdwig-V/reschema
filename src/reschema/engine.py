"""Task state: recorded traces, experiments, ledger. On-disk under .reschema/tasks/<task_id>.

Ledger writes are atomic (temp file + os.replace) but otherwise uncoordinated:
single-process (or out-of-band serialized) access is assumed.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import random
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from .driver import podrun
from .driver.spec import Param
from .exec.canonical import CANONICALIZER_VERSION, canonicalize
from .exec.recorder import record
from .memory import read_family
from .validate.function import N_FUZZ, validate_function
from .validate.program import compile_model, hidden_input_stream, replay_against

# plan said parents[1]; that lands at src/ — engine.py sits at src/reschema/, so root is parents[2]
# ponytail: correct for src-layout dev runs; pip-installed this lands under
# site-packages (upgrade: platformdirs/importlib.resources when packaging matters)
# RESCHEMA_HOME override: test isolation (pytest-xdist gives each worker its own
# root so shared .reschema state can't race); default keeps production shape.
ROOT = Path(os.environ.get("RESCHEMA_HOME", Path(__file__).resolve().parents[2]))
TASKS = ROOT / ".reschema" / "tasks"
MANIFEST = ROOT / ".reschema" / "corpus" / "manifest.json"

HIDDEN_N = 8  # distinct usable hidden inputs each submission must survive
# Cost-shaped efficiency: E = accepted * exp(-(alpha*(probes-1)+beta*(subs-1)))
# (roadmap phase 2 sizing: probe/submission counts only, no wall-clock flake).
E_ALPHA, E_BETA = 0.15, 0.40

# ISSUE-103: agents stop on ANY `{accepted: true}` payload — the floor's
# agent-exit records were ~46% FALSE COMPLETIONS: a function-level accept
# read as task completion ({func: src} in the ledger, no program marker,
# agent gone per the prompt's own stop rule). Every accept payload now
# carries `task_complete`, read live from the ledger, and incomplete accepts
# attach this constant note. The string is agent-facing, truth-only, and
# constant across tasks/conditions — drift is a deliberate §5 configuration
# change, snapshot-pinned in tests.
TASK_INCOMPLETE_NOTE = (
    "function-level acceptance is a building block: the task completes ONLY "
    "when the program model is accepted (submit_model without a `function` "
    "argument)"
)


def load_manifest() -> list[dict]:
    sidecar = MANIFEST.parent / "canonicalizer_version"
    stamped = sidecar.read_text() if sidecar.exists() else "(missing)"
    if stamped != CANONICALIZER_VERSION:
        raise RuntimeError(
            f"corpus recorded under canonicalizer {stamped}, current {CANONICALIZER_VERSION}; "
            f"a rules change means a corpus re-record: run `python -m reschema.corpus.generate` "
            f"and re-record affected task traces (stale .reschema state)"
        )
    return json.loads(MANIFEST.read_text())


def _record_stable(binary: str | Path, argv: list[str], stdin: bytes) -> dict | None:
    """Double-record ground truth; canonical trace, or None on flaky divergence."""
    a = canonicalize(record(binary, argv, stdin))
    return a if a == canonicalize(record(binary, argv, stdin)) else None


class TaskStore:
    def __init__(self, task_id: str) -> None:
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
        """Record a ground-truth trace (one probe); double-record to catch flakiness."""
        led = self.ledger()
        led["probes"] = led.get("probes", 0) + 1
        self.save_ledger(led)
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

    def save_ledger(self, led: dict) -> None:
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


def _topology_digest(store: TaskStore, func: str) -> dict:
    """Call-shape digest for the manifest fn: NAME-INDEPENDENT call structure
    — callee count, per-child chain depths, chain depth, arity hint. Names are
    intentionally excluded so a symbol-less slot maps family facts by the same
    shape (callee names are slot-local conventions; the structure is the identity)."""
    from .disasm.analyze import analyze_function

    facts = analyze_function(store.meta["binary"], store.meta["functions"])
    graph = {
        fn: sorted({c["name"] for c in f["callees"] if c["name"] in facts})
        for fn, f in facts.items()
    }

    def depth(fn: str, seen: frozenset) -> int:
        if fn in seen:
            return 0
        kids = graph.get(fn, [])
        if not kids:
            return 0
        seen = seen | {fn}
        return 1 + max(depth(k, seen) for k in kids)

    return {
        # name-independent call shape: callee COUNT + per-child chain depths +
        # THIS fn's own chain depth. Names are intentionally absent — a symbol-less
        # slot renames all of them, the shape survives per codex P2.
        "callee_count": len(graph.get(func, [])),
        "child_depths": sorted(
            depth(k, frozenset((func,))) for k in graph.get(func, [])
        ),
        "call_depth": depth(func, frozenset()),
        "arity_hint": facts[func]["arity_guess"],
    }


def _record_notes(
    store: TaskStore, fn: str, notes: list[str] | None, promoted: bool
) -> None:
    """Agent-declared notes land as unverified_hypothesis entries, promoted only
    if the submission they annotate is accepted (never by later submissions)."""
    if not notes:
        return
    from .memory import append_fact

    for note in notes:
        append_fact(
            store.meta["seed"],
            {
                "tier": "unverified_hypothesis",
                "fn": fn,
                "task_id": store.meta["task_id"],
                "note": note,
                "promoted": promoted,
            },
        )


def _journal(led: dict, event: dict) -> None:
    """Capped per-submission event log for status telemetry (last 16)."""
    led.setdefault("recent", []).append(event)
    del led["recent"][:-16]


# --- near-duplicate resubmission guard (#95, the flail guard) ---------------
# Live smoke: small agents resubmit near-identical broken sources in a loop
# (comment churn, one edited line, syntactically incomplete), each pass paying
# a compile+replay round and the β E-decay. The guard fingerprints every
# GATE-rejected source (spec rejects excluded: the source was never judged)
# and refuses a candidate whose normalized shape has looped too often. Two
# bands, because text alone cannot tell verbatim churn from the legitimate
# minimal repair (codex P2 on #99):
#   * EXACT band (normalized diff 0): resubmitting a byte-shape already
#     gate-rejected twice is zero-information — refused outright.
#   * NEAR band (0 < diff <= threshold): a small CODE edit may BE the correct
#     one-char repair, which only the gate can judge — it always gets runway,
#     refused only after the shape's THIRD rejection.
# Blocking verbatim-ish loops, not paraphrase: an agent that really changes
# approach sails past the threshold (reformatting is not an evasion, per the
# issue's caution).
DUP_DIFF_FLOOR = 24  # chars: near-dup at-or-below this absolute edit mass
DUP_DIFF_FRAC = 0.06  # ... or this fraction of the candidate's length
DUP_MIN_REJECTS = 2  # EXACT-band: refuse from the 3rd verbatim resubmission loop
DUP_MIN_NEAR_REJECTS = 3  # NEAR-band: refuse from the 4th edited-variant loop
DUP_STORE = 8  # fingerprints kept per task ledger (recent flail is the target)
# Stages without a CODE verdict (the model never ran against cases): a
# params-fixed resubmit of the same source must not be blocked by its own
# declaration failures (codex P2 on #101, same exclusion class as the
# malformed-spec early return).
DUP_NO_VERDICT_STAGES = ("spec", "arity", "skip-starvation", "infra")


def _norm_source(src: str) -> str:
    """Comment/whitespace-stripped fingerprint, string-literal aware.

    A heuristic, not a parse: deterministic and crash-free on any input.
    `//`/`/* */` sequences INSIDE literals survive; outside they die."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":  # literal: copy through its closing quote (or EOF)
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            out.append(src[i : j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j == -1 else j + 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif not c.isspace():
            out.append(c)
            i += 1
        else:
            i += 1
    return "".join(out)


def _char_diff(a: str, b: str) -> int:
    """Edit mass between normalized sources: chars added/removed/replaced."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )


def _flail_verdict(fingerprints: list[str], cand: str) -> tuple[int, int] | None:
    """-> (match count, min edit mass) when cand is a refused loop, else None.

    Two bands: EXACT (diff 0) matches refuse at DUP_MIN_REJECTS — nothing
    semantics-bearing can be new. NEAR matches (0 < diff <= threshold) count
    toward DUP_MIN_NEAR_REJECTS — a small code edit might be the legitimate
    minimal fix and gets one extra repair attempt's runway before the loop
    is killed."""
    threshold = max(DUP_DIFF_FLOOR, int(len(cand) * DUP_DIFF_FRAC))
    exact, near = 0, []
    for fp in fingerprints:
        d = _char_diff(fp, cand)
        if d == 0:
            exact += 1
        elif d <= threshold:
            near.append(d)
    if exact >= DUP_MIN_REJECTS:
        return exact + len(near), 0
    if exact + len(near) >= DUP_MIN_NEAR_REJECTS:
        return exact + len(near), min(near)
    return None


def _fingerprint_reject(led: dict, c_source: str) -> None:
    fps = led.setdefault("rejected_norm", [])
    fps.append(_norm_source(c_source))
    del fps[:-DUP_STORE]


def status_snapshot(store: TaskStore) -> dict:
    """Ledger+manifest status: readiness, coverage, validation telemetry."""
    led = store.ledger()
    n_exp = led.get("probes", 0)
    n_sub = led.get("submissions", 0)
    accepted_any = bool(led["accepted"])
    e_value = (
        # both counters clamped at their baseline: legacy accepted ledgers with
        # submissions == 0 predate the bookkeeping fix and must not score > 1
        math.exp(-(E_ALPHA * max(0, n_exp - 1) + E_BETA * max(0, n_sub - 1)))
        if accepted_any
        else 0.0
    )
    accepted_fns = sorted(
        name for entry in led["accepted"] if isinstance(entry, dict) for name in entry
    )
    return {
        "task_id": store.meta["task_id"],
        "recorded_cases": len(store.recorded()),
        "readiness": {
            "minimum": HIDDEN_N,
            "ready": len(store.recorded()) >= HIDDEN_N,
        },
        "coverage": {
            "accepted_functions": accepted_fns,
            "total_functions": len(store.meta["functions"]),
            "program_accepted": any(
                isinstance(x, str) and x == "program" for x in led["accepted"]
            ),
        },
        "ledger": led,
        "recent": led.get("recent", []),
        "efficiency": {
            "E": e_value,
            "n_exp": n_exp,
            "n_sub": n_sub,
            "alpha": E_ALPHA,
            "beta": E_BETA,
        },
    }


def submit_program(
    store: TaskStore, c_source: str, notes: list[str] | None = None
) -> dict:
    """Anti-hardcoding gate: replay recorded cases, then freshly-recorded hidden ones."""
    rec = store.recorded()
    model = store.dir / "model"
    led = store.ledger()
    led["submissions"] += 1

    def reject(**kw):
        led["rejections"] += 1
        _record_notes(store, "__main__", notes, promoted=False)
        _fingerprint_reject(led, c_source)  # every program reject is a code verdict
        _journal(
            led,
            {
                "mode": "program",
                "outcome": "reject",
                "stage": kw.get("stage", kw["reason"]),
            },
        )
        store.save_ledger(led)
        return {"accepted": False, **kw}

    verdict = _flail_verdict(led.get("rejected_norm", []), _norm_source(c_source))
    if verdict is not None:  # flail loop, refused BEFORE the gate spend
        n_dup, d_dup = verdict
        return reject(
            reason="duplicate",
            detail=(
                f"near-duplicate of {n_dup} earlier rejected submission(s) "
                f"({d_dup} normalized-char diff) — change approach or stop"
            ),
        )

    ok, err = compile_model(c_source, model)
    if not ok:
        return reject(reason="compile", detail=err)
    v = replay_against(model, rec)
    if not v.ok:
        return reject(reason=v.reason, stage="recorded", divergence=v.divergence)
    modes = _hidden_modes(store.meta["seed"])
    known = {(tuple(t["argv"][1:]), t["stdin_hex"]) for t in rec}
    # Hidden ground truth: fresh unguessable inputs (new entropy every submission),
    # double-recorded like stored cases. A flaky or crashing draw invalidates the
    # INPUT (redraw), never the model — a crash trace is not a behavior spec.
    hidden_seed = f"hidden:{store.meta['task_id']}:{secrets.token_hex(16)}"
    rng = random.Random(hidden_seed)
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
        return reject(
            reason="hidden-starvation",
            detail=f"{len(fresh)}/{HIDDEN_N} usable hidden inputs after 80 draws",
        )
    v = replay_against(model, fresh)
    if not v.ok:
        return reject(reason=v.reason, stage="hidden", divergence=v.divergence)
    # Accept marker is idempotent (re-accept re-records one), audit keeps the
    # effective hidden seed so the passing suite is traceable like function mode.
    led["accepted"] = [
        x for x in led["accepted"] if not (isinstance(x, str) and x == "program")
    ]
    led["accepted"].append("program")
    led.setdefault("audit", {})["program"] = {"hidden_seed": hidden_seed}
    _journal(led, {"mode": "program", "outcome": "accept"})
    _record_notes(store, "__main__", notes, promoted=True)
    store.save_ledger(led)
    from .memory import append_fact

    append_fact(
        store.meta["seed"],
        {
            "tier": "verified_fact",
            "promoted": True,
            "fn": "__main__",
            "task_id": store.meta["task_id"],
            "c_source": c_source,
        },
    )
    return {
        "accepted": True,
        "recorded_cases": len(rec),
        "hidden_cases": HIDDEN_N,
        "hidden_seed": hidden_seed,
        "task_complete": True,  # #103: program acceptance IS the slot contract
    }


def _fn_meta(store: TaskStore, func: str) -> dict:
    fn = store.meta["functions"]
    if func not in fn:
        raise KeyError(
            f"unknown function {func!r} for task {store.meta['task_id']}; "
            f"available: {sorted(fn)}"
        )
    return fn[func]


def _abi_template(func: str, facts: dict) -> str:
    """Compile-ready function-mode starter, rendered from the driver's own
    constants (KINDS + Param defaults) so the schema docs can't drift."""
    from .driver.spec import KINDS

    n = facts["arity_guess"]
    ret_void = not facts["returns_hint"]
    default_range = list(Param.__dataclass_fields__["range"].default)
    params = [
        {"name": f"arg{i}", "kind": "i32", "range": default_range} for i in range(n)
    ]
    if ret_void and params:
        # The sketch must satisfy the engine's own void floor (>=1 memory-channel
        # param), or a pasted submission self-rejects at the spec stage. Channel
        # kind is a guess (buffer_i32+length param for multi-arg fns, cstring for
        # 1-arg fns); the agent verifies against experiments — facts are guesses.
        params[0] = {
            "name": "arg0",
            **(
                {
                    "kind": "buffer_i32",
                    "direction": "in_out",
                    "length_param": "arg1",
                    "range": default_range,
                }
                if n > 1
                else {"kind": "cstring", "direction": "in_out"}
            ),
            "ret": "void",
        }
    sketch = json.dumps(params)
    ret_ty = "void" if ret_void else "int32_t"
    sig = ", ".join(f"int32_t arg{i}" for i in range(n)) or "void"
    return f"""/* ReSchema ABI starter — {func} (facts are {facts["labeled"]})
 *
 * COMPOSE RULE: a helper used by ONE function MUST be declared `static` —
 * internal linkage means no cross-TU duplicate-symbol failures at compose time.
 */
#include <stdint.h>
#define RESCHEMA_FN __attribute__((sysv_abi, noinline))

/* Param-spec JSON sketch (schema from the driver constants):
 *   kind: {(" | ").join(f'"{k}"' for k in KINDS)}
 *   direction: "in" (default) | "out" | "in_out"
 *   ret: "i32" (default) | "void" — carried on params[0]; a void spec with NO
 *        memory-channel param (buffer_i32/cstring) is REJECTED (compares {{}} == {{}}).
 *   length_param: scalar param name carrying a buffer_i32's length
 *   range: i32 default [-100, 100]; cstring case values travel as hex
 *
 * {sketch}
 *
 * Sketched signature: {ret_ty} {func}({sig}) — arity {n}, returns {"void" if ret_void else "int32_t"}:
 * heuristic guess; verify via experiments before declaring. */

"""


def _repair_directive(store: TaskStore) -> dict | None:
    """Two-pass repair coaching (research slot 2B-5): rejections are answered
    FIRST with abstract bit-logic repair, idiomatic annotation after acceptance.
    Guidance only, attached when the task ledger shows rejection history."""
    led = store.ledger()
    rej = [e for e in led.get("recent", []) if e.get("outcome") == "reject"]
    if not rej:
        return None
    last = rej[-1]
    stage = last.get("stage") or last.get("reason", "divergence")
    return {
        "trigger": f"{len(rej)} recent rejection(s); last stage: {stage}",
        "order": [
            "1) abstract bit-logic repair: fixed-width integers and exact byte-level behavior, no idiomatic or semantic attempts; satisfy the verifier bare-minimum",
            "2) idiomatic annotation and semantic refinement (types, names, structure): only AFTER the submission is accepted",
        ],
        "provenance": "coaching guidance derived from your rejection history, not a verified fact",
    }


def open_function_task(store: TaskStore, func: str) -> dict:
    from .disasm.analyze import analyze_function, disasm_function
    from .memory import present

    f = _fn_meta(store, func)
    facts = analyze_function(store.meta["binary"], store.meta["functions"])[func]
    mem = read_family(store.meta["seed"], fn=func)
    out = {
        "task_id": store.meta["task_id"],
        "function": func,
        "address": hex(f["addr"]),
        "disasm": disasm_function(store.meta["binary"], f["addr"], f["size"]),
        "signature_guess": {
            "arity_guess": facts["arity_guess"],
            "returns_hint": facts["returns_hint"],
            "labeled": facts["labeled"],
        },
        "callees": facts["callees"],
        "abi_template": _abi_template(func, facts),
        "memory": mem,
        # #92/#93: memory_provenance framing + ready_to_submit card, additive
        **present(mem),
    }
    d = _repair_directive(store)
    if d is not None:
        out["repair_directive"] = d
    return out


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

    led = store.ledger()
    led["probes"] = led.get("probes", 0) + 1
    store.save_ledger(led)
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
    notes: list[str] | None = None,
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
        _journal(
            led,
            {
                "mode": "function",
                "outcome": "reject",
                "function": func,
                "stage": "spec",
            },
        )
        store.save_ledger(led)
        return {"accepted": False, "reason": "spec", "detail": str(e)}
    # Flail guard (#95): same shape as the program path, refused before the
    # fuzz VM spend. NOTE: spec rejects above are NOT fingerprinted — the
    # source was never judged, only the declaration was.
    verdict = _flail_verdict(led.get("rejected_norm", []), _norm_source(c_source))
    if verdict is not None:
        n_dup, d_dup = verdict
        led["submissions"] += 1
        led["rejections"] += 1
        _fingerprint_reject(led, c_source)
        _journal(
            led,
            {
                "mode": "function",
                "outcome": "reject",
                "function": func,
                "stage": "duplicate",
            },
        )
        store.save_ledger(led)
        return {
            "accepted": False,
            "reason": "duplicate",
            "detail": (
                f"near-duplicate of {n_dup} earlier rejected submission(s) "
                f"({d_dup} normalized-char diff) — change approach or stop"
            ),
        }
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
    _record_notes(store, func, notes, promoted=v.ok)
    if not v.ok:
        led["rejections"] += 1
        # Fingerprint CODE verdicts only: divergence verdicts carry no stage
        # key; spec/arity/starvation/infra stages never executed the model.
        if v.divergence.get("stage") not in DUP_NO_VERDICT_STAGES:
            _fingerprint_reject(led, c_source)
        _journal(
            led,
            {
                "mode": "function",
                "outcome": "reject",
                "function": func,
                "stage": v.divergence.get("stage", "divergence"),
            },
        )
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
    _journal(led, {"mode": "function", "outcome": "accept", "function": func})
    store.save_ledger(led)
    from .memory import append_fact

    append_fact(
        store.meta["seed"],
        {
            "tier": "verified_fact",
            "promoted": True,
            "fn": func,
            "task_id": store.meta["task_id"],
            "params": [p.to_json() for p in ps],
            "c_source": c_source,
            "n_fuzz": n_fuzz,
            "audit_seed": v.seed,
            "topology": _topology_digest(store, func),
        },
    )
    # #103: the completion signal must be read off the ledger, not implied by
    # mode — a function accept while the task is done reports complete too.
    done = any(isinstance(x, str) and x == "program" for x in led["accepted"])
    out = {
        "accepted": True,
        "compared": v.compared,
        "skipped": v.skipped,
        "seed": v.seed,
        "task_complete": done,
    }
    if not done:
        out["note"] = TASK_INCOMPLETE_NOTE  # what completes the task (constant)
    return out


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
    # ISSUE-61: compose compiles in a scratch dir like every agent-C compile;
    # the composed binary is the only artifact worth copying back.
    with tempfile.TemporaryDirectory(prefix="reschema-compose-") as scratch:
        try:
            r = podrun.run_worker(
                {
                    "mode": "compile-link",
                    "sources": sources,
                    "objects": [*sources],
                    "out": "composed",
                },
                Path(scratch),
            )
        except RuntimeError as e:
            return False, f"infra: {e}"
        if "stage" in r:
            return False, f"infra: {r['detail']}"
        for f in Path(scratch).glob("*.compose.c"):
            shutil.copy2(f, store.dir / f.name)  # debug artifacts, like model.c
        if r["ok"]:
            shutil.copy2(Path(scratch) / "composed", store.dir / "composed")
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
