"""Family deduction cache: .reschema/memory/<seed>.jsonl, two-tier provenance.

verified_fact entries are written ONLY by the harness on gate acceptance
(compile+validate passed) — immutable ground truth for a {seed, function}
family. unverified_hypothesis entries are agent-declared notes, promoted to
verified only when the submission they annotate is accepted.

Writes follow the task-ledger discipline: single-process, temp file + atomic
replace. Reads never reject a malformed line with an exception (a cache is a
hint source, it degrades silently to "no memory").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# RESCHEMA_HOME: see engine.py (xdist per-worker isolation); default unchanged.
ROOT = Path(os.environ.get("RESCHEMA_HOME", Path(__file__).resolve().parents[2]))
MEMORY = ROOT / ".reschema" / "memory"


def _path(root: Path | None, seed: str) -> Path:
    base = root if root is not None else MEMORY
    return Path(base) / f"{seed}.jsonl"


def read_family(
    seed: str, root: Path | None = None, fn: str | None = None
) -> list[dict]:
    p = _path(root, seed)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # a hint source degrades to "no memory", never crashes
        if not isinstance(e, dict):
            continue  # valid JSON but not an entry (null/[]/scalar) — skip quietly
        if fn is None or e.get("fn") == fn:
            out.append(e)
    return out


def append_fact(seed: str, entry: dict, root: Path | None = None) -> None:
    p = _path(root, seed)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    # rewrite whole content into a temp file, then one atomic replace
    prior = p.read_text() if p.exists() else ""
    tmp.write_text(prior + json.dumps(entry) + "\n")
    os.replace(tmp, p)  # same-dir rename is atomic


# --- presentation tier (#92/#93, config B) --------------------------------
# Natural-language, action-oriented framing layered OVER the raw `memory`
# list at task_open time. Small agents act on a field NAMED ready_to_submit
# far more reliably than on tier semantics. Both strings are STATIC framing,
# constant across tasks/conditions — exact text snapshot-pinned in
# tests/test_memory.py; drift is a deliberate §5 configuration change.

MEMORY_PROVENANCE = (
    "Entries marked `verified_fact` were written by the harness after a "
    "hidden-gate acceptance — verified, not agent-claimed."
)

READY_NOTE = "passed the hidden gate on a sibling build of this seed"


def ready_to_submit(entries: list[dict]) -> dict | None:
    """Action card distilled from the NEWEST verified_fact, or None.

    Content discipline (negative-tested): ONLY verified_fact entries feed the
    card — agent notes (promoted or not) never do, so the card can never
    widen into answer-leaking as formats evolve. The note is static framing,
    not judgable content."""
    facts = [e for e in entries if e.get("tier") == "verified_fact"]
    if not facts:
        return None
    f = facts[-1]  # JSONL append order is chronological: newest acceptance last
    card = {
        "c_source": f.get("c_source"),
        "fn": f.get("fn"),
        "verified_on": f.get("task_id"),
        "note": READY_NOTE,
    }
    if "params" in f:  # function-mode facts only; __main__ facts omit the key
        card["params"] = f["params"]
    return card


def present(entries: list[dict]) -> dict:
    """task_open's additive presentation for a family-cache bundle:
    `memory_provenance` framing on ANY non-empty cache (#93), the
    `ready_to_submit` card only when a verified fact exists (#92)."""
    out = {}
    if entries:
        out["memory_provenance"] = MEMORY_PROVENANCE
    card = ready_to_submit(entries)
    if card is not None:
        out["ready_to_submit"] = card
    return out
