import json

import pytest


@pytest.fixture
def stub_corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps([{"task_id": "rot13::gcc-O0-sym"}]))
    # a corpus artifact the layout must NOT copy (mounted corpora are manifest-only)
    (d / "rot13").mkdir()
    (d / "rot13" / "fake-binary").write_bytes(b"\x7fELF")
    return d
