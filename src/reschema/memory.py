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
