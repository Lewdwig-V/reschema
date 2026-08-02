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


def test_ensure_image_podman_not_installed_is_actionable_runtimeerror(monkeypatch):
    # No podman binary at all: FileNotFoundError must become the same structured
    # guidance, not a traceback up to the MCP 'internal' catch-all.
    monkeypatch.setattr(podrun.subprocess, "run", Mock(side_effect=FileNotFoundError))
    with pytest.raises(RuntimeError, match="podman"):
        podrun.ensure_image()


def test_run_worker_timeout_scales_with_case_count(tmp_path, monkeypatch):
    # 64 hanging cases x CASE_TIMEOUT > 240s default: the podman timeout must be
    # sized from the fuzz budget, or hang submissions die as infra failures
    # instead of their documented per-case crash verdicts.
    monkeypatch.setattr(podrun, "ensure_image", lambda: None)
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(
            argv, 0, stdout=b'{"ok": true, "results": []}'
        )

    monkeypatch.setattr(podrun.subprocess, "run", fake_run)
    podrun.run_worker({"mode": "validate", "cases": [{}] * 64}, tmp_path)
    assert captured["timeout"] >= 64 * 5, captured


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
