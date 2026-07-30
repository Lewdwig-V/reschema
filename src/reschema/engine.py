"""Task state: recorded traces, experiments, ledger. On-disk under .reschema/tasks/<task_id>."""

from __future__ import annotations

import json
from pathlib import Path

from .exec.canonical import canonicalize
from .exec.recorder import record

# plan said parents[1]; that lands at src/ — engine.py sits at src/reschema/, so root is parents[2]
ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / ".reschema" / "tasks"
MANIFEST = ROOT / ".reschema" / "corpus" / "manifest.json"


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text())


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
        a = canonicalize(record(self.meta["binary"], argv, stdin))
        b = canonicalize(record(self.meta["binary"], argv, stdin))
        if a != b:
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
