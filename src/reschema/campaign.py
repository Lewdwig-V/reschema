"""Write a live campaign for corpus slots (M8 S8).

    python -m reschema.campaign write --out DIR --config RUN.toml [...] SLOT...|--all

Lays out `DIR/campaign.toml` and its tasks (each slot's function tasks, then
its program task) with `tasks.write_campaign`, building the corpus only if it
has no manifest (or, for --all, lacks a slot of the full matrix), and prints the `warranted` commands that run it. The project
allowances it prints are the campaign's worst case: every planned run spending
its run config's whole [budgets], and every task its whole check budget.
Errors are one JSON object on stderr, exit 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path

from . import tasks
from .corpus import generate

DOMAIN = "reschema.domain:ReSchemaDomain"


def _fail(reason: str, **detail) -> int:
    print(json.dumps({"accepted": False, "reason": reason, **detail}), file=sys.stderr)
    return 2


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
    try:
        budgets = [tomllib.loads(c.read_text()).get("budgets", {}) for c in a.config]
    except (OSError, tomllib.TOMLDecodeError) as e:
        return _fail("bad-config", detail=str(e))
    for c, b in zip(a.config, budgets):
        if type(b) is not dict or not all(
            type(u) is str and type(n) is int and n >= 0 for u, n in b.items()
        ):
            return _fail(
                "bad-config", detail=f"{c}: [budgets] maps units to integers >= 0"
            )

    if not (generate.OUT_ROOT / "manifest.json").exists():
        generate.build()
    try:
        manifest = {m["task_id"]: m for m in generate.load_manifest()}
        if a.all and not generate.all_task_ids() <= set(manifest):
            # a targeted build left a partial manifest; --all means every slot
            generate.build()
            manifest = {m["task_id"]: m for m in generate.load_manifest()}
    except RuntimeError as e:  # recorded under another canonicalizer
        return _fail("stale-corpus", detail=str(e))
    unknown = [s for s in a.slots if s not in manifest]
    if unknown:
        return _fail("unknown-slot", slots=unknown, available=sorted(manifest))
    metas = list(manifest.values()) if a.all else [manifest[s] for s in a.slots]

    specs = [t for m in metas for t in tasks.slot_tasks(m)]
    out.mkdir(parents=True, exist_ok=True)
    configs = {n: os.path.relpath(c.resolve(), out) for n, c in zip(names, a.config)}
    path = tasks.write_campaign(campaign_id, specs, configs, out)

    allow = Counter()
    for b in budgets:
        allow.update({u: n * len(specs) for u, n in b.items()})
    allow["check"] = len(a.config) * sum(t.check_budget or 0 for t in specs)
    allows = " ".join(f"--allow {u}={n}" for u, n in sorted(allow.items()))
    print(f"wrote {path} ({len(specs)} tasks, {len(metas)} slots)")
    print("next:")
    print(f"  uv run warranted init {project} --domain {DOMAIN} {allows}")
    print(f"  uv run warranted campaign run {project} {a.out / 'campaign.toml'}")
    print(f"  uv run warranted campaign report {project} {campaign_id}")
    print(f"  uv run python -m reschema.regrade {project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
