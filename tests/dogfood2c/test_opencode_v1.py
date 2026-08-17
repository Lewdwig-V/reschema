import json
import os
import signal
import stat
import subprocess
import urllib.error

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


def _fake_post(responses):
    """_post dispatching on the request path; unlisted paths look dead."""
    return lambda base, path, payload: responses.get(path, (0, {}))


def _spawn(r, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    r.prepare(cfg)
    r.spawn("p")
    return r


def test_session_config_tools_allowlist(tmp_path):
    r = OpenCodeV1Runner(binary="/bin/true")
    r.prepare(_cfg(tmp_path))
    cfg = json.loads((tmp_path / "sb/opencode.json").read_text())
    tools = cfg["tools"]  # TOP-LEVEL map: opencode's global tool switchboard
    assert tools["reschema_*"] is True
    assert all(v is False for k, v in tools.items() if k != "reschema_*")
    for k in ("bash", "edit", "write", "read"):
        assert tools[k] is False
    assert cfg["model"].endswith("gemma4")


def test_session_config_containment_keys(tmp_path):
    r = OpenCodeV1Runner(binary="/bin/true")
    cfg = _cfg(tmp_path)
    r.prepare(cfg)
    c = json.loads((cfg.sandbox / "opencode.json").read_text())
    prov = c["provider"]["local"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "http://lan:11434/v1"
    assert prov["models"] == {"gemma4": {}}  # else local/gemma4 won't resolve
    assert c["snapshot"] is False  # no worktree git-jobs starving the agent
    mcp = c["mcp"]["reschema"]
    assert mcp["type"] == "local"
    assert mcp["command"] == ["uv", "run", "python", "-m", "reschema.mcp.server"]
    # ABSOLUTE: the MCP child resolves relative values against opencode's
    # project-root cwd, not the sandbox — a relative path would plant slot
    # state in the checkout's .reschema/
    assert os.path.isabs(mcp["environment"]["RESCHEMA_HOME"])
    assert mcp["environment"]["RESCHEMA_HOME"] == str(cfg.run_root.resolve())


def test_kill_makes_wait_return_with_timeout_kind(tmp_path):
    r = _spawn(OpenCodeV1Runner(binary=_sleeper(tmp_path)), tmp_path)
    r.kill()
    assert r.wait().exit_kind == "timeout"  # SIGKILL under kill() ⇒ timeout
    assert r.exited()


def test_external_signal_death_is_error_not_timeout(tmp_path):
    r = _spawn(OpenCodeV1Runner(binary=_sleeper(tmp_path)), tmp_path)
    os.killpg(r._p.pid, signal.SIGKILL)  # died on its own, not via kill()
    assert r.wait().exit_kind == "error"


def test_wait_without_spawn_is_error():
    out = OpenCodeV1Runner(binary="/bin/true").wait()
    assert out.exit_kind == "error" and out.returncode is None


def test_exited_tracks_process(tmp_path):
    r = _spawn(OpenCodeV1Runner(binary=_sleeper(tmp_path)), tmp_path)
    assert not r.exited()
    r.kill()
    r.wait()
    assert r.exited()


def test_spawn_env_isolates_opencode_from_global_config(tmp_path, monkeypatch):
    # opencode MERGES config layers — a developer-global ~/.config/opencode
    # (MCP servers, plugins, tools) would silently ride along with the sandbox
    # config. spawn() must pin HOME/XDG at a sandbox-private dir.
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured.update(kw)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = OpenCodeV1Runner(binary="true")
    cfg = _cfg(tmp_path)
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    r.prepare(cfg)
    r.spawn("p")
    env = captured["env"]
    home = cfg.sandbox.resolve() / "_home"
    assert env["HOME"] == str(home) and env["HOME"] != os.environ["HOME"]
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local/share")
    assert env["OPENCODE_CONFIG"] == str(cfg.sandbox.resolve() / "opencode.json")
    # the v1.18 child anchors its cwd at the project root it discovers —
    # RELATIVE pins resolve there, the config file is silently missed, and
    # opencode falls back to the developer-global config (wrong model, full
    # built-in tools). Absoluteness is the containment contract.
    for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "OPENCODE_CONFIG"):
        assert os.path.isabs(env[k])
    for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        assert env[k].startswith(str(cfg.sandbox))  # nothing leaks outside
    assert env["PATH"] == os.environ["PATH"]  # everything else inherits


def test_preflight_reports_endpoint_facts(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    monkeypatch.setattr(
        r,
        "_post",
        _fake_post(
            {
                "/models": (200, {"data": [{"id": "gemma4"}]}),
                "/chat/completions": (200, {}),
                "/api/version": (200, {"version": "fake-stack-1.0"}),
            }
        ),
    )
    info = r.preflight(_cfg(tmp_path))
    assert info["model"] == "gemma4" and info["digest"] == "fake-stack-1.0"


def test_preflight_digest_unknown_without_stack_version(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    live = {"/models": (200, {}), "/chat/completions": (200, {})}
    monkeypatch.setattr(r, "_post", _fake_post(live))  # /api/version dead
    assert r.preflight(_cfg(tmp_path))["digest"] == "unknown"
    monkeypatch.setattr(
        r, "_post", _fake_post(live | {"/api/version": (200, {"version": None})})
    )
    assert r.preflight(_cfg(tmp_path))["digest"] == "unknown"


def test_preflight_raises_when_endpoint_dead(tmp_path, monkeypatch):
    r = OpenCodeV1Runner(binary="/bin/true")
    monkeypatch.setattr(r, "_post", _fake_post({}))
    with pytest.raises(RuntimeError):
        r.preflight(_cfg(tmp_path))
    monkeypatch.setattr(
        r,
        "_post",
        _fake_post({"/models": (200, {}), "/chat/completions": (500, {})}),
    )
    with pytest.raises(RuntimeError):
        r.preflight(_cfg(tmp_path))


def test_preflight_request_shapes(tmp_path, monkeypatch):
    """Pin the wire dialect: GET /models (no body), POST chat with messages."""
    calls = []

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_method(), req.data))
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    OpenCodeV1Runner(binary="/bin/true").preflight(_cfg(tmp_path))
    by_path = {u.rsplit("/", 1)[-1]: (m, d) for u, m, d in calls}
    assert by_path["models"] == ("GET", None)
    assert by_path["version"] == ("GET", None)  # /api/version
    method, data = by_path["completions"]
    assert method == "POST"
    body = json.loads(data)
    # strict stacks (ollama) 400 on empty messages — a `[]` regression must
    # fail here, not surface as infra-error in every live slot record
    assert body["messages"] and body["max_tokens"] == 1


def test_post_surfaces_http_error_status(monkeypatch):
    """A live but 500ing endpoint reports its code, not the 0 of a dead one."""

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "busy", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = OpenCodeV1Runner(binary="/bin/true")
    assert r._post("http://x", "/models", None) == (429, {})
