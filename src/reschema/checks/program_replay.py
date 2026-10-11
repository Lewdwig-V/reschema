"""M8 S4: the `program-replay` Warranted checker (decisions D1 and D3 in
docs/proposals/m8-rebuild-on-warranted.md); the Warranted-backed successor of
the old engine.program_gate.

Candidate (see `candidate_files`): `model.c` and `cases.json` (the nominated recorded cases: at most
MAX_CASES of {"argv": [str], "stdin_hex": str}, argv excluding argv[0]).
Private task files: `binary` (the corpus ELF) and `task.json` ({"seed": name}).

D3: ground truth for every nominated case is double-recorded here, on the
host, from the corpus binary itself; the hidden suite is drawn from
`draw_seed()`. D1: the model is compiled and replayed under qiling inside ONE
contained job that receives only the model source, the recorder, the
canonicaliser and the case INPUTS. Ground truth never enters the job, so the
job's output is the model's claim about itself and is judged on the host by
the same `compare` as the old gate: a forged trace has to reproduce outputs
the job never saw.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from warranted import FACTS_LIMIT, FEEDBACK_LIMIT, JobLimits, Verdict, VerdictStatus

from .. import toolchain
from ..validate.program import CFLAGS, HIDDEN_N, compare, hidden_suite, record_stable
from . import candidate_files

MAX_CASES = 16  # nominated cases per submission (D3, like the mystery fixture)
MAX_ARGS, MAX_ARG, MAX_STDIN = 16, 256, 4096
_EXEC = Path(__file__).resolve().parents[1] / "exec"
# Measured 2026-10-10: a 24-case job (compile + qiling replays, tmpfs pages
# included) peaks at ~77 MB and dies under 32 MB; 256 MB is ~3x headroom and
# bounds what a model's allocations can take from the host. ~0.5s per case.
LIMITS = JobLimits(memory_mb=256, pids=64, scratch_mb=64)

# Runs inside the job beside the files below. One JSON line per case, or one
# {"compile": stderr} line. The recorder's own per-record timeout (3s) bounds
# each case; the job timeout bounds the whole.
REPLAY = """
import json, subprocess
from canonical import canonicalize
from recorder import record
job = json.load(open("job.json"))
cc = subprocess.run(["gcc", *job["flags"], "/work/model.c", "-o", "/work/model"],
                    capture_output=True, text=True)
if cc.returncode:
    print(json.dumps({"compile": cc.stderr}))
else:
    for c in job["cases"]:
        t = record("/work/model", c["argv"], bytes.fromhex(c["stdin_hex"]))
        print(json.dumps(canonicalize(t)), flush=True)
"""


def _reject(reason: str, stage: str, **kw) -> Verdict:
    return Verdict(
        VerdictStatus.REJECTED,
        _capped({"accepted": False, "reason": reason, "stage": stage, **kw}),
    )


def _parse_cases(raw: bytes) -> list[dict] | str:
    """The nominated cases, deduplicated in order, or why they are malformed."""
    try:
        cases = json.loads(raw)
    except (ValueError, RecursionError):
        return "cases.json is not JSON"
    if type(cases) is not list or len(cases) > MAX_CASES:
        return f"cases.json must be a list of at most {MAX_CASES} cases"
    out = {}
    for c in cases:
        if (
            type(c) is not dict
            or set(c) != {"argv", "stdin_hex"}
            or type(c["argv"]) is not list
            or len(c["argv"]) > MAX_ARGS
            or not all(
                type(a) is str and len(a) <= MAX_ARG and "\0" not in a
                for a in c["argv"]
            )
            or type(c["stdin_hex"]) is not str
            or len(c["stdin_hex"]) > 2 * MAX_STDIN
        ):
            return (
                'each case is {"argv": [str], "stdin_hex": str} with at most '
                f"{MAX_ARGS} args of {MAX_ARG} chars and {MAX_STDIN} stdin bytes"
            )
        try:
            bytes.fromhex(c["stdin_hex"])
        except ValueError:
            return f"stdin_hex is not hex: {c['stdin_hex'][:64]!r}"
        out[(tuple(c["argv"]), c["stdin_hex"])] = c
    return list(out.values())


def _hex(s) -> bool:
    if type(s) is not str:
        return False
    try:
        bytes.fromhex(s)
    except ValueError:
        return False
    return True


def _trace_ok(t) -> bool:
    """Shape `compare` relies on; the job's output is untrusted."""
    return (
        type(t) is dict
        and _hex(t.get("stdout"))
        and _hex(t.get("stderr"))
        and type(t.get("exit_code")) is int
        and type(t.get("files_written")) is dict
        and all(type(v) is str for v in t["files_written"].values())
        and type(t.get("events")) is list
        and all(
            type(e) is dict
            and type(e.get("sc")) is str
            and type(e.get("phase")) is str
            and type(e.get("args")) is list
            and all(type(a) is str for a in e["args"])
            for e in t["events"]
        )
    )


