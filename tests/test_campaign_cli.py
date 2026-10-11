"""M8 S8: `python -m reschema.campaign write` and the checked-in run configs."""

import json
import shlex
import tomllib
from pathlib import Path

import pytest
from warranted import RunConfig

from reschema import campaign, tasks
from reschema.corpus import generate

CAMPAIGNS = Path(__file__).resolve().parents[1] / "campaigns"
QWEN = str(CAMPAIGNS / "run-qwen-local.toml")


def test_write_orders_tasks_and_prints_allowances(
    tmp_path, built_corpus, monkeypatch, capsys
):
    builds = []
    monkeypatch.setattr(generate, "build", lambda *a, **k: builds.append(1))
    out = tmp_path / "smoke"
    argv = ["write", "--out", str(out), "--config", QWEN]
    assert campaign.main([*argv, "rot13::gcc-O2-sym", "check::gcc-O1-stripped"]) == 0

    plan = tomllib.loads((out / "campaign.toml").read_text())
    assert plan["id"] == "smoke"
    assert (out / plan["configs"]["run-qwen-local"]).resolve() == Path(QWEN)
    ids = [tomllib.loads((out / t["path"]).read_text())["id"] for t in plan["tasks"]]
    assert ids == [
        "rot13::gcc-O2-sym::fn::rot13_char",
        "rot13::gcc-O2-sym::fn::rot13",
        "rot13::gcc-O2-sym::program",
        "check::gcc-O1-stripped::fn::pw_hash",
        "check::gcc-O1-stripped::fn::check_pw",
        "check::gcc-O1-stripped::program",
    ]
    printed = capsys.readouterr().out
    # 6 tasks x the run budgets, 6 tasks x 8 checks
    assert (
        "--allow check=48 --allow model=1920 --allow probe=1536 --allow tool=1920"
        in printed
    )
    assert f"warranted campaign report {out / 'project'} smoke" in printed
    assert builds == [1]  # always a full rebuild, never a stale manifest


