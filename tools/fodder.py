"""ISSUE-111: fodder-yield experiment (phase 3A entry — offline tooling only).

Measures the viability of the self-play supply chain BEFORE any engine work:
of sources that reached the gate and lost, how many survive a
compile + behavioral-stability filter? UB-est classes (escape slips, mangled
C) must fail the compile gate; nondeterministic behavior must fail the
stability gate (qiling double-record, same input twice).

Population availability: ledgers only persist `rejected_sources` going
forward (#116), so the experiment's historical source is dogfood floor
TRANSCRIPTS (submit_model payloads carry full bodies). Verdict tagging is
exact-string equality against the accepted sources persisted per task
(function-mode accepts live in ledger `accepted` dicts; the accepted program
source lives as the task dir's `model.c` debug artifact).

Cost shape: one podman compile + a few qiling records per unique candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

from reschema.engine import _record_stable
from reschema.validate.program import compile_model

_SUBMIT_RE = re.compile(r"reschema_submit_model (\{.*)")
_VERDICT_CLASSES = ("stable", "compile-fail", "nondeterministic", "crashy")


def parse_transcript(text: str) -> list[dict]:
    """submit_model payloads from an opencode transcript; malformed lines
    degrade silently (truncated tails, HTML-glitch agents), never crash."""
    out, seen = [], set()
    for m in _SUBMIT_RE.finditer(text):
        try:
            args = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        src = args.get("c_source")
        if not isinstance(src, str):
            continue
        key = (args.get("task_id"), args.get("function"), src)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "task_id": args.get("task_id"),
                "mode": "function" if args.get("function") else "program",
                "function": args.get("function"),
                "params": args.get("params"),
                "c_source": src,
            }
        )
    return out


def tag_verdicts(entries: list[dict], accepted_sources: set[str]) -> None:
    """Exact-string equality ONLY: pre-acceptance churned twins of the winning
    source are flail-class rejects (fodder), not winners."""
    for e in entries:
        e["verdict"] = "accepted" if e["c_source"] in accepted_sources else "rejected"


def classify_source(src: str) -> str:
    """Pragmatic failure-class tags for the yield report (heuristics, not a
    taxonomy of truth; the stability verdict is the authority)."""
    if "'\\\\0'" in src or "'\\\\n'" in src:
        return "escape-slip"
    if re.search(r"0x[0-9a-fA-F]{6,}", src):
        return "magic-stub"
    return "standard"


def accepted_sources_for_runs(runs_dir: Path) -> set[str]:
    """Exact accepted sources persisted per task: function accepts from
    ledger dicts, program accepts from the ledger's immutable
    `program_source` record. NEVER the task dir's `model.c` — compile_model
    rewrites that artifact on EVERY compile, including rejected attempts
    after acceptance (codex P2 on #118); absence degrades to skipping
    nothing, the safe direction."""
    out = set()
    for led in runs_dir.rglob("ledger.json"):
        try:
            d = json.loads(led.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for entry in d.get("accepted", []):
            if isinstance(entry, dict):
                out.update(str(v) for v in entry.values())
        if "program" in d.get("accepted", []):
            if isinstance(d.get("program_source"), str):
                out.add(d["program_source"])  # immutable record (current schema)
            else:
                codex_fallback = led.parent / "model.c"
                if codex_fallback.exists():
                    # LEGACY floors only: dogfood slots are killed on accept,
                    # so model.c coincides with the winner by lifecycle. Codex
                    # P2's corruption vector is post-accept resubmits, which
                    # this layout can't produce.
                    out.add(codex_fallback.read_text())
    return out


# Yield probes: one argv-shaped draw and one stdin-shaped draw (covers both
# seed input channels without manifest knowledge).
_PROBES = [(["a"], b""), (["a", "bc"], b""), ([], b"abc\n")]

FODDER_FN_SEED = 111  # deterministic re-verification budget draw
FODDER_FN_N = 8  # tiny re-check budget: yield measurement, not a new gate


def load_manifests(corpus_roots: list[Path]) -> dict:
    """{task_id: {binary, functions}} from one or more corpus mount dirs
    (floor run roots carry the canonical mount under .reschema/corpus)."""
    tasks = {}
    for root in corpus_roots:
        mp = Path(root) / "manifest.json"
        if mp.exists():
            for t in json.loads(mp.read_text()):
                tasks[t["task_id"]] = {
                    "binary": t["binary"],
                    "functions": t["functions"],
                }
    return tasks


def function_verdict(
    tasks: dict,
    task_id: str,
    func: str,
    params_json: list[dict],
    c_source: str,
    out_dir: Path,
) -> str:
    """Re-verify ONE function candidate against the original (the engine's own
    differential validator):

    - behavioral: compiles + runs, loses vs the original — THE usable fodder
    - compile-fail: garbage — no cases even attempted
    - ok-lucky: PASSES the tiny re-check despite its historical reject —
      counted separately (entropy-luck or later-fixed), never merged into the
      behavioral pool and never re-scored as a win
    - spec-malformed: the transcript's decl is unusable before any judgment
    - unresolvable: task/function unknown to the manifest
    """
    from reschema.driver.spec import Param
    from reschema.validate.function import validate_function

    task = tasks.get(task_id)
    if not task or func not in task["functions"]:
        return "unresolvable"
    try:
        ps = [Param.from_json(p) for p in params_json]
    except (KeyError, ValueError):
        return "spec-malformed"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    so = out_dir / f"{func}.so"
    try:
        v = validate_function(
            task["binary"],
            task["functions"][func]["addr"],
            func,
            ps,
            c_source,
            so,
            seed=FODDER_FN_SEED,
            n_fuzz=FODDER_FN_N,
        )
    except Exception:  # noqa: BLE001 — infra faults are not fodder verdicts
        return "compile-fail"
    if v.ok:
        return "ok-lucky"
    stage = v.divergence.get("stage", "divergence") if v.divergence else "divergence"
    return "compile-fail" if stage in ("compile", "link", "symbol") else "behavioral"


def stability_verdict(c_source: str, out_dir: Path) -> str:
    """compile gate + qiling double-record stability gate (the engine's own
    `_record_stable` discipline). Any flaky probe = nondeterministic."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model = out_dir / hashlib.sha256(c_source.encode()).hexdigest()[:16]
    ok, _ = compile_model(c_source, model)
    if not ok:
        return "compile-fail"
    for argv, stdin in _PROBES:
        t1 = _record_stable(str(model), argv, stdin)
        if t1 is None:
            return "nondeterministic"
        if t1["exit_code"] == -1:
            return "crashy"
    return "stable"


def run_experiment(
    floor_roots: list[Path], out_dir: Path, verbose: bool = True
) -> dict:
    entries, accepted = [], set()
    for root in floor_roots:
        accepted |= accepted_sources_for_runs(root / "runs")
        for log in (root / "runs").glob("*/sandbox/*.log"):
            entries.extend(parse_transcript(log.read_text(errors="replace")))
    tag_verdicts(entries, accepted)
    # codex P2 on #118: candidates are unique BODIES (mode + exact source),
    # not repeated attempts — first occurrence keeps its provenance; verbatim
    # replays across slot logs / floor roots collapse to one probed body.
    unique, seen = [], set()
    for e in entries:
        key = (e["mode"], e["c_source"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    dupes_collapsed = len(entries) - len(unique)
    program = [e for e in unique if e["mode"] == "program"]
    rejected = [e for e in program if e["verdict"] == "rejected"]
    fn_rejected = [
        e for e in unique if e["mode"] == "function" and e["verdict"] == "rejected"
    ]
    # one canonical manifest per floor root covers function-mode verification
    # (all slot mounts of a floor share identical manifest bytes)
    tasks_by_root = {}
    for root in floor_roots:
        for mp in (root / "runs").glob("*/.reschema/corpus/manifest.json"):
            tasks_by_root.update(load_manifests([mp.parent]))
    report = {
        "floors": [str(r) for r in floor_roots],
        "submit_model_calls_found": len(entries),
        "dupes_collapsed": dupes_collapsed,
        "function_mode_total": sum(1 for e in unique if e["mode"] == "function"),
        "program_mode_total": len(program),
        "program_accepted_skipped": sum(
            1 for e in program if e["verdict"] == "accepted"
        ),
        "candidates": [],
        "totals": {},
    }
    for i, e in enumerate(rejected):
        with tempfile.TemporaryDirectory(prefix="reschema-fodder-") as scratch:
            v = stability_verdict(e["c_source"], Path(scratch))
        cls = classify_source(e["c_source"])
        entry = {
            "hash": hashlib.sha256(e["c_source"].encode()).hexdigest()[:16],
            "task_id": e["task_id"],
            "class": cls,
            "stability": v,
            "preview": " ".join(e["c_source"].split())[:100],
        }
        report["candidates"].append(entry)
        report["totals"][(cls, v)] = report["totals"].get((cls, v), 0) + 1
        if verbose:
            print(f"[{i + 1}/{len(rejected)}] {cls:12s} {v}", file=sys.stderr)
    # function-mode candidates: transcript params + floor manifests let them
    # be re-verified here and now (the forward rejected_sources store persists
    # the same fields, #111b).
    report["function_mode_deferred"] = sum(
        1 for e in unique if e["mode"] == "function" and e["verdict"] == "accepted"
    )
    for i, e in enumerate(fn_rejected):
        with tempfile.TemporaryDirectory(prefix="reschema-fodder-") as scratch:
            v = function_verdict(
                tasks_by_root,
                e["task_id"],
                e["function"],
                e.get("params") or [],
                e["c_source"],
                Path(scratch),
            )
        entry = {
            "hash": hashlib.sha256(e["c_source"].encode()).hexdigest()[:16],
            "task_id": e["task_id"],
            "function": e["function"],
            "class": "function",
            "stability": v,
            "preview": " ".join(e["c_source"].split())[:100],
        }
        report["candidates"].append(entry)
        report["totals"][("function", v)] = report["totals"].get(("function", v), 0) + 1
        if verbose:
            print(f"[fn {i + 1}/{len(fn_rejected)}] {v}", file=sys.stderr)
    report["totals"] = {f"{c}/{v}": n for (c, v), n in sorted(report["totals"].items())}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report))  # indent adds ~nix
    return report
