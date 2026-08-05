from tools.dogfood.prompt import _TEMPLATE, FORBIDDEN_TERMS, render, template_hash

EXPECTED_DIGEST = "797261d7750a8f6ca504b0ec70bd9fb2187ef97c7c741771402175f7964f6207"


def test_render_is_neutral_and_mentions_only_the_task():
    p = render("rot13::gcc-O1-sym")
    assert "rot13::gcc-O1-sym" in p
    low = p.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"prompt coaches the agent: {term!r}"


def test_raw_template_is_neutral():
    low = _TEMPLATE.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"template coaches the agent: {term!r}"


def test_forbidden_list_is_nonvacuous_and_bites():
    assert len(FORBIDDEN_TERMS) >= 8
    assert "memory" in FORBIDDEN_TERMS  # protocol vocabulary contamination
    contaminated = (render("t") + " submission memory").lower()
    assert any(t.lower() in contaminated for t in FORBIDDEN_TERMS)


def test_template_hash_pinned_to_golden_digest():
    assert template_hash() == EXPECTED_DIGEST
