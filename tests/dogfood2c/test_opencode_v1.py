import json
import stat

import pytest

from tools.dogfood.runners.base import RunnerConfig
from tools.dogfood.runners.opencode_v1 import OpenCodeV1Runner


def _cfg(tmp_path):
    return RunnerConfig(
        model="gemma4",
        endpoint="http://lan:11434/v1",
        sandbox=tmp_path / "sb",
        run_root=tmp_path / "root",
    )


def _sleeper(tmp_path):
    """Stub harness binary: ignores argv, sleeps; kill() must cut it short."""
    p = tmp_path / "stub-opencode"
    p.write_text("#!/bin/sh\nsleep 300\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def test_session_config_allowlists_exactly_five_tools(tmp_path):
    r = OpenCodeV1Runner(binary="/bin/true")
    r.prepare(_cfg(tmp_path))
    cfg = json.loads((tmp_path / "sb/opencode.json").read_text())
    tools = cfg["agent"]["tools"]
    assert tools["reschema_*"] is True
    assert all(v is False for k, v in tools.items() if k != "reschema_*")
    assert cfg["mcp"]["reschema"]["type"] == "local"
    assert cfg["model"].endswith("gemma4")


def test_kill_makes_wait_return(tmp_path):
    r = OpenCodeV1Runner(binary=_sleeper(tmp_path))
    cfg = _cfg(tmp_path)
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    r.prepare(cfg)
    r.spawn("p")
    r.kill()
    assert r.wait().exit_kind in ("timeout", "exit")


def test_exited_tracks_process(tmp_path):
    r = OpenCodeV1Runner(binary=_sleeper(tmp_path))
    cfg = _cfg(tmp_path)
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    r.prepare(cfg)
    r.spawn("p")
    assert not r.exited()
    r.kill()
    r.wait()
    assert r.exited()


def test_preflight_reports_endpoint_facts(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    fake = {"models": [{"id": "gemma4"}], "version": "fake-stack-1.0"}
    monkeypatch.setattr(r, "_post", lambda *a, **k: (200, fake))
    info = r.preflight(_cfg(tmp_path))
    assert info["model"] == "gemma4" and info["digest"] == "fake-stack-1.0"


def test_preflight_raises_when_endpoint_dead(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    monkeypatch.setattr(r, "_post", lambda *a, **k: (0, {}))
    with pytest.raises(RuntimeError):
        r.preflight(_cfg(tmp_path))

    calls = iter([(200, {"version": "x"}), (500, {})])  # models ok, probe 500
    monkeypatch.setattr(r, "_post", lambda *a, **k: next(calls))
    with pytest.raises(RuntimeError):
        r.preflight(_cfg(tmp_path))


def test_preflight_digest_unknown_on_null_version(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    monkeypatch.setattr(r, "_post", lambda *a, **k: (200, {"version": None}))
    assert r.preflight(_cfg(tmp_path))["digest"] == "unknown"
