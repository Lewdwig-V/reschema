import hashlib

from tools.dogfood.prompt import FORBIDDEN_TERMS, render, template_hash


def test_render_is_neutral_and_mentions_only_the_task():
    p = render("rot13::gcc-O1-sym")
    assert "rot13::gcc-O1-sym" in p
    low = p.lower()
    for term in FORBIDDEN_TERMS:
        assert term.lower() not in low, f"prompt coaches the agent: {term!r}"


def test_template_hash_stable_and_tied_to_content():
    assert template_hash() == hashlib.sha256(render("X").encode()).hexdigest()
