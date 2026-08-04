import json

import pytest


@pytest.fixture
def stub_corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps([{"task_id": "rot13::gcc-O0-sym"}]))
    return d
