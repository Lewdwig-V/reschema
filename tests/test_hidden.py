"""The hidden suite: grammar-aligned, seed-driven input draws, and the
program-replay checker's use of them. Real models against hidden draws (the
hardcoded, memorized, overfit and always-bad-magic attacks) are the golden
fixture's program cases (test_golden_decisions.py)."""

import json
import random
from itertools import islice
from pathlib import Path

from warranted import CheckContext, JobResult, VerdictStatus

from reschema import tasks
from reschema.checks.program_replay import ProgramReplay
from reschema.corpus.generate import slot
from reschema.validate import program
from reschema.validate.program import INPUT_MODE, hidden_input_stream


def gen_hidden_inputs(
    task_id: str, n: int = 8, modes: tuple = ("argv",), seed: str | None = None
) -> list[tuple[list[str], bytes]]:
    """Deterministic fresh inputs given a seed; submissions pass fresh entropy."""
    rng = random.Random(seed if seed is not None else f"hidden:{task_id}")
    return list(islice(hidden_input_stream(rng, modes), n))


class EmptyJob:
    """Stands in for the model's replay job when no case is replayed."""

    identity = "empty"

    def run(self, image, argv, files, **_):
        assert json.loads(files["job.json"])["cases"] == []
        return JobResult(0, b"", b"")


def test_hidden_seed_fresh_per_submission_and_starvation_is_loud(
    built_corpus, monkeypatch
):
    # Every check draws its hidden seed from Warranted (recorded, unguessable),
    # and a stream that can't yield inputs is a loud rejection, never a
    # vacuous pass.
    rngs = []
    monkeypatch.setattr(
        program,
        "hidden_input_stream",
        lambda rng, modes, seed=None: rngs.append(rng) or iter([]),
    )
    meta = slot("rot13::gcc-O1-sym")
    private = {
        "binary": Path(meta["binary"]).read_bytes(),
        "task.json": json.dumps({"seed": meta["seed"]}).encode(),
    }
    candidate = {"model.c": b"int main(void){return 0;}", "cases.json": b"[]"}
    for _ in range(2):
        ctx = CheckContext(candidate, {}, private, EmptyJob())
        v = ProgramReplay().check(ctx)
        assert v.status is VerdictStatus.REJECTED
        assert v.feedback["reason"] == "hidden-starvation", v.feedback
        assert len(ctx.seeds) == 1  # drawn, and recorded as check evidence
    assert rngs[0].getstate() != rngs[1].getstate()  # streams differ check to check


def test_hidden_inputs_deterministic():
    a = gen_hidden_inputs("rot13::gcc-O2-sym", seed="s1")
    assert a == gen_hidden_inputs("rot13::gcc-O2-sym", seed="s1")  # pinned by seed
    assert a != gen_hidden_inputs(
        "rot13::gcc-O2-sym", seed="s2"
    )  # seed drives the draw


def test_hidden_inputs_stdin_mode():
    for argv, stdin in gen_hidden_inputs("check::gcc-O2-sym", modes=("stdin",)):
        assert argv == [] and stdin.endswith(b"\n")


def test_filewrite_is_stdin_driven():
    # Hidden draws must carry stdin (the seed ignores argv); otherwise both gate
    # and model see empty input and the hidden suite proves nothing.
    assert INPUT_MODE["filewrite"] == "stdin-bytes"


def test_hidden_inputs_filewrite_byte_mode():
    # filewrite consumes raw bytes (fread): hidden draws must span the byte
    # domain — NUL + non-printable guaranteed per draw, not just line text.
    kw = {"modes": ("stdin-bytes",), "seed": "s1"}
    a = gen_hidden_inputs("filewrite::gcc-O2-sym", **kw)
    assert a == gen_hidden_inputs("filewrite::gcc-O2-sym", **kw)  # pinned by seed
    for argv, stdin in a:
        assert argv == []
        assert b"\0" in stdin
        assert any(b >= 0x80 for b in stdin)


def test_pkfmt_unrepresentable_functions_are_not_function_tasks(built_corpus):
    # codex P1 on #125: pointer-buffer helpers must NOT be function tasks while
    # the param schema can't model them (an all-i32 sketch would pass stubs on
    # unknown tags); the representable i32 surface remains one.
    ids = [t.id for t in tasks.slot_tasks(slot("pkfmt::gcc-O2-sym"))]
    assert ids == ["pkfmt::gcc-O2-sym::fn::pk_version_ok", "pkfmt::gcc-O2-sym::program"]


def test_pkfmt_grammar_draws_structure():
    # The always-bad-magic stub dies at the hidden stage (golden
    # pkfmt-o2-always-bad-magic) only because valid packets appear in bulk:
    # raw bytes almost never meet the 0x5AB1 magic.
    draws = list(
        islice(
            hidden_input_stream(random.Random(1), ("stdin-bytes",), seed="pkfmt"), 40
        )
    )
    valid = sum(1 for _, b in draws if b[:2] == b"\xb1\x5a")
    assert 15 <= valid <= 35  # 60% grammar share draws structure
