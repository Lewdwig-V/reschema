from tools.dogfood.prompt import (
    _TEMPLATE,
    AFFORDANCE_LINE,
    FORBIDDEN_TERMS,
    render,
    template_hash,
)

# Config B (#91): the affordance line moved the digest off config A's
# 797261d7750a8f6ca504b0ec70bd9fb2187ef97c7c741771402175f7964f6207 (the blind
# prompt the 2026-08-16 gemma4 floor ran at). Floors across A/B are not
# comparable — docs/benchmark-protocol.md §5 carries the family note.
EXPECTED_DIGEST = "f17d4c5ebd90118a0854ea8d06bf17d51a0936b07595bdfd5d9e1f90b5cdea72"


def _lint_scope(text: str) -> str:
    """The neutrality lint covers everything EXCEPT the one sanctioned
    affordance line — the only place `memory` may appear (pinned below)."""
    return text.replace(AFFORDANCE_LINE, "")


def test_render_is_neutral_and_mentions_only_the_task():
    p = _lint_scope(render("rot13::gcc-O1-sym"))
    assert "rot13::gcc-O1-sym" in p
    low = p.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"prompt coaches the agent: {term!r}"


def test_raw_template_is_neutral():
    low = _TEMPLATE.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"template coaches the agent: {term!r}"


def test_affordance_line_is_pinned_and_sanctioned():
    # The ONE protocol-affordance sentence (#91): constant across tasks, no
    # answer content, no task-specific text. Drift = a deliberate §5 config
    # change; it must be visible here and in the digest above.
    assert AFFORDANCE_LINE == (
        "The harness remembers verified results across related tasks; when "
        "`task_open` shows `memory`, reusing it is the cheapest path."
    )
    assert AFFORDANCE_LINE in render("any::slot")
    assert '"' not in AFFORDANCE_LINE  # dogfood fakes split the task on '"'


def test_forbidden_list_is_nonvacuous_and_bites():
    assert len(FORBIDDEN_TERMS) >= 8
    assert "memory" in FORBIDDEN_TERMS  # protocol vocabulary contamination
    contaminated = (_lint_scope(render("t")) + " submission memory").lower()
    assert any(t.lower() in contaminated for t in FORBIDDEN_TERMS)


def test_template_hash_pinned_to_golden_digest():
    assert template_hash() == EXPECTED_DIGEST
