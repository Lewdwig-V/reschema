"""Write a live campaign for corpus slots (M8 S8).

    python -m reschema.campaign write --out DIR --config RUN.toml [...] SLOT...|--all

Rebuilds the full corpus, lays out `DIR/campaign.toml` and its tasks (each
slot's function tasks, then its program task) with `tasks.write_campaign`, and
prints the `warranted` commands that run it, shell-quoted. The project
allowances it prints are the campaign's worst case: every planned run spending
its run config's whole [budgets], and every task its whole check budget.
Operator errors are one JSON object on stderr, exit 2; a build failure is a
host failure and raises.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tomllib
from collections import Counter
from pathlib import Path

from warranted import LocalChatCompletions, RunConfig

from . import tasks
from .corpus import generate

DOMAIN = "reschema.domain:ReSchemaDomain"
# Units every run spends: steps cost model and tool, experiments cost probe.
REQUIRED_BUDGETS = {"model", "tool", "probe"}
LITELLM_REQUIRED = {"api_key_file", "max_tokens", "timeout_seconds"}
LITELLM_FIELDS = LITELLM_REQUIRED | {"api_base", "parameters"}
OLLAMA_URL = "http://127.0.0.1:11434/v1"  # Warranted's default local endpoint


def _int_in(n, lo: int, hi: int) -> bool:
    return type(n) is int and lo <= n <= hi


def _fail(reason: str, **detail) -> int:
    print(json.dumps({"accepted": False, "reason": reason, **detail}), file=sys.stderr)
    return 2


def _budgets(data: dict) -> dict:
    """The run config's [budgets], after checking the whole config the way
    `warranted run` would (its loader is private: warranted#78), short of
    reading the litellm key file, which may not exist until run time."""
    known = {"model", "provider", "max_steps", "budgets", "adapter"}
    if not data.keys() <= known or type(data.get("model")) is not str:
        raise ValueError(f"needs a model string, and only {sorted(known)}")
    provider = data.get("provider", "ollama")
    adapter = data.get("adapter", {})
    if provider not in ("ollama", "litellm") or type(adapter) is not dict:
        raise ValueError("provider is ollama or litellm; [adapter] is a table")
    adapter = dict(adapter)
    if provider == "ollama":
        # Warranted's own adapter: its construction validates every field and
        # touches nothing (no request until a run)
        LocalChatCompletions(
            adapter.pop("base_url", OLLAMA_URL), data["model"], **adapter
        )
    else:
        # The litellm adapter reads the key and probes litellm when built, so
        # mirror its field rules instead; the key file is read at run time.
        if not LITELLM_REQUIRED <= adapter.keys() <= LITELLM_FIELDS:
            raise ValueError(
                f"a litellm [adapter] needs {sorted(LITELLM_REQUIRED)}, "
                f"and only {sorted(LITELLM_FIELDS)}"
            )
        if "/" not in data["model"].strip("/") or (
            type(adapter["api_key_file"]) is not str
            or not _int_in(adapter["max_tokens"], 1, 131072)
            or not _int_in(adapter["timeout_seconds"], 1, 600)
            or type(adapter.get("api_base", "")) is not str
            or type(adapter.get("parameters", {})) is not dict
        ):
            raise ValueError(
                "litellm: model is provider/model; api_key_file a path; "
                "max_tokens 1..131072; timeout_seconds 1..600"
            )
    b = data.get("budgets", {})
    # explicit, so the printed allowances cover every unit a run spends
    if (
        type(b) is not dict
        or not REQUIRED_BUDGETS <= set(b)
        or not all(type(u) is str and type(n) is int and n >= 0 for u, n in b.items())
    ):
        raise ValueError(
            f"[budgets] must set {sorted(REQUIRED_BUDGETS)} "
            "(and any other units) to integers >= 0"
        )
    RunConfig(data["model"], data.get("max_steps", 4), b)  # Warranted's own checks
    return b


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m reschema.campaign")
    sub = p.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="write campaign.toml and task files")
    w.add_argument("--out", type=Path, required=True)
    w.add_argument("--id", help="campaign id (default: the --out directory name)")
    w.add_argument(
        "--config",
        type=Path,
        action="append",
        required=True,
        help="run config TOML (repeatable); named by its file stem",
    )
    w.add_argument("--project", type=Path, help="project dir (default: OUT/project)")
    w.add_argument("slots", nargs="*", metavar="SLOT")
    w.add_argument("--all", action="store_true", help="every corpus slot")
    a = p.parse_args(argv)
    if bool(a.slots) == a.all:
        p.error("give SLOT... or --all")

    out = a.out.resolve()
    campaign_id = a.id or out.name
    project = a.project or a.out / "project"
    names = [c.stem for c in a.config]
    if len(set(names)) != len(names):
        return _fail("duplicate-config-name", configs=names)
    budgets = []
    for c in a.config:
        try:
            budgets.append(_budgets(tomllib.loads(c.read_text())))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            return _fail("bad-config", detail=f"{c}: {e}")
        except (ValueError, TypeError) as e:
            return _fail("bad-config", detail=f"{c}: {e}")

    current = generate.all_task_ids()
    unknown = [s for s in a.slots if s not in current]
    if unknown:
        return _fail("unknown-slot", slots=unknown, available=sorted(current))
    # Always a full rebuild (~10s): a kept manifest can predate the current
    # seeds, flags or toolchain, while family.json hashes today's sources. Run
    # projects pin their binaries as task bytes, so this never moves one. A
    # build failure is a host failure and propagates.
    generate.build()
    manifest = {m["task_id"]: m for m in generate.load_manifest()}
    if a.all:  # the manifest's canonical order: -sym variants before -stripped,
        # so a seed's easier slots can leave verified facts for its harder ones
        metas = [m for i, m in manifest.items() if i in current]
    else:
        metas = [manifest[s] for s in a.slots]

    specs = [t for m in metas for t in tasks.slot_tasks(m)]
    out.mkdir(parents=True, exist_ok=True)
    configs = {n: os.path.relpath(c.resolve(), out) for n, c in zip(names, a.config)}
    path = tasks.write_campaign(campaign_id, specs, configs, out)

    allow = Counter()
    for b in budgets:
        allow.update({u: n * len(specs) for u, n in b.items()})
    allow["check"] = len(a.config) * sum(t.check_budget or 0 for t in specs)
    allows = [x for u, n in sorted(allow.items()) for x in ("--allow", f"{u}={n}")]
    print(f"wrote {path} ({len(specs)} tasks, {len(metas)} slots)")
    print("next:")
    for cmd in (
        ["uv", "run", "warranted", "init", str(project), "--domain", DOMAIN, *allows],
        [
            "uv",
            "run",
            "warranted",
            "campaign",
            "run",
            str(project),
            str(a.out / "campaign.toml"),
        ],
        ["uv", "run", "warranted", "campaign", "report", str(project), campaign_id],
        ["uv", "run", "python", "-m", "reschema.regrade", str(project)],
    ):
        print("  " + shlex.join(cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
