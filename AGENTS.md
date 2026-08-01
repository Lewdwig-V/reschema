# AGENTS.md — ReSchema

Global agent instructions live in `~/.config/opencode/AGENTS.md`; this file is
project-local override/context.

## Commands

```bash
uv run pytest -q          # full suite (~2 min; qiling emulation is the cost)
uv run ruff check src tests
uv lock                   # after touching pyproject
```

Run pytest **via `uv run` only** — the venv (`.venv`, Python 3.12) carries native
deps. `gcc`+`strip` on PATH are required for corpus builds (clang also used in
the matrix).

## Conventions

- **TDD, and negative tests are first-class.** Every acceptance path ships with
  the attack it must reject (hardcoding, overfit, wrong-spec flattery, event
  splitting, void-register residue). A gate without its negative test is
  unfinished.
- **Canonicalize before diffing.** Address-shaped tokens → `ADDR_<n>` ordinals,
  `argv[0]` → basename. Changing `exec/canonical.py` rules = corpus re-record
  (see "rules v1" in its docstring).
- **Structured rejects, never tracebacks**, at every agent-facing boundary:
  `{accepted: False, reason, divergence}`. The engine is the only logic owner;
  `mcp/server.py` is dispatch-only.
- **Entropy policy:** production validation/sampling draws fresh per call
  (`secrets.token_hex(16)`); tests pin explicit seeds. Fuzz budgets are
  harness-controlled (`N_FUZZ` floor at the MCP boundary — the agent cannot
  tune its own judge).
- **Trust model is deliberate:** agent C is compiled and executed (ctypes
  native + qiling rootfs `/`). See README "Trust model".
- On-disk state under `.reschema/` (gitignored) is shared runtime state; tests
  wipe what they own (see `tests/test_hidden.py` pattern). Single-process use
  is assumed (engine module docstring).
- Commits: short imperative subject; quality-review fix rounds use
  `fix: ... (quality findings)`. Work happens on feature branches; `main`
  requires PR + green `test` check.

## Non-goals (v1)

See spec doc `docs/superpowers/specs/2026-07-29-reschema-design.md` §/heading
"Descoped for v1" (multi-arch, packing, symbolic checks, struct-by-value,
float/vector regs, level-B syscall comparison, >6 int args, ...). Don't build
them speculatively.
