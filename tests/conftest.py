"""Session-wide test wiring: wall-clock budget and one shared corpus build.

Budget: the docs promise the full suite in ~2 minutes. The autouse fixture
makes that a hard bound — a suite that burns past it exits failed
(RESCHEMA_TEST_BUDGET_S overrides the default for profiling runs).

Corpus: build() is ALWAYS a full rebuild (60 slots, two container rounds,
~9s); one session-scoped build replaces the per-module copies.
"""

import os
import shutil
import tempfile
import time

import pytest

# pytest-xdist: give each worker its own .reschema root so task dirs, ledgers,
# memory, and the corpus manifest can't race across processes. Must run at
# conftest import time — reschema's ROOT constants read the env at THEIR import.
# _OWN_ROOT is the ONE root this process made (#76): sessionfinish deletes
# exactly it — never a path a user pointed RESCHEMA_HOME at.
_OWN_ROOT: str | None = None
if "PYTEST_XDIST_WORKER" in os.environ:
    _OWN_ROOT = tempfile.mkdtemp(
        prefix=f"reschema-{os.environ['PYTEST_XDIST_WORKER']}-"
    )
    os.environ["RESCHEMA_HOME"] = _OWN_ROOT

BUDGET_S = int(os.environ.get("RESCHEMA_TEST_BUDGET_S", "120"))
_STARTED = time.monotonic()


@pytest.fixture(autouse=True)
def _suite_time_budget():
    """The documented 2-minute wall-clock budget is a hard failure."""
    if time.monotonic() - _STARTED > BUDGET_S:
        pytest.exit(
            f"test suite exceeded its documented {BUDGET_S}s wall-clock budget",
            returncode=2,
        )


def pytest_sessionfinish(session, exitstatus):
    """Setup-time checks can't catch a budget-busting FINAL test (no later
    setup to trigger them) — the suite-end verdict lives here."""
    over = time.monotonic() - _STARTED - BUDGET_S
    if over > 0:
        print(
            f"\ntest suite exceeded its documented {BUDGET_S}s wall-clock "
            f"budget (+{over:.1f}s)"
        )
        session.exitstatus = 2
    if _OWN_ROOT:
        # otherwise each xdist run leaks a ~30MB corpus-carrying root in /tmp
        shutil.rmtree(_OWN_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def built_corpus():
    """The 60-slot corpus, built once per test session."""
    from reschema.corpus.generate import build

    return build()


def mcp_call(tool, **kw):
    """One in-process MCP tool call; structured_content unwrapped, errors
    asserted. Shared by test_mcp/test_dogfood (was two hand-rolled copies).

    reschema import stays lazy: RESCHEMA_HOME is set at conftest import time
    above and reschema's ROOT constants read the env at THEIR import.
    """
    import json

    import anyio
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    from reschema.mcp.server import server

    async def go():
        async with InMemoryTransport(server) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, kw)
            assert not res.is_error, res.content
            sc = res.structured_content
            if sc is not None:
                return sc["result"] if set(sc) == {"result"} else sc
            return json.loads(res.content[0].text)

    return anyio.run(go)


def wipe_task(store):
    """Clean slate for a shared-runtime-state task dir: traces + ledger gone."""
    for p in store.dir.glob("trace_*.json"):
        p.unlink()
    store._path("ledger.json").unlink(missing_ok=True)
