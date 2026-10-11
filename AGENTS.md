# AGENTS.md — ReSchema

Global agent instructions live in `~/.config/opencode/AGENTS.md`; this file is
project-local override/context.

## Commands

```bash
uv run pytest -q -n auto  # full suite: 150s wall-clock budget (conftest
                          # enforces, per xdist worker); exceeding it fails the run.
                          # A guard against test-time creep: speed a slow test up
                          # before raising it
uv run ruff check src tests
uv run pytest -q -n auto -m golden  # M8 parity fixture (tests/golden/), excluded
                          # from the default run; CI gives it its own 300s budget
uv lock                   # after touching pyproject
```

Tests run under **pytest-xdist** (`-n auto`): every worker gets its own
`.reschema/` root via `RESCHEMA_HOME`, and the 60-slot corpus builds once per
session (never per module — `build()` is always a full rebuild). A test that
only passes in a specific ordering or with a specific worker layout is a bug:
fix the test, not the scheduler.

Run pytest **via `uv run` only** — the venv (`.venv`, Python 3.12) carries native
deps. Rootless **podman** is the hard dependency: every compile (corpus matrix,
`strip`, every model) and every checker job runs inside the pinned
`localhost/reschema-toolchain:1` image — get it once with
`tools/toolchain_image.sh`, which pulls the build CI
published to `ghcr.io/lewdwig-v/reschema-toolchain:<sha256 of the sha256sum manifest of Containerfile, checker-requirements.txt, tools/worker/*>[:16]`
and falls back to `podman build -t localhost/reschema-toolchain:1 -f
Containerfile .`. Sandboxes that block the snapshot.debian.org apt mirror need
the pull (blobs come from pkg-containers.githubusercontent.com). No host
`gcc`/`strip` is needed (or used) anywhere.

## Running a slot on Warranted

ReSchema is a Warranted domain (`reschema.domain:ReSchemaDomain`, Warranted
pinned at v0.4.0). Warranted owns task state, the ledger, accounting, budgets,
the duplicate guard, containment and memory; ReSchema owns the corpus, ground
truth, canonicalisation, the `function-fuzz` and `program-replay` checkers,
the `experiment`/`experiment_function` operations and the task specs. See
`docs/proposals/m8-rebuild-on-warranted.md` (decisions D1–D3).

```bash
uv run python -m reschema.campaign write --out results/ID \
    --config campaigns/run-qwen-local.toml SLOT...  # or --all; prints the rest
uv run warranted init results/ID/project --domain reschema.domain:ReSchemaDomain \
    --allow model=N --allow tool=N --allow check=N --allow probe=N
uv run warranted campaign run results/ID/project results/ID/campaign.toml
uv run warranted campaign report results/ID/project ID
uv run python -m reschema.regrade results/ID/project [--run R] [--submission N]
```

Live campaigns: run configs and the smoke slots are in `campaigns/`
(`campaigns/README.md`). Campaigns, projects and the OpenRouter key file
(`results/.secrets/openrouter`, mode 600: litellm reads keys from a file,
never the environment) stay under `results/`, which is gitignored. Run from
the repo root: the key path is relative to the working directory.

The worker image is the toolchain image: the `Containerfile` copies
`tools/worker/` (`experiment`, `experiment-function`, `submit`) onto PATH, so
the worker, the checkers' jobs and the corpus build share one image ID
(`toolchain.image_id()`, the domain's `worker_image`). Changing a helper means
a new image tag. A project pins the domain identity (package sources, image,
Warranted build): rebuilding the image or editing `src/reschema` needs a new
project; `reschema.regrade` reads an old project's ledger directly.

## Conventions

- **TDD, and negative tests are first-class.** Every acceptance path ships with
  the attack it must reject (hardcoding, overfit, wrong-spec flattery, event
  splitting, void-register residue, forged job output). A gate without its
  negative test is unfinished.
- **Parity is pinned.** `tests/golden/m8_decisions.json` is the old gates'
  recorded decisions; the checkers must reproduce every one (golden run).
- **Canonicalize before diffing.** Address-shaped tokens → `ADDR_<n>` ordinals,
  `argv[0]` → basename. Changing `exec/canonical.py` rules = corpus rebuild
  (see "rules v2" in its docstring; `CANONICALIZER_VERSION = "2.1"`), and the
  version is in each task's `family.json`, so it marks old memory stale.
- **Structured rejects, never tracebacks**, for anything the worker controls:
  a checker returns `Verdict(REJECTED, {accepted: False, reason?, stage?,
  divergence?, detail?})` (exact keys vary by checker, see ARCHITECTURE.md
  §"The rejection payload"). Only a host-side fault (missing image, runner
  failure) raises, which Warranted records as an infrastructure failure. Job
  output is the model's claim: shape-check it, judge it on the host.
- **Entropy policy:** checkers draw seeds only through `ctx.draw_seed()`
  (fresh per check, recorded as check evidence); tests pin explicit seeds.
  Fuzz and hidden budgets (`N_FUZZ`, `HIDDEN_N`) are checker constants, never
  worker input.
- **Trust model is deliberate (D1):** agent C is compiled and executed only in
  Warranted jobs (no network, no mounts, read-only root); ground truth is
  recorded on the host under qiling against a fresh empty rootfs. The corpus
  build (`toolchain.run_build`) is the only bind-mounted container, and it
  only ever sees ReSchema's own seed sources. See README "Trust model".
- `.reschema/` (gitignored) holds the corpus; `RESCHEMA_HOME` overrides its
  root — set it for installed-layout runs, where the default would land
  `.reschema/` under site-packages (`corpus/generate.py` `ROOT`).
- Commits: short imperative subject; quality-review fix rounds use
  `fix: ... (quality findings)`. Work happens on feature branches; `main`
  requires PR + green `test` check.

## Non-goals (v1)

See `ARCHITECTURE.md` §"Decision records", last entry ("Scope guardrails
observed"): multi-arch, packing, symbolic checks, struct-by-value,
float/vector regs, level-B syscall comparison, >6 int args, ... Don't build
them speculatively.
