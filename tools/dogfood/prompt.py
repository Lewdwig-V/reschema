"""The ONE neutral task prompt. Protocol words are contamination: a prompt
that coaches probe/submission strategy is solver scaffolding (#63 class) and
invalidates the free-run measurement. The forbidden list is lint-tested —
with ONE sanctioned exception: AFFORDANCE_LINE (#91, config B). Small agents
don't infer the memory-reuse affordance from a raw JSON key, so the prompt
spends exactly one sentence on the protocol surface it can't discover. The
exact string and the template digest are pinned in tests/dogfood2c/
test_prompt.py; drift there is a deliberate §5 configuration change (config A
= the blind prompt, config B = + the affordance line)."""

from __future__ import annotations

import hashlib

# The ONE sanctioned protocol-affordance sentence (#91): constant across
# tasks, answer-free, task-free. Exempt from FORBIDDEN_TERMS by name — pinned
# exactly in test_prompt so any edit is a visible configuration change.
# NO double quotes here: the dogfood test fakes split the task_id on '"'.
# The hook keys on `ready_to_submit`, NOT `memory`: the memory key is present
# (as []) on COLD slots, so conditioning the affordance on it would tell cold
# agents to reuse what doesn't exist (codex P2 on #97). ready_to_submit is
# emitted only when the cache holds a verified fact (#92) — the affordance is
# truthful exactly when it is actionable.
AFFORDANCE_LINE = (
    "The harness remembers verified results across related tasks; when "
    "`task_open` shows `ready_to_submit`, reusing it is the cheapest path."
)

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

{affordance_line}

When the task is accepted, stop. Do not take any further actions.
"""


def render(task_id: str) -> str:
    return _TEMPLATE.format(task_id=task_id, affordance_line=AFFORDANCE_LINE)


def template_hash() -> str:
    return hashlib.sha256(render("X").encode()).hexdigest()