def test_unknown_slot_is_a_structured_reject(tmp_path, built_corpus, capsys):
    argv = ["write", "--out", str(tmp_path / "c"), "--config", QWEN]
    assert campaign.main([*argv, "rot13::gcc-O2-sym", "nope::x"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["accepted"] is False
    assert err["reason"] == "unknown-slot"
    assert err["slots"] == ["nope::x"]
    assert not (tmp_path / "c").exists()


@pytest.mark.parametrize("name", ["run-qwen-local.toml", "run-glm-openrouter.toml"])
def test_checked_in_run_configs_are_valid_run_configs(name):
    data = tomllib.loads((CAMPAIGNS / name).read_text())
    RunConfig(data["model"], data["max_steps"], data["budgets"])
    assert data["budgets"]["model"] >= data["max_steps"] * tasks.SUBMISSIONS


def test_all_skips_obsolete_manifest_entries(tmp_path, built_corpus, monkeypatch):
    # Codex #168: another branch's leftover entry must not reach slot_tasks
    monkeypatch.setattr(generate, "build", lambda *a, **k: None)
    real = generate.load_manifest()
    stale = {**real[0], "task_id": "gone::gcc-O0-sym", "seed": "gone"}
    monkeypatch.setattr(generate, "load_manifest", lambda *a, **k: [*real, stale])
    out = tmp_path / "c"
    assert campaign.main(["write", "--out", str(out), "--config", QWEN, "--all"]) == 0
    plan = tomllib.loads((out / "campaign.toml").read_text())
    assert not any("gone" in t["path"] for t in plan["tasks"])


def test_an_obsolete_slot_is_unknown(tmp_path, capsys):
    argv = ["write", "--out", str(tmp_path / "c"), "--config", QWEN, "gone::gcc-O0-sym"]
    assert campaign.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "unknown-slot"


def test_a_build_failure_propagates(tmp_path, monkeypatch):
    # Codex #168: a podman/compile failure is the host's, not a "stale-corpus"
    def broken(*a, **k):
        raise RuntimeError("podman: image missing")

    monkeypatch.setattr(generate, "build", broken)
    argv = ["write", "--out", str(tmp_path / "c"), "--config", QWEN]
    with pytest.raises(RuntimeError, match="image missing"):
        campaign.main([*argv, "rot13::gcc-O2-sym"])


def test_printed_commands_are_shell_quoted(tmp_path, built_corpus, monkeypatch, capsys):
    # Codex #168: a path with spaces must survive copy-paste
    monkeypatch.setattr(generate, "build", lambda *a, **k: None)
    out = tmp_path / "my run"
    argv = ["write", "--out", str(out), "--config", QWEN, "rot13::gcc-O2-sym"]
    assert campaign.main(argv) == 0
    lines = [ln.strip() for ln in capsys.readouterr().out.splitlines()]
    run = next(shlex.split(ln) for ln in lines if " campaign run " in ln)
    assert run[-2:] == [str(out / "project"), str(out / "campaign.toml")]


@pytest.mark.parametrize(
    "budgets",
    [
        'model = "320"\ntool = 1\nprobe = 1',
        "model = -1\ntool = 1\nprobe = 1",
        "model = 1.5\ntool = 1\nprobe = 1",
        "model = true\ntool = 1\nprobe = 1",
        # Codex #168: a missing unit would print an init that can't run
        "model = 1\ntool = 1",
        "",
    ],
)
def test_non_integer_budgets_are_a_structured_reject(tmp_path, capsys, budgets):
    # Codex #168: these used to reach the allowance arithmetic and raise
    cfg = tmp_path / "bad.toml"
    cfg.write_text(f'model = "m"\nprovider = "ollama"\n\n[budgets]\n{budgets}\n')
    argv = ["write", "--out", str(tmp_path / "c"), "--config", str(cfg)]
    assert campaign.main([*argv, "rot13::gcc-O2-sym"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["reason"] == "bad-config" and "[budgets]" in err["detail"]
    assert not (tmp_path / "c").exists()


def test_an_undecodable_config_is_a_structured_reject(tmp_path, capsys):
    # Codex #168: read_text raised UnicodeDecodeError past the bad-config path
    cfg = tmp_path / "bad.toml"
    cfg.write_bytes(b"\xff\xfe model")
    argv = ["write", "--out", str(tmp_path / "c"), "--config", str(cfg)]
    assert campaign.main([*argv, "rot13::gcc-O2-sym"]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "bad-config"


@pytest.mark.parametrize(
    "config",
    [
        'provider = "ollama"\n',  # no model
        "model = 1\n",
        'model = "m"\nprovider = "openrouter"\n',
        'model = "m"\nprovider = "litellm"\n[adapter]\nmax_tokens = 1\n',
        'model = "m"\nmax_steps = 0\n',
        'model = "m"\nextra = 1\n',
        # Codex #168: adapter values, not only keys
        'model = "m"\n[adapter]\nmax_tokens = "many"\n',
        'model = "m"\n[adapter]\nbase_url = "http://example.com/v1"\n',
        (
            'model = "a/b"\nprovider = "litellm"\n[adapter]\napi_key_file = 1\n'
            "max_tokens = 1\ntimeout_seconds = 1\n"
        ),
        (
            'model = "a/b"\nprovider = "litellm"\n[adapter]\napi_key_file = "k"\n'
            'max_tokens = "many"\ntimeout_seconds = 1\n'
        ),
        (
            'model = "a/b"\nprovider = "litellm"\n[adapter]\napi_key_file = "k"\n'
            "max_tokens = 1\ntimeout_seconds = false\n"
        ),
        (
            'model = "a/b"\nprovider = "litellm"\n[adapter]\napi_key_file = ""\n'
            "max_tokens = 1\ntimeout_seconds = 1\n"
        ),
        (
            'model = "nomodel"\nprovider = "litellm"\n[adapter]\napi_key_file = "k"\n'
            "max_tokens = 1\ntimeout_seconds = 1\n"
        ),
    ],
)
def test_the_whole_run_config_is_checked_before_writing(tmp_path, capsys, config):
    # Codex #168: only [budgets] was checked, so a config Warranted refuses
    # still produced a campaign
    cfg = tmp_path / "bad.toml"
    cfg.write_text(config + "\n[budgets]\nmodel = 1\ntool = 1\nprobe = 1\n")
    argv = ["write", "--out", str(tmp_path / "c"), "--config", str(cfg)]
    assert campaign.main([*argv, "rot13::gcc-O2-sym"]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "bad-config"
    assert not (tmp_path / "c").exists()


def test_glm_config_writes_before_its_key_exists(tmp_path, built_corpus, monkeypatch):
    # the key file is read at run time, so a campaign can be laid out first
    monkeypatch.setattr(generate, "build", lambda *a, **k: None)
    glm = str(CAMPAIGNS / "run-glm-openrouter.toml")
    out = tmp_path / "glm"
    assert (
        campaign.main(
            ["write", "--out", str(out), "--config", glm, "rot13::gcc-O2-sym"]
        )
        == 0
    )
    assert (out / "campaign.toml").exists()


def test_all_keeps_the_manifests_canonical_order(tmp_path, built_corpus, monkeypatch):
    # Codex #168: sorting ids put each -stripped slot before its -sym sibling,
    # starving the harder task of the facts memory would hand it
    monkeypatch.setattr(generate, "build", lambda *a, **k: None)
    out = tmp_path / "all"
    assert campaign.main(["write", "--out", str(out), "--config", QWEN, "--all"]) == 0
    plan = tomllib.loads((out / "campaign.toml").read_text())
    slots = []
    for t in plan["tasks"]:
        slot = tomllib.loads((out / t["path"]).read_text())["id"].split("::")[:2]
        if slot not in slots:
            slots.append(slot)
    assert ["::".join(s) for s in slots] == [
        m["task_id"] for m in generate.load_manifest()
    ]
