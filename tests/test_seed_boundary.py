"""Entropy policy at the MCP boundary: the agent cannot pin its own fuzz draw.

Attack (closed here): `submit_model(function=..., seed=S)` used to forward S to
the function-mode fuzz draw, so an agent could fix the case set and iterate
first-divergence feedback against it — a memorizable fixed test suite. The tool
schema now has no `seed`, a stray `seed` from an old client never reaches the
engine, and only the server's test-only hook can pin determinism.

Pure: the engine is spied, so no corpus, podman or emulation is needed.
"""

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

import reschema.mcp.server as srv


@pytest.fixture
def engine_kwargs(monkeypatch):
    seen = {}
    monkeypatch.setattr(srv, "TaskStore", lambda task_id: object())
    monkeypatch.setattr(
        srv, "submit_function", lambda *a, **kw: seen.update(kw) or {"ok": True}
    )
    return seen


def _session(fn):
    async def go():
        async with InMemoryTransport(srv.server) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)

    return anyio.run(go)


def test_submit_model_schema_has_no_seed():
    async def schema(s):
        tools = {t.name: t for t in (await s.list_tools()).tools}
        return tools["submit_model"].input_schema["properties"]

    props = _session(schema)
    assert "seed" not in props
    assert {"task_id", "c_source", "function", "n_fuzz"} <= set(props)


def test_agent_supplied_seed_never_reaches_the_engine(engine_kwargs):
    # An old client (or an agent ignoring the schema) still sends seed=; it must
    # not pin the draw — the engine sees no seed and draws fresh entropy.
    async def submit(s):
        return await s.call_tool(
            "submit_model",
            {"task_id": "t", "c_source": "", "function": "f", "seed": 999},
        )

    _session(submit)
    assert "seed" not in engine_kwargs


def test_only_the_test_hook_pins_the_seed(engine_kwargs, monkeypatch):
    monkeypatch.setattr(srv, "TEST_PINNED_SEED", 7)

    async def submit(s):
        return await s.call_tool(
            "submit_model",
            {"task_id": "t", "c_source": "", "function": "f", "seed": 999},
        )

    _session(submit)
    assert engine_kwargs["seed"] == 7  # hook wins; the agent's 999 is ignored
