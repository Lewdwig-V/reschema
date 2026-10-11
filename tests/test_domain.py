"""M8 S2: the Warranted domain skeleton and the checker image's dependency pin."""

import re
import subprocess
from pathlib import Path

from warranted import domain_identity

from reschema.domain import ReSchemaDomain

ROOT = Path(__file__).resolve().parent.parent


def test_identity_pins_image_and_source() -> None:
    identity = domain_identity(ReSchemaDomain())
    assert identity["domain"] == "reschema"
    # Warranted refuses a project whose worker image is a mutable tag.
    assert re.fullmatch(r"[0-9a-f]{64}", identity["worker_image"])
    assert re.fullmatch(r"[0-9a-f]{64}", identity["domain_source"])


def test_sources_cover_every_module() -> None:
    # Negative side of the pin: a module outside the declared sources could
    # change checker behaviour without changing the domain identity.
    roots = [Path(s).resolve() for s in ReSchemaDomain.sources]
    for module in (ROOT / "src" / "reschema").rglob("*.py"):
        assert any(module.is_relative_to(r) for r in roots), module


def _requirements(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_checker_requirements_match_lock() -> None:
    # The checker image installs qiling from this file; it must be exactly
    # uv.lock's closure, or the in-job replay drifts from host recording.
    # Regenerate with the command in the file's header.
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--prune",
            "warranted",
            "--format",
            "requirements-txt",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout
    committed = (ROOT / "checker-requirements.txt").read_text()
    assert _requirements(committed) == _requirements(exported)


def test_norm_source_strips_comments_and_whitespace_keeps_literals() -> None:
    # The duplicate guard's fingerprint (ReSchemaDomain.normalize).
    from reschema.domain import norm_source

    a = norm_source('int f() { // a comment\n return printf("a//b /*c*/"); }')
    b = norm_source('int f(){/*c*/return printf("a//b /*c*/");}')
    assert a == b
    assert '"a//b /*c*/"' in b  # literal content intact (// and /* inside)
    # char literals and escapes survive; comments die at an unterminated EOF
    assert norm_source("char c='/'; /* unterminated") == "charc='/';"
