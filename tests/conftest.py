"""Session-wide test wiring: wall-clock budget and one shared corpus build.

Budget: a guard against test-time creep, not a promise. The autouse fixture
fails a suite that runs past BUDGET_S (150s; RESCHEMA_TEST_BUDGET_S overrides
it for profiling runs). Raised from 120s once the suite sat at ~117s and
runner variance alone tipped it over.

Corpus: build() is ALWAYS a full rebuild (60 slots, two container rounds,
~9s); one session-scoped build replaces the per-module copies.
"""

import os
import shutil
import tempfile
import time

import pytest

# pytest-xdist: give each worker its own .reschema root so corpus builds
# can't race across processes. Must run at
# conftest import time — reschema's ROOT constants read the env at THEIR import.
# _OWN_ROOT is the ONE root this process made (#76): sessionfinish deletes
# exactly it — never a path a user pointed RESCHEMA_HOME at.
_OWN_ROOT: str | None = None
if "PYTEST_XDIST_WORKER" in os.environ:
    _OWN_ROOT = tempfile.mkdtemp(
        prefix=f"reschema-{os.environ['PYTEST_XDIST_WORKER']}-"
    )
    os.environ["RESCHEMA_HOME"] = _OWN_ROOT

BUDGET_S = int(os.environ.get("RESCHEMA_TEST_BUDGET_S", "150"))
_STARTED = time.monotonic()


@pytest.fixture(autouse=True)
def _suite_time_budget():
    """Running past the wall-clock budget is a failure."""
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


def check_context(task_id: str, candidate: dict, jobs=None, seed: int = 0):
    """A Warranted CheckContext for the program-replay checker on a corpus
    task, draw_seed pinned (tests pin entropy; production draws fresh).
    `jobs` defaults to real podman jobs. reschema imports stay lazy:
    RESCHEMA_HOME is set at conftest import time above and reschema's ROOT
    constants read the env at THEIR import."""
    import json
    from pathlib import Path

    from warranted import CheckContext, PodmanJobs

    from reschema.corpus.generate import slot

    meta = slot(task_id)

    class Pinned(CheckContext):
        def draw_seed(self) -> int:
            self.seeds.append(seed)
            return seed

    return Pinned(
        {k: v if type(v) is bytes else v.encode() for k, v in candidate.items()},
        {},
        {
            "binary": Path(meta["binary"]).read_bytes(),
            "task.json": json.dumps({"seed": meta["seed"]}).encode(),
        },
        PodmanJobs() if jobs is None else jobs,
    )


def compile_original(c_source: str, out):
    """A test-only original, compiled like a corpus seed (trusted source, the
    toolchain image's build path): gcc with the program gate's CFLAGS."""
    import shutil
    from pathlib import Path

    from reschema import toolchain
    from reschema.validate.program import CFLAGS

    with tempfile.TemporaryDirectory(prefix="reschema-orig-") as d:
        job = {"c_source": c_source, "out": "prog", "compiler": "gcc", "flags": CFLAGS}
        r = toolchain.run_build({"mode": "compile", "jobs": [job]}, Path(d))
        assert "stage" not in r and r["results"][0]["rc"] == 0, r
        shutil.copy2(Path(d) / "prog", out)


def validate_function(*args, run_model=None, **kw):
    """validate_function as the function-fuzz checker runs it: the model in a
    Warranted job (podman), unless a test stubs `run_model`."""
    from warranted import PodmanJobs

    from reschema.checks.function_fuzz import job_model
    from reschema.validate import function

    if run_model is None:
        run_model = job_model(PodmanJobs().run)
    return function.validate_function(*args, run_model=run_model, **kw)
