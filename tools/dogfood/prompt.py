"""The ONE neutral task prompt. Protocol words are contamination: a prompt
that coaches probe/submission strategy is solver scaffolding (#63 class) and
invalidates the free-run measurement. The forbidden list is lint-tested."""

from __future__ import annotations

import hashlib

# Stems, not words: morphology must not evade the lint ("submission"
# vs "submissions", "prime/primed/priming"). Short stems like "prim"
# and "cach" are deliberate.
FORBIDDEN_TERMS = [
    "prim",
    "probe",
    "submission",
    "cach",
    "φ",
    "phi",
    "transfer",
    "benchmark",
    "protocol",
    "hidden",
    "memory",
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
