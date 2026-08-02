# Level-B containment: mandatory podman worker (2026-08-02)

## Why
Level B differential-fuzzing compiles agent C and runs it natively in the engine
process (ctypes), i.e. arbitrary machine code with the harness user's privileges
on every `submit_model(function=...)`. Level A gained structural containment via
isolated rootfs (ISSUE-02); this spec gives level B the equivalent guarantee:
**no agent-authored source ever touches the host toolchain or host process.**

## Goals
- Compile, symbol-check, and ALL native case calls execute inside a one-shot,
  rootless podman container per validation round.
- Hard mandatory: no native fallback. Absent podman/image → structured
  `{stage: "infra"}` rejection naming the exact build command.
- Native fuzz speed preserved (one container per submission, all N_FUZZ calls
  inside it). Verdict composition stays on the host.
- Fork-per-case: model segfault/hang maps to a structured per-case crash
  rejection instead of wedging or killing the harness (removes the documented
  `call_model_native` ceiling).
- `compose()` compile/link moves the same way; host `gcc` never sees agent source.

## Non-goals
- Batching `call_original` qiling runs (perf follow-up, spec unchanged).
- Anything about level A (unchanged) or network/egress policy for the engine
  host itself.
- Executing composed binaries (nothing does today).

## Architecture
### Container contract
`podman run --rm -i --network none --read-only --tmpfs /tmp:rw,size=64m
  --memory 1g --pids-limit 128
  -v <repo src>:/app:ro -e PYTHONPATH=/app
  -v <workdir>:/work:rw
  localhost/reschema-levelb:1` (image CMD runs the worker module).
stdin = job JSON, stdout = result JSON. Non-zero exit/timeout → `{stage: infra}`.
The worker ships with the repo (mounted ro), not baked into the image: image
rebuilds are only for toolchain changes.

Image: `Containerfile.levelb` = `python:3.12-slim` + `gcc`, `libc6-dev`; tag
`localhost/reschema-levelb:1`. CI (`ci.yml`) builds it before pytest.

### Worker protocol (`driver/native_worker.py`, pure stdlib)
Job `validate`: `{mode, c_source, fname, params, cases}` — params in the same
dict shape `Param.from_json` accepts; cstring case values are hex (engine-wide
JSON convention). Result:
- `{"ok": true, "results": [{ret, mem}, ...]}` — one entry per case; a crashed/
  hung case yields `{crash: {signal: N}}`/`{crash: {timeout: true}}` at its index.
- `{"stage": "compile"|"link"|"symbol", "stderr"|"detail": ...}` on failure.
Per-case calls fork (posix, stdlib) with a wall-clock budget; dlopen caching is
defeated exactly as today (copyfile-per-call into /tmp tmpfs).
Job `compile-link`: `{mode, sources: {name: src}, objects: [...], out: name}` —
per-TU compile + link; `{ok, stderr}`; duplicate-symbol stderr parsing stays
host-side.

### validate_function re-shape
`gen_inputs` list → host qiling `call_original` for all cases (unchanged
semantics, scratch rootfs now) → one worker `validate` round-trip → host-side
compare (first-mismatch divergence, existing payload shape + `seed`). Skip/
starvation floors unchanged (`compared == 0` rejects). Crashed/hung model cases
reject as divergence (`field: "crash"`): the model's fault, not the input's.

### Deleted
`call_model_native`, all ctypes from the engine process. `README`/`AGENTS.md`
trust lines rewritten: level B = "agent code executes only inside throwaway
rootless podman containers; host runs python + qiling only."

## Acceptance
- Containment: model writing absolute host paths / opening a socket validates
  deterministically (ro root, `--network none`); host FS/network untouched.
- Negative-first-class: segfault crash reject, infinite-loop crash reject,
  compile/link/symbol stages preserved, infra reject on missing image.
- Full suite, dogfood, CI green with the image build step.
