# AGENTS.md — ReSchema

Global agent instructions live in `~/.config/opencode/AGENTS.md`; this file is
project-local override/context.

## Commands

```bash
uv run pytest -q -n auto  # full suite: 120s HARD wall-clock budget (conftest
                          # enforces, per xdist worker); exceeding it fails the run
uv run ruff check src tests
uv lock                   # after touching pyproject
```

Tests run under **pytest-xdist** (`-n auto`): every worker gets its own
`.reschema/` root via `RESCHEMA_HOME`, and the 48-slot corpus builds once per
session (never per module — `build()` is always a full rebuild). A test that
only passes in a specific ordering or with a specific worker layout is a bug:
fix the test, not the scheduler.

Run pytest **via `uv run` only** — the venv (`.venv`, Python 3.12) carries native
deps. Rootless **podman** is the hard dependency: every compile (corpus matrix,
model checks, `strip`) runs inside the pinned `localhost/reschema-toolchain:1`
image — build it once with `podman build -t localhost/reschema-toolchain:1 -f
Containerfile .`. No host `gcc`/`strip` is needed (or used) anywhere.

## Conventions

- **TDD, and negative tests are first-class.** Every acceptance path ships with
  the attack it must reject (hardcoding, overfit, wrong-spec flattery, event
  splitting, void-register residue). A gate without its negative test is
  unfinished.
- **Canonicalize before diffing.** Address-shaped tokens → `ADDR_<n>` ordinals,
  `argv[0]` → basename. Changing `exec/canonical.py` rules = corpus re-record
  (see "rules v2" in its docstring; `CANONICALIZER_VERSION = "2.1"`).
- **Structured rejects, never tracebacks**, at every agent-facing boundary:
  `{accepted: False, reason?, divergence?, detail?}` — exact keys vary by
  path (see ARCHITECTURE.md §"The rejection payload"); function-floor rejects
  nest `detail` under `divergence`, they are not top-level. The engine is the
  only logic owner; `mcp/server.py` is dispatch-only.
- **Entropy policy:** production validation/sampling draws fresh per call
  (`secrets.token_hex(16)`); tests pin explicit seeds. Fuzz budgets are
  harness-controlled (`N_FUZZ` floor at the MCP boundary — the agent cannot
  tune its own judge).
- **Trust model is deliberate:** agent C is compiled and executed — level A
  (qiling vs a fresh empty rootfs), level B (mandatory one-shot rootless podman
  worker; podman is a hard dependency for all build/validate/test flows).
  ALL compiles — corpus matrix and both levels' models — run inside the one
  pinned toolchain image (`Containerfile`); host gcc is never used. See README "Trust model".
- On-disk state under `.reschema/` (gitignored) is shared runtime state; tests
  wipe what they own (see `tests/test_hidden.py` pattern). Single-process use
  is assumed (engine module docstring).
- `tools/dogfood/` is benchmark tooling (phase 2C), NOT shipped in the
  `reschema` package; its tests live in `tests/dogfood2c/` and must stay
  CI-safe (no LLM, no podman, no endpoint).

## 2C smoke campaign (manual)

```bash
uv run python -m tools.dogfood.driver tools/dogfood/campaigns/smoke.toml \
    --out results/smoke          # [--pool N]; needs an opencode binary, an
                                 # endpoint (RESCHEMA_2C_ENDPOINT), and
                                 # .reschema/corpus (`uv run python -m
                                 # reschema.corpus.generate` first)
```

Before trusting a run:
- Keep `--out` INSIDE the repo: the slot's MCP server is `uv run` from the
  sandbox under the results dir; an out-of-repo `--out` breaks launcher
  discovery and masquerades as agent failure.
- After the first smoke slot, read its transcript_tail: the 5 reschema tools
  (corpus_build, task_open, experiment, submit_model, status) must be visible.
- Review the report's abort classes BEFORE interpreting φ. Mid-slot endpoint
  death POST-preflight surfaces as `aborted: agent-exit` / `aborted: timeout`
  — an abort-class signature to recognize, not agent failure.
- Housekeeping: a killed driver leaves `<out>/runs` staging roots and stray
  `opencode` processes behind; both are restart-free — kill/delete them
  freely between campaigns.
- Commits: short imperative subject; quality-review fix rounds use
  `fix: ... (quality findings)`. Work happens on feature branches; `main`
  requires PR + green `test` check.

## Non-goals (v1)

See `ARCHITECTURE.md` §"Decision records", last entry ("Scope guardrails
observed"): multi-arch, packing, symbolic checks, struct-by-value,
float/vector regs, level-B syscall comparison, >6 int args, ... Don't build
them speculatively.