def _cap(v, n: int):
    if type(v) is str:
        return v if len(v) <= n else f"{v[:n]}...[+{len(v) - n} chars]"
    if type(v) is list:
        return [_cap(x, n) for x in v[:16]] + ([f"...[+{len(v) - 16}]"] * (len(v) > 16))
    if type(v) is dict:
        items = list(v.items())
        out = {k[:n]: _cap(x, n) for k, x in items[:16]}
        if len(items) > 16:
            out["..."] = f"+{len(items) - 16} entries"
        return out
    return v


def _capped(feedback: dict) -> dict:
    """Divergence payloads echo model output and ground truth of any size:
    shrink every string and collection until the whole fits FEEDBACK_LIMIT
    (oversize feedback is a checker fault in Warranted)."""
    for n in (4096, 1024, 256, 64):
        out = _cap(feedback, n)
        if len(json.dumps(out, sort_keys=True, indent=2)) < FEEDBACK_LIMIT - 1024:
            return out
    raise AssertionError("feedback does not fit FEEDBACK_LIMIT at the floor cap")


class ProgramReplay:
    version = "1"
    isolated = True  # host recording in private temp dirs; no shared state

    def check(self, ctx) -> Verdict:
        files = candidate_files(ctx.candidate)
        if type(files) is str:
            return _reject("candidate", "candidate", detail=files)
        try:
            source = files["model.c"].decode()
        except KeyError:
            return _reject("candidate", "candidate", detail="model.c is missing")
        except UnicodeDecodeError:
            return _reject("candidate", "candidate", detail="model.c is not UTF-8")
        if "params.json" in files:
            # The guard fingerprints every declaration file; an unused one could
            # be varied to dodge it while the judged source repeats.
            return _reject(
                "candidate", "candidate", detail="params.json belongs to function tasks"
            )
        cases = _parse_cases(files.get("cases.json", b"[]"))
        if type(cases) is str:
            return _reject("cases", "candidate", detail=cases)
        seed_name = json.loads(ctx.private["task.json"])["seed"]
        with tempfile.TemporaryDirectory(prefix="reschema-check-") as d:
            binary = Path(d) / "binary"
            binary.write_bytes(ctx.private["binary"])
            rec = []
            for c in cases:
                t = record_stable(binary, c["argv"], bytes.fromhex(c["stdin_hex"]))
                if t is None:  # the input, not the model, is at fault
                    return _reject("flaky-case", "recorded", argv=c["argv"])
                rec.append(t)
            hidden = hidden_suite(binary, seed_name, rec, str(ctx.draw_seed()))
        # inputs only: ground truth stays on the host
        replayed = [
            {"argv": t["argv"][1:], "stdin_hex": t["stdin_hex"]} for t in rec + hidden
        ]
        r = ctx.run_job(
            toolchain.image_id(),
            ["python", "replay.py"],
            {
                "model.c": source.encode(),
                "replay.py": REPLAY.encode(),
                "recorder.py": (_EXEC / "recorder.py").read_bytes(),
                "canonical.py": (_EXEC / "canonical.py").read_bytes(),
                "job.json": json.dumps({"flags": CFLAGS, "cases": replayed}).encode(),
            },
            timeout_seconds=60 + 5 * len(replayed),
            limits=LIMITS,
        )
        if r.timed_out:
            return _reject("replay-timeout", "replay")
        if r.truncated:
            # ponytail: a whole-job output cap; per-case budgets if honest
            # models ever get near 256 KiB of trace
            return _reject("replay-output", "replay", detail="job output over cap")
        try:
            lines = [json.loads(x) for x in r.stdout.decode().splitlines()]
        except (ValueError, RecursionError):
            lines = None
        first = lines[0] if lines else None
        if r.returncode == 0 and type(first) is dict and set(first) == {"compile"}:
            return _reject("compile", "compile", detail=str(lines[0]["compile"]))
        if (
            r.returncode != 0
            or lines is None
            or len(lines) != len(replayed)
            or not all(_trace_ok(t) for t in lines)
        ):
            return _reject(
                "replay-fault",
                "replay",
                detail=f"rc={r.returncode}: {r.stderr.decode(errors='replace')}",
            )
        v = compare(rec, lines[: len(rec)])
        if not v.ok:
            return _reject(v.reason, "recorded", divergence=v.divergence)
        if len(hidden) < HIDDEN_N:
            # Loud failure over vacuous pass: too few distinct usable hidden inputs.
            return _reject(
                "hidden-starvation",
                "hidden",
                detail=f"{len(hidden)}/{HIDDEN_N} usable hidden inputs",
            )
        v = compare(hidden, lines[len(rec) :])
        if not v.ok:
            return _reject(v.reason, "hidden", divergence=v.divergence)
        fact = {"fn": "__main__", "c_source": source}
        if len(json.dumps([fact], sort_keys=True, indent=2)) > FACTS_LIMIT:
            fact = {
                "fn": "__main__",
                "c_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        return Verdict(
            VerdictStatus.PASSED,
            {"accepted": True, "recorded_cases": len(rec), "hidden_cases": HIDDEN_N},
            facts=(fact,),
        )
