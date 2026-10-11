"""M8 S8: `python -m reschema.campaign write` and the checked-in run configs."""

import json
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
    def no_rebuild(*a, **k):
        raise AssertionError("corpus rebuilt although its manifest exists")

    monkeypatch.setattr(generate, "build", no_rebuild)
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


def test_all_rebuilds_a_partial_corpus(tmp_path, built_corpus, monkeypatch):
    # Codex #168: a targeted build leaves a partial manifest; --all must not
    # silently write a campaign over just those slots.
    class Rebuilt(Exception):
        pass

    def rebuild(*a, **k):
        raise Rebuilt

    monkeypatch.setattr(generate, "build", rebuild)
    full = generate.all_task_ids()
    assert {m["task_id"] for m in generate.load_manifest()} == full  # session corpus
    monkeypatch.setattr(generate, "all_task_ids", lambda: full | {"extra::gcc-O0-sym"})
    with pytest.raises(Rebuilt):
        campaign.main(
            ["write", "--out", str(tmp_path / "c"), "--config", QWEN, "--all"]
        )


@pytest.mark.parametrize(
    "budgets", ['model = "320"', "model = -1", "model = 1.5", "model = true"]
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
