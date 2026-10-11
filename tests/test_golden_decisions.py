"""M8 parity fixture (docs/proposals/m8-rebuild-on-warranted.md): the
Warranted checkers reproduce every accept/reject decision the old gates made,
recorded in tests/golden/m8_decisions.json (slice S1; the old-gate halves
went with the old gates in S7). `function-fuzz` (S3) and `program-replay` (S4)
replay every case with no differences.

Cases come from the existing gate tests (each names its `source_test`). Seeds
are pinned so a hidden or fuzz draw cannot move a verdict between runs; a case
whose source test drew fresh entropy is pinned to a derived seed here.

Program cases follow decision D3: the recorded stage replays the case's own
nominated inputs, double-recorded from the corpus binary by the checker.

Marked `golden`: CI runs it as its own step, outside the default suite's
wall-clock budget."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from conftest import check_context, compile_original
from elftools.elf.elffile import ELFFile
from warranted import CheckContext, PodmanJobs, VerdictStatus

from reschema.checks.function_fuzz import FunctionFuzz
from reschema.checks.program_replay import ProgramReplay
from reschema.corpus.generate import slot

pytestmark = pytest.mark.golden

FIXTURE = json.loads(
    (Path(__file__).parent / "golden" / "m8_decisions.json").read_text()
)


def _id(case):
    return case["id"]


def _expect(case, got):
    # `stage_pinned`/`reason_pinned` record provenance (a value observed by
    # running the gate rather than asserted by the source test); both kinds
    # are compared.
    want = {
        k: v
        for k, v in case["expect"].items()
        if k not in ("stage_pinned", "reason_pinned")
    }
    assert {k: got.get(k) for k in want} == want


def test_fixture_ids_are_unique():
    ids = [c["id"] for c in FIXTURE["function"] + FIXTURE["program"]]
    assert len(ids) == len(set(ids)), ids


def _synthetic_original(spec: dict, d: Path) -> tuple[str, int]:
    """A test-only original (the scout cases): compiled in the toolchain image
    like a model, addressed by its symbol."""
    binary = d / "orig"
    compile_original(spec["compile_from_c_source"], binary)
    with binary.open("rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab")
        addr = sym.get_symbol_by_name(spec["addr_symbol"])[0]["st_value"]
    return str(binary), addr


def _original(case: dict, d: Path) -> tuple[str, int, int | None]:
    """The case's original: binary path, function address, scout window."""
    if case.get("binary_override"):
        binary, addr = _synthetic_original(case["binary_override"], d)
        size = None
    else:
        meta = slot(case["task_id"])
        fmeta = meta["functions"][case["func"]]
        binary, addr = meta["binary"], fmeta["addr"]
        # the old submit_function passed the manifest size; direct
        # validate_function calls in the source tests left the default
        size = None if case["call_path"] == "validate_function" else fmeta["size"]
    if case.get("addr_override") is not None:
        addr = case["addr_override"]
    return binary, addr, size


class _PinnedSeed(CheckContext):
    def __init__(self, seed, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seed = seed

    def draw_seed(self):
        self.seeds.append(self._seed)
        return self._seed


@pytest.mark.parametrize("case", FIXTURE["function"], ids=_id)
def test_function_fuzz_checker_decision(request, case):
    """M8 S3: the function-fuzz checker over run_job decides every golden
    function case as the old gate did (spec decode rejects included)."""
    request.getfixturevalue("built_corpus")
    with tempfile.TemporaryDirectory(prefix="reschema-golden-") as d:
        binary, addr, size = _original(case, Path(d))
        fn = {"name": case["func"], "addr": addr, "size": size}
        ctx = _PinnedSeed(
            case["seed"],
            {
                "model.c": case["c_source"].encode(),
                "params.json": json.dumps(case["params"]).encode(),
            },
            {},
            {
                "binary": Path(binary).read_bytes(),
                "task.json": json.dumps({"seed": "golden", "function": fn}).encode(),
            },
            jobs=PodmanJobs(),
        )
        v = FunctionFuzz(n_fuzz=case["n_fuzz"]).check(ctx)
    div = v.feedback.get("divergence") or {}
    _expect(
        case,
        {
            "ok": v.status is VerdictStatus.PASSED,
            "stage": v.feedback.get("reason") or div.get("stage"),
            "field": div.get("field"),
        },
    )


@pytest.mark.parametrize("case", FIXTURE["program"], ids=_id)
def test_program_replay_decision(built_corpus, case):
    """S4: the same decisions through the Warranted `program-replay` checker
    (model compiled and replayed inside a job). The fixture's cases are
    already D3's nominated cases, so no decision needs a D3 exception; hidden
    draws come from a pinned 64-bit seed, not the old gate's string seed."""
    seed = int.from_bytes(hashlib.sha256(case["id"].encode()).digest()[:8])
    ctx = check_context(
        case["task_id"],
        {"model.c": case["c_source"], "cases.json": json.dumps(case["cases"])},
        seed=seed,
    )
    v = ProgramReplay().check(ctx)
    assert (v.status is VerdictStatus.PASSED) == v.feedback["accepted"]
    got = {
        "accepted": v.feedback["accepted"],
        "reason": v.feedback.get("reason"),
        "stage": v.feedback.get("stage"),
    }
    if case["scope"] == "recorded_only" and got["stage"] == "hidden":
        got = {"accepted": True, "reason": None, "stage": None}  # pins recorded only
    _expect(case, got)
