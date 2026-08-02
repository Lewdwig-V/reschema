import subprocess
from unittest.mock import Mock

import pytest

from reschema.driver import podrun


def test_ensure_image_hard_fails_with_build_cmd(monkeypatch):
    # Mandatory containment: missing image is a loud, actionable refusal.
    monkeypatch.setattr(
        podrun.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1))
    )
    with pytest.raises(RuntimeError, match="podman build"):
        podrun.ensure_image()


def test_run_worker_container_failure_is_structured_infra(tmp_path, monkeypatch):
    monkeypatch.setattr(podrun, "ensure_image", lambda: None)
    monkeypatch.setattr(
        podrun.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 125, stderr=b"boom")),
    )
    r = podrun.run_worker({"mode": "validate"}, tmp_path)
    assert r["stage"] == "infra"
    assert "boom" in r["detail"]
