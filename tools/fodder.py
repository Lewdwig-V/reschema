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
    ledger dicts, program accepts from the task dir's `model.c` artifact."""
    out = set()
    for led in runs_dir.rglob("ledger.json"):
        try:
            d = json.loads(led.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for entry in d.get("accepted", []):
            if isinstance(entry, dict):
                out.update(str(v) for v in entry.values())
        model_c = led.parent / "model.c"
        if model_c.exists() and "program" in d.get("accepted", []):
            out.add(model_c.read_text())
    return out


# Yield probes: one argv-shaped draw and one stdin-shaped draw (covers both
# seed input channels without manifest knowledge).
_PROBES = [(["a"], b""), (["a", "bc"], b""), ([], b"abc\n")]


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
    program = [e for e in entries if e["mode"] == "program"]
    rejected = [e for e in program if e["verdict"] == "rejected"]
    report = {
        "floors": [str(r) for r in floor_roots],
        "submit_model_calls_found": len(entries),
        "function_mode_deferred": sum(1 for e in entries if e["mode"] == "function"),
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
    report["totals"] = {f"{c}/{v}": n for (c, v), n in sorted(report["totals"].items())}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report))  # indent adds ~nix
    return report
