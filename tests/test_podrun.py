import os
import pwd
import subprocess
from pathlib import Path
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


def test_podman_store_ignores_sandbox_pinned_xdg(tmp_path, monkeypatch):
    """2C dogfood pins HOME/XDG into a per-slot sandbox (opencode config
    isolation). Rootless podman must still consult the account's REAL image
    store: the sandbox-relocated store is always empty, the localhost/
    toolchain image can never be pulled into it, and every level-B gate would
    refuse with "image missing; build it" — unfixable by the confined agent
    (observed live in the first gemma4 smoke). $HOME is pinnable; getpwuid is
    not — that is the seam this test pins."""
    fake_data = tmp_path / "sandbox-xdg"
    real_data = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_data))
    monkeypatch.setenv("HOME", str(tmp_path))
    captured = []

    def fake_run(argv, **kw):
        captured.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"results": []}')

    monkeypatch.setattr(podrun.subprocess, "run", fake_run)
    podrun.ensure_image()
    podrun.run_worker({"mode": "validate", "cases": []}, tmp_path)
    # image-exists guard (explicit + run_worker's internal one) + the worker
    assert [a[:3] for a, _ in captured] == [
        ["podman", "image", "exists"],
        ["podman", "image", "exists"],
        ["podman", "run", "--rm"],
    ]
    for argv, kw in captured:
        assert argv[0] == "podman"
        assert kw["env"]["XDG_DATA_HOME"] == str(real_data)
        assert kw["env"]["XDG_DATA_HOME"] != str(fake_data)
        assert kw["env"]["HOME"] == str(tmp_path)  # ambient env otherwise passes
