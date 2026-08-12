"""Guards for the Containerfile base-image digest pin (issue #40).

The pin and its refresh procedure must survive edits: the FROM line carries
an immutable digest, and the documented refresh workflow stays in the
comments so the pin can be moved forward deliberately.
"""

import re
from pathlib import Path

CONTAINERFILE = Path(__file__).resolve().parent.parent / "Containerfile"


def _text() -> str:
    return CONTAINERFILE.read_text()


def test_from_line_pins_digest() -> None:
    from_line = next(line for line in _text().splitlines() if line.startswith("FROM "))
    assert re.search(r"trixie-slim@sha256:[0-9a-f]{64}\b", from_line), (
        "base image must be pinned as trixie-slim@sha256:<64 hex>"
    )


def test_refresh_procedure_documented() -> None:
    text = _text()
    assert "podman image inspect" in text
    assert "{{.Digest}}" in text


def test_pin_metadata_comments_present() -> None:
    text = _text()
    # Mutation-proof: format lock + pin date, not the digest value itself.
    assert re.search(r"#\s*digest pinned: \d{4}-\d{2}-\d{2}", text), (
        "the pin date comment must move with the digest"
    )


def test_apt_sources_pinned_to_snapshot() -> None:
    # Format pin, not date-value pin: any snapshot timestamp satisfies this;
    # removing the sed that pins apt sources must fail it.
    assert re.search(r"snapshot\.debian\.org/archive/debian/\d{8}T\d{6}Z", _text()), (
        "apt sources must be pinned to a snapshot.debian.org timestamp, "
        "else gcc/clang/libc resolve against live rolling trixie"
    )
