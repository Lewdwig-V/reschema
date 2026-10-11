# Live campaigns

Run configurations for live Warranted campaigns, and the first smoke's slots.
Campaign files, projects and keys live under `results/` (gitignored), never
here.

| File | Model |
| --- | --- |
| `run-qwen-local.toml` | `qwen3.8:27b` on local Ollama (`http://127.0.0.1:11434/v1`) |
| `run-glm-openrouter.toml` | `openrouter/z-ai/glm-5.3-flash` via litellm; key in `results/.secrets/openrouter` (mode 600, run from the repo root) |

Both allow 40 steps per episode and, per run, 320 model calls, 320 commands
and 256 probes: with a task's 8 submissions, the submissions bind first.

## Smoke slots

| Slot | Input | Why |
| --- | --- | --- |
| `rot13::gcc-O2-sym` | argv | Easy: the slot the scripted golden campaign drives to two acceptances (`tests/test_cli_campaign.py`), so a failure here is the model, not the pipeline. |
| `check::gcc-O1-stripped` | stdin | Medium: stdin input, a stripped binary (no symbols in the disassembly), and a hash to recover exactly. |
| `calc::gcc-O2-sym` | argv | Medium: three function tasks, one taking a buffer (`scale_buf`), whose verified facts the program task should reuse. |

10 tasks: each slot's function tasks, then its program task.

## Allowances

`python -m reschema.campaign write` prints the project's `--allow` flags: every
planned run spending its whole `[budgets]`, and every task its 8 checks. For
the smoke under one run config: `model=3200 tool=3200 probe=2560 check=80`.

## Local smoke

```bash
uv run python -m reschema.campaign write --out results/smoke-qwen \
    --config campaigns/run-qwen-local.toml \
    rot13::gcc-O2-sym check::gcc-O1-stripped calc::gcc-O2-sym
uv run warranted init results/smoke-qwen/project \
    --domain reschema.domain:ReSchemaDomain \
    --allow check=80 --allow model=3200 --allow probe=2560 --allow tool=3200
uv run warranted campaign run results/smoke-qwen/project results/smoke-qwen/campaign.toml
uv run warranted campaign report results/smoke-qwen/project smoke-qwen
uv run python -m reschema.regrade results/smoke-qwen/project
```

For GLM, the same with `results/smoke-glm` and
`--config campaigns/run-glm-openrouter.toml`, after writing the key to
`results/.secrets/openrouter` with `chmod 600`.
