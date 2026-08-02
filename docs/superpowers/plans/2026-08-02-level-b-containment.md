# Implementation plan: ISSUE-11 level-B containment (2026-08-02)

Spec: `docs/superpowers/specs/2026-08-02-level-b-containment.md`. TDD throughout;
negative tests are first-class. Pods: `podman` is the runtime (hard requirement,
no hatch).

## T1 — Containerfile + infra guard
- `Containerfile.levelb` (repo root): python:3.12-slim + gcc/libc6-dev,
  `CMD ["python", "-m", "reschema.driver.native_worker"]`.
- `src/reschema/driver/podrun.py`: `IMAGE`, `BUILD_CMD`, `ensure_image()`
  (`podman image exists`), `run_worker(job, workdir, timeout)` → dict;
  non-zero rc / JSON garbage → `{"stage": "infra", ...}`.
- RED: `tests/test_podrun.py` — image check rc!=0 → RuntimeError naming
  BUILD_CMD; `validate_function` maps to `FnVerdict(stage="infra")` under
  monkeypatched absence.
- GREEN: build image locally (`podman build -t localhost/reschema-levelb:1
  -f Containerfile.levelb .`).

## T2 — worker validate core (compile + symbol + one case)
- `src/reschema/driver/native_worker.py` (stdlib only): stdin job →
  `gcc -O1 -shared -fPIC` → RTLD_NOW symbol check → ctypes call → stdout JSON.
  Stage names preserved: compile/link/symbol.
- RED: `tests/test_native_worker.py::test_validate_roundtrip` (real podman) —
  sum function model → `{"ok": true, results: [...]}` with expected `ret`.
- GREEN: minimal worker.

## T3 — case batching + marshaling parity
- Move ctypes marshaling from `driver/calling.py::call_model_native` into the
  worker (i32 / cstring / buffer_i32, poisoned out-buffers, copyfile-per-call
  dlopen-cache defeat). cstring case values hex over JSON, mem results follow
  the existing schema (buffer_i32 → list[int], cstring → bytes hex).
- RED: batch of 3 mixed-kind cases incl. out-buffer poison → results match
  hand-computed expectations; bad-hex input surfaces as structured stage.
- GREEN.

## T4 — fork-per-case crash/hang semantics
- RED: segfault model (`int*p=0;*p=1;`) → `{crash: signal 11}` at that index,
  other cases unaffected; infinite-loop model → `{crash: timeout}`; worker as a
  whole stays alive and answers.
- GREEN: `os.fork()` + pipe-per-case, wall-clock budget, SIGKILL on timeout.

## T5 — validate_function re-wire
- Shape: gen cases → host `call_original` per case (skip-starvation floor kept)
  → ONE worker round-trip → first-mismatch compare (payload shape preserved:
  input/field/expected/actual/seed, compared, skipped) → crash entries reject
  with `field: "crash"`.
- Harness: existing `tests/test_validate_function.py` stays the acceptance
  surface (RED while unwired → GREEN after). `engine.submit_function` unchanged
  externally (ledger audit fields `seed`/`n_fuzz` preserved).
- Delete `call_model_native` (+ ctypes import) once nothing references it.

## T6 — containment pins + compose/rootfs sweeps
- RED: `tests/test_native_worker.py::test_containment` — model computes the
  right answer AND attempts absolute host-path write + `socket.connect` →
  validation passes, host `/pwn` absent, worker deterministic.
- `engine.compose()` → worker `compile-link` (dup-symbol stderr still parsed
  host-side; existing compose tests are the harness).
- `driver/calling.py::call_original`: scratch rootfs + binary copy (ISSUE-02
  pattern); driver tests unchanged-green.

## T7 — CI + docs + PR
- `.github/workflows/ci.yml`: `podman build` step before pytest.
- Docs: README trust model (level-B bullet), AGENTS.md trust line, this repo's
  spec §8 guardrails bullet (level-B worker), roadmap phase-1.5 + ISSUE-11 line.
- Full suite (`uv run pytest -q`) + `uv run ruff check src tests`, then PR.

## Task checklists
- T1: Containerfile.levelb, driver/podrun.py, tests/test_podrun.py
- T2/T3/T4/T6: driver/native_worker.py, tests/test_native_worker.py
- T5: validate/function.py, engine.py, driver/calling.py, tests/test_validate_function.py
  (+ any call_model_native importers), tests/test_engine_b.py compose tests
- T7: .github/workflows/ci.yml, README.md, AGENTS.md,
  docs/superpowers/specs/2026-07-29-reschema-design.md, docs/roadmap.md
