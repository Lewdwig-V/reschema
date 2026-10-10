"""M8 parity fixture (docs/proposals/m8-rebuild-on-warranted.md, slice S1):
today's gates reproduce the accept/reject decisions recorded in
tests/golden/m8_decisions.json. The Warranted-backed checkers must later pass
the same file, so a decision that changes is caught on either side.

Cases come from the existing gate tests (each names its `source_test`). Seeds
are pinned so a hidden or fuzz draw cannot move a verdict between runs; a case
whose source test drew fresh entropy is pinned to a derived seed here.

Program cases follow decision D3: the recorded stage replays the case's own
nominated inputs, double-recorded from the corpus binary for this run, never a
task directory's stored traces.

Marked `golden`: CI runs it as its own step, outside the default suite's
wall-clock budget."""

import json
import tempfile
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from reschema.driver.spec import Param
from reschema.engine import TaskStore, _record_stable, program_gate
from reschema.validate.function import validate_function
from reschema.validate.program import compile_model, replay_against

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
    ok, err = compile_model(spec["compile_from_c_source"], binary)
    assert ok, err
    with binary.open("rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab")
        addr = sym.get_symbol_by_name(spec["addr_symbol"])[0]["st_value"]
    return str(binary), addr


@pytest.mark.parametrize("case", FIXTURE["function"], ids=_id)
def test_function_gate_decision(request, case):
    if case["gate_level"] == "submit_function":
        # rejected while decoding the spec, before validate_function runs
        with pytest.raises((KeyError, ValueError)):
            [Param.from_json(p) for p in case["params"]]
        _expect(case, {"ok": False, "stage": "spec"})
        return
    request.getfixturevalue("built_corpus")
    with tempfile.TemporaryDirectory(prefix="reschema-golden-") as d:
        if case.get("binary_override"):
            binary, addr = _synthetic_original(case["binary_override"], Path(d))
            size = None
        else:
            meta = TaskStore(case["task_id"]).meta
            fmeta = meta["functions"][case["func"]]
            binary, addr = meta["binary"], fmeta["addr"]
            # submit_function and the re-grade pass the manifest size; direct
            # validate_function calls in the source tests leave the default
            size = None if case["call_path"] == "validate_function" else fmeta["size"]
        if case.get("addr_override") is not None:
            addr = case["addr_override"]
        v = validate_function(
            binary,
            addr,
            case["func"],
            [Param.from_json(p) for p in case["params"]],
            case["c_source"],
            Path(d) / f"{case['func']}.so",
            seed=case["seed"],
            n_fuzz=case["n_fuzz"],
            size=size,
        )
    div = v.divergence or {}
    _expect(case, {"ok": v.ok, "stage": div.get("stage"), "field": div.get("field")})


@pytest.mark.parametrize("case", FIXTURE["program"], ids=_id)
def test_program_gate_decision(built_corpus, case):
    store = TaskStore(case["task_id"])
    rec = []
    for c in case["cases"]:
        t = _record_stable(
            store.meta["binary"], c["argv"], bytes.fromhex(c["stdin_hex"])
        )
        assert t is not None, f"flaky ground truth for {c}"
        rec.append(t)
    with tempfile.TemporaryDirectory(prefix="reschema-golden-") as d:
        model = Path(d) / "model"
        if case["scope"] == "recorded_only":
            # the source test pins only compile + recorded replay
            ok, err = compile_model(case["c_source"], model)
            assert ok, err
            v = replay_against(model, rec)
            got = {
                "accepted": v.ok,
                "reason": v.reason or None,
                "stage": None if v.ok else "recorded",
            }
        else:
            seed = case["hidden_seed"] or f"golden:{case['id']}"
            fail, _ = program_gate(
                store, case["c_source"], model, hidden_seed=seed, rec=rec
            )
            got = {
                "accepted": fail is None,
                "reason": None if fail is None else fail["reason"],
                "stage": None if fail is None else fail.get("stage"),
            }
    _expect(case, got)
