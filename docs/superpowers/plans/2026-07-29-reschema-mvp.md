# ReSchema MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ReSchema harness per `docs/superpowers/specs/2026-07-29-reschema-design.md`: an MCP tool server that forces an LLM agent to maintain C world-models of binaries and strictly validates them against recorded execution traces (level A) and differential function tests (level B).

**Architecture:** Python package. `exec/` records + canonicalizes qiling traces; `validate/` compiles agent C models and replays/diffs (level A) or fuzzes differentially (level B, model native via ctypes, original under qiling); `driver/` marshals function calls per agent-declared param specs; `corpus/` builds a synthetic compile matrix; `mcp/` wraps everything as 5 tools.

**Tech Stack:** Python 3.12, uv, qiling+unicorn, capstone, pyelftools, mcp (official SDK), pytest, system gcc/clang. No worktree — fresh repo, work on `main`.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/reschema/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Init with uv**

```bash
uv init --package --name reschema
uv add qiling capstone pyelftools mcp
uv add --dev pytest ruff
```

- [ ] **Step 2: Ensure `pyproject.toml` build config**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "reschema"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["qiling", "capstone", "pyelftools", "mcp"]

[dependency-groups]
dev = ["pytest", "ruff"]

[tool.hatch.build.targets.wheel]
packages = ["src/reschema"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: `.gitignore`**

```
__pycache__/
.ruff_cache/
.pytest_cache/
.reschema/
build/
```

- [ ] **Step 4: Smoke test**

Run: `uv run python -c "import qiling, capstone, elftools, mcp; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: uv scaffold + deps"
```

---

### Task 2: Corpus seeds + generator

**Files:**
- Create: `src/reschema/corpus/__init__.py`, `src/reschema/corpus/seeds/rot13.c`, `src/reschema/corpus/seeds/check.c`, `src/reschema/corpus/seeds/calc.c`, `src/reschema/corpus/generate.py`
- Test: `tests/test_corpus.py`

- [ ] **Step 1: Write seeds** (ABI-pinned, deterministic, minimal syscalls)

```c
// rot13.c — argv-driven, 2 functions
#include <stdio.h>
#include <stdint.h>
__attribute__((sysv_abi)) char rot13_char(char c) {
    if (c >= 'a' && c <= 'z') return (char)('a' + (c - 'a' + 13) % 26);
    if (c >= 'A' && c <= 'Z') return (char)('A' + (c - 'A' + 13) % 26);
    return c;
}
__attribute__((sysv_abi)) void rot13(char *in_out) {
    for (char *p = in_out; *p; p++) *p = rot13_char(*p);
}
int main(int argc, char **argv) {
    if (argc < 2) { puts("usage: rot13 WORD"); return 2; }
    rot13(argv[1]);
    puts(argv[1]);
    return 0;
}
```

```c
// check.c — stdin-driven crackme, 2 functions
#include <stdio.h>
#include <string.h>
#include <stdint.h>
__attribute__((sysv_abi)) uint32_t pw_hash(const char *s) {
    uint32_t h = 5381;
    for (; *s; s++) h = h * 33u + (uint8_t)*s;
    return h;
}
__attribute__((sysv_abi)) int check_pw(const char *s) {
    return pw_hash(s) == 0x1F33E35Fu; /* hash of the real password */
}
int main(void) {
    char buf[64];
    if (!fgets(buf, sizeof buf, stdin)) return 2;
    buf[strcspn(buf, "\n")] = 0;
    if (check_pw(buf)) { puts("OK"); return 0; }
    puts("NOPE");
    return 1;
}
```

```c
// calc.c — multi-function, good level-B source
#include <stdio.h>
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi) {
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi) {
    int32_t s = 0;
    for (int32_t i = lo; i <= hi; i++) s = clamp_i32(s + i, -1000, 1000);
    return s;
}
__attribute__((sysv_abi)) void scale_buf(int32_t *buf, int32_t n, int32_t factor) {
    for (int32_t i = 0; i < n; i++) buf[i] = clamp_i32(buf[i] * factor, -100, 100);
}
int main(void) {
    int32_t data[4] = {1, 2, 3, 4};
    scale_buf(data, 4, 3);
    printf("%d,%d\n", sum_range(-5, 12), data[0]);
    return 0;
}
```

- [ ] **Step 2: Generator `src/reschema/corpus/generate.py`**

```python
"""Build the synthetic corpus matrix; write manifest with function addresses."""
from __future__ import annotations
import json, shutil, subprocess
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "src" / "reschema" / "corpus" / "seeds"
OUT_ROOT = ROOT / ".reschema" / "corpus"

# functions the harness exposes as level-B tasks, per seed
FUNCS = {
    "rot13": ["rot13_char", "rot13"],
    "check": ["pw_hash", "check_pw"],
    "calc": ["clamp_i32", "sum_range", "scale_buf"],
}
COMPILERS = ["gcc", "clang"]
OPTS = ["-O0", "-O1", "-O2"]


def _symtab(binary: Path) -> dict[str, int]:
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        sym = elf.get_section_by_name(".symtab")
        return {s.name: int(s["st_value"]) for s in sym.iter_symbols()
                if s["st_info"]["type"] == "STT_FUNC"} if sym else {}


def build(out_root: Path = OUT_ROOT) -> list[dict]:
    manifest = []
    for seed in sorted(SEEDS.glob("*.c")):
        name = seed.stem
        for cc in COMPILERS:
            if not shutil.which(cc):
                continue
            for opt in OPTS:
                for strip in (False, True):
                    slot = f"{name}/{cc}-{opt.lstrip('-')}-{'stripped' if strip else 'sym'}"
                    out = out_root / slot
                    out.mkdir(parents=True, exist_ok=True)
                    binary = out / "prog"
                    cmd = [cc, opt, "-static", "-fno-pie", "-no-pie", "-g0",
                            str(seed), "-o", str(binary)]
                    subprocess.run(cmd, check=True)
                    syms = _symtab(binary)
                    funcs = {f: syms[f] for f in FUNCS[name] if f in syms}
                    if strip:
                        subprocess.run(["strip", "-s", str(binary)], check=True)
                    manifest.append({
                        "seed": name, "compiler": cc, "opt": opt,
                        "stripped": strip, "binary": str(binary),
                        "task_id": f"{name}::{cc}-{opt}-{'stripped' if strip else 'sym'}",
                        "functions": funcs,
                    })
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    m = build()
    print(f"{len(m)} corpus slots")
```

- [ ] **Step 3: Write test `tests/test_corpus.py`**

```python
import json
from reschema.corpus.generate import build, OUT_ROOT

def test_corpus_builds_with_addresses():
    m = build()
    assert len(m) >= 6 * 3, "expect >= 6 compiler-opt combos x seeds (minus missing compilers)"
    rot = next(x for x in m if x["seed"] == "rot13" and not x["stripped"] and x["opt"] == "-O2")
    assert rot["functions"]["rot13"] != 0
    stripped = next(x for x in m if x["seed"] == "rot13" and x["stripped"] and x["opt"] == "-O2")
    assert stripped["functions"]["rot13"] != 0  # address captured pre-strip


def test_stripped_has_no_symtab():
    from elftools.elf.elffile import ELFFile
    m = json.loads((OUT_ROOT / "manifest.json").read_text())
    s = next(x for x in m if x["stripped"])
    with open(s["binary"], "rb") as f:
        assert ELFFile(f).get_section_by_name(".symtab") is None
```

- [ ] **Step 4: Run tests**

Run: `cd /home/lewdwig/git/reschema && uv run pytest tests/test_corpus.py -v`
Expected: 2 passed. (If `gcc -static` fails, `apt install glibc-static` or use `musl-gcc`—note it in commit message.)

- [ ] **Step 5: Commit**

```bash
git add src/reschema/corpus tests/test_corpus.py && git commit -m "feat: synthetic corpus seeds + compile matrix generator"
```

---

### Task 3: Trace recorder (`exec/recorder.py`)

**Files:**
- Create: `src/reschema/exec/__init__.py`, `src/reschema/exec/recorder.py`
- Test: `tests/test_recorder.py`

- [ ] **Step 1: Write recorder**

```python
"""Run a static ELF under qiling; capture observable trace."""
from __future__ import annotations
import io
from pathlib import Path
from qiling import Qiling
from qiling.const import QL_INTERCEPT

LOG_SYSCALLS = ("read", "write", "writev", "openat", "close", "brk", "mmap", "exit_group")


class Sink(io.RawIOBase):
    def __init__(self):
        self.buf = bytearray()
    def writable(self):
        return True
    def write(self, b):
        self.buf += b
        return len(b)


def record(binary: str | Path, argv: list[str], stdin: bytes = b"", timeout_us: int = 3_000_000) -> dict:
    out, err = Sink(), Sink()
    ql = Qiling([str(binary), *argv], "/", stdout=out, stderr=err,
                stdin=io.BytesIO(stdin), verbose=0)  # verbose param name may be QL_VERBOSE
    events = []
    def _hook(name, phase):
        def h(ql, *args):
            e = {"phase": phase, "sc": name, "args": [hex(a) if isinstance(a, int) else str(a) for a in args]}
            if phase == "exit":
                e["result"] = hex(ql.reg.rax)
            events.append(e)
        return h
    for sc in LOG_SYSCALLS:
        ql.os.set_syscall(sc, _hook(sc, "enter"), QL_INTERCEPT.ENTER)
        ql.os.set_syscall(sc, _hook(sc, "exit"), QL_INTERCEPT.EXIT)
    try:
        ql.run(timeout=timeout_us)
        exit_code = ql.os.exit_code if ql.os.exit_code is not None else 0
    except Exception as e:  # crash = observable too
        exit_code = -1
        events.append({"phase": "fault", "sc": "crash", "args": [repr(e)]})
    import hashlib
    written = {}  # qiling host-fs writes land under rootfs "/"; v1 seeds don't write files
    return {
        "argv": [str(binary), *argv],
        "stdin_hex": stdin.hex(),
        "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
        "stdout": bytes(out.buf).hex(),
        "stderr": bytes(err.buf).hex(),
        "exit_code": exit_code,
        "files_written": written,
        "events": events,
    }
```

- [ ] **Step 2: Write golden test `tests/test_recorder.py`**

```python
import json
from reschema.corpus.generate import build, OUT_ROOT
from reschema.exec.recorder import record

def _bin():
    build()
    m = json.loads((OUT_ROOT / "manifest.json").read_text())
    return next(x for x in m if x["seed"] == "rot13" and x["opt"] == "-O2"
                and x["compiler"] == "gcc" and not x["stripped"])["binary"]

def test_rot13_golden():
    t = record(_bin(), ["hello"], b"")
    assert t["exit_code"] == 0
    assert bytes.fromhex(t["stdout"]).decode() == "uryyb\n"

def test_check_rejects_wrong_password():
    import json as j
    m = j.loads((OUT_ROOT / "manifest.json").read_text())
    b = next(x for x in m if x["seed"] == "check" and x["opt"] == "-O2"
             and x["compiler"] == "gcc" and not x["stripped"])["binary"]
    t = record(b, [], b"hunter2\n")
    assert t["exit_code"] == 1
    assert t["stdout"] == b"NOPE\n".hex()
    scs = [e["sc"] for e in t["events"] if e["phase"] == "enter"]
    assert "write" in scs and "exit_group" in scs
```

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_recorder.py -v`
Expected: pass. (Qiling API quirks to adjust if offended by: `verbose` → `verbose=QL_VERBOSE.OFF`; `ql.os.exit_code` → `ql.os.exit_code or 0`.)

- [ ] **Step 4: Commit**

```bash
git add src/reschema/exec tests/test_recorder.py && git commit -m "feat: qiling trace recorder"
```

---

### Task 4: Trace canonicalizer (`exec/canonical.py`)

**Files:**
- Create: `src/reschema/exec/canonical.py`
- Test: `tests/test_canonical.py`

- [ ] **Step 1: Write canonicalizer**

```python
"""Normalize legitimately-divergent values in recorded traces before diffing."""
from __future__ import annotations
import re

ADDR = re.compile(r"0x[0-9a-f]{6,}")
KEEP_FD = {"0x0", "0x1", "0x2"}  # stdin/out/err stay stable


def _mapper(prefix):
    m: dict[str, str] = {}
    def f(s: str) -> str:
        if s not in m:
            m[s] = f"{prefix}_{len(m)}"
        return m[s]
    return f


def canonicalize(trace: dict) -> dict:
    addr_of = _mapper("ADDR")
    fd_of = _mapper("FD")
    evs = []
    for e in trace["events"]:
        e = dict(e)
        def sub(m):
            tok = m.group(0)
            # heuristic: brk/mmap results & pointer args -> ADDR; small ints that look like fds -> FD
            return addr_of(tok)
        e["args"] = [ADDR.sub(sub, a) if ADDR.fullmatch(a) else a for a in e["args"]]
        if "result" in e:
            r = e["result"]
            e["result"] = addr_of(r) if ADDR.fullmatch(r) else r
        evs.append(e)
    t = dict(trace)
    t["events"] = evs
    return t
```

- [ ] **Step 2: Test `tests/test_canonical.py`**

```python
from reschema.exec.canonical import canonicalize

def test_addresses_ordinal_mapped():
    t = {"events": [
        {"phase": "exit", "sc": "mmap", "args": ["0x0", "0x1000"], "result": "0x7f12ab000000"},
        {"phase": "exit", "sc": "mmap", "args": ["0x0", "0x1000"], "result": "0x7f12ab001000"},
        {"phase": "enter", "sc": "write", "args": ["0x1", "0x7f12ab000000", "0x5"]},
    ], "stdout": "", "stderr": "", "exit_code": 0, "argv": [], "stdin_sha256": "", "files_written": {}}
    c = canonicalize(t)
    assert c["events"][0]["result"] == "ADDR_0"
    assert c["events"][1]["result"] == "ADDR_1"
    assert c["events"][2]["args"] == ["0x1", "ADDR_0", "0x5"]  # fd 1 untouched, same addr same ordinal
```

- [ ] **Step 3: Run**

Run: `uv run pytest tests/test_canonical.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/exec/canonical.py tests/test_canonical.py && git commit -m "feat: trace canonicalizer"`

---

### Task 5: Task store + experiment engine

**Files:**
- Create: `src/reschema/engine.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Engine**

```python
"""Task state: recorded traces, experiments, ledger. On-disk under .reschema/<task_id>."""
from __future__ import annotations
import json, shutil
from pathlib import Path
from .exec.recorder import record
from .exec.canonical import canonicalize

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / ".reschema" / "tasks"
MANIFEST = ROOT / ".reschema" / "corpus" / "manifest.json"


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text())


class TaskStore:
    def __init__(self, task_id: str):
        self.meta = next(t for t in load_manifest() if t["task_id"] == task_id)
        self.dir = TASKS / task_id.replace("::", "__")
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / name

    def record_case(self, label: str, argv: list[str], stdin: bytes) -> dict:
        """Record a ground-truth trace; double-record to catch flakiness."""
        a = canonicalize(record(self.meta["binary"], argv, stdin))
        b = canonicalize(record(self.meta["binary"], argv, stdin))
        if a != b:
            raise RuntimeError(f"task {self.meta['task_id']} flaky on {label}")
        (self._path(f"trace_{label}.json")).write_text(json.dumps(a))
        return a

    def recorded(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.dir.glob("trace_*.json"))]

    def ledger(self) -> dict:
        p = self._path("ledger.json")
        return json.loads(p.read_text()) if p.exists() else {"accepted": [], "submissions": 0, "rejections": 0}

    def save_ledger(self, l: dict):
        self._path("ledger.json").write_text(json.dumps(l, indent=2))
```

- [ ] **Step 2: Test `tests/test_engine.py`**

```python
import json
from unittest.mock import patch
import pytest
from reschema.corpus.generate import build
from reschema.engine import TaskStore

def test_record_case_saves_canonical_trace():
    build()
    st = TaskStore("rot13::gcc-O2-sym")
    t = st.record_case("a", ["abc"], b"")
    assert st.recorded()[0]["stdout"] == t["stdout"]

def test_flakiness_detected(monkeypatch):
    build()
    st = TaskStore("rot13::gcc-O2-sym")
    runs = iter([{"stdout": "aa", "events": []}, {"stdout": "bb", "events": []}])
    monkeypatch.setattr("reschema.engine.record", lambda *a, **k: next(runs))
    with pytest.raises(RuntimeError, match="flaky"):
        st.record_case("x", ["z"], b"")
```

- [ ] **Step 3: Run** `uv run pytest tests/test_engine.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/engine.py tests/test_engine.py && git commit -m "feat: task store + double-record flakiness gate"`

---

### Task 6: Program-mode validation (`validate/program.py`)

**Files:**
- Create: `src/reschema/validate/__init__.py`, `src/reschema/validate/program.py`
- Test: `tests/test_validate_program.py`

- [ ] **Step 1: Validator**

```python
"""Compile agent C model; replay canonical traces; structured accept/reject."""
from __future__ import annotations
import subprocess, tempfile
from dataclasses import dataclass
from pathlib import Path
from ..exec.recorder import record
from ..exec.canonical import canonicalize

CFLAGS = ["gcc", "-O1", "-static", "-fno-pie", "-no-pie", "-g0"]


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    divergence: dict | None = None


def compile_model(c_source: str, out: Path) -> tuple[bool, str]:
    src = out.with_suffix(".c")
    src.write_text(c_source)
    p = subprocess.run([*CFLAGS, str(src), "-o", str(out)],
                       capture_output=True, text=True, timeout=60)
    return p.returncode == 0, p.stderr


def replay_against(model_bin: Path, traces: list[dict]) -> Verdict:
    for tr in traces:
        argv = tr["argv"][1:]  # original binary path n/a to model
        stdin = bytes.fromhex(tr.get("stdin_hex", "")) if "stdin_hex" in tr else b""
        got = canonicalize(record(model_bin, argv, stdin))
        if got["stdout"] != tr["stdout"] or got["stderr"] != tr["stderr"] or got["exit_code"] != tr["exit_code"]:
            return Verdict(False, "io-mismatch", {
                "argv": argv,
                "expected": {"stdout": tr["stdout"], "exit_code": tr["exit_code"]},
                "actual": {"stdout": got["stdout"], "exit_code": got["exit_code"]},
            })
        # Compare only the observable channel: write-family + exit syscalls.
        # Allocator/brk/mmap chatter differs between two correct C impls — noise, not semantics.
        OBS = ("write", "writev", "exit_group")
        ge = [e for e in got["events"] if e["sc"] in OBS]
        te = [e for e in tr["events"] if e["sc"] in OBS]
        for i, (a, e) in enumerate(zip(te, ge)):
            if a != e:
                return Verdict(False, "event-divergence", {
                    "argv": argv, "first_diverging_event_index": i,
                    "expected": e, "actual": a,
                })
        if len(ge) != len(te):
            return Verdict(False, "event-length", {
                "argv": argv, "expected_len": len(te), "actual_len": len(ge)})
    return Verdict(True)
```

- [ ] **Step 2: Test**

```python
from pathlib import Path
from reschema.corpus.generate import build
from reschema.engine import TaskStore
from reschema.validate.program import compile_model, replay_against

GOOD = r'''
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
'''
BAD = r'#include <stdio.h>\nint main(void){puts("uryyb");return 0;}'

def test_good_model_accepted(tmp_path):
    build(); st = TaskStore("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b""); st.record_case("b", ["Hello"], b"")
    ok, err = compile_model(GOOD, tmp_path / "m")
    assert ok, err
    v = replay_against(tmp_path / "m", st.recorded())
    assert v.ok, v.divergence
    # hardcoding outputs fails on any case not matching
    ok, _ = compile_model(BAD, tmp_path / "b")
    assert ok
    v2 = replay_against(tmp_path / "b", st.recorded())
    assert not v2.ok and v2.divergence["argv"] == ["abc"]
```

- [ ] **Step 3: Run** `uv run pytest tests/test_validate_program.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/validate tests/test_validate_program.py && git commit -m "feat: program-mode compile+replay validator (canonical, write-family events)"`

---

### Task 7: Hidden-test replay gate

**Files:**
- Modify: `src/reschema/validate/program.py` (add `gen_hidden_inputs`, `hidden_replay`)
- Modify: `src/reschema/engine.py` (add `submit_program`)
- Test: `tests/test_hidden.py`

- [ ] **Step 1: Input generator (append to program.py) + submission gate (engine.py)**

```python
# append to program.py
import random, string

def gen_hidden_inputs(seed: int, n: int = 8, modes: tuple = ("argv",)) -> list[tuple[list[str], bytes]]:
    """Fresh inputs from task input-space. v1 space: printable strings for argv[1] or stdin."""
    rng = random.Random(seed)
    cs = string.ascii_letters + string.digits + string.punctuation.replace('"', '').replace("\\", "")
    cases = []
    for _ in range(n):
        s = "".join(rng.choice(cs) for _ in range(rng.randint(1, 24)))
        cases.append(([s], b"") if "argv" in modes else ([], (s + "\n").encode()))
    return cases
```

```python
# append to engine.py
from .validate.program import compile_model, replay_against, gen_hidden_inputs
from .exec.recorder import record as _record
from .exec.canonical import canonicalize as _canon

def submit_program(store: TaskStore, c_source: str, hidden_seed: int, modes=("argv",)) -> dict:
    model = store.dir / "model"
    ok, err = compile_model(c_source, model)
    if not ok:
        return {"accepted": False, "reason": "compile", "detail": err}
    v = replay_against(model, store.recorded())
    if not v.ok:
        return {"accepted": False, "reason": v.reason, "divergence": v.divergence}
    for argv, stdin in gen_hidden_inputs(hidden_seed, modes=modes):
        want = _canon(_record(store.meta["binary"], argv, stdin))
        got = _canon(_record(model, argv, stdin))
        if (want["stdout"], want["exit_code"]) != (got["stdout"], got["exit_code"]):
            return {"accepted": False, "reason": "hidden-test", "divergence":
                    {"argv": argv, "expected": want["stdout"], "actual": got["stdout"]}}
    led = store.ledger(); led["accepted"].append("program"); store.save_ledger(led)
    return {"accepted": True, "replay_pct": 100}
```

- [ ] **Step 2: Test `tests/test_hidden.py`**

```python
from reschema.corpus.generate import build
from reschema.engine import TaskStore, submit_program

HARD = r'#include <stdio.h>\nint main(){puts("uryyb");return 0;}'  # passes recorded 'hello' only

def test_hardcoded_model_fails_hidden():
    build(); st = TaskStore("rot13::gcc-O2-sym")
    st.record_case("a", ["hello"], b"")
    r = submit_program(st, HARD, hidden_seed=7)
    assert not r["accepted"] and r["reason"] in ("io-mismatch", "hidden-test")

def test_true_model_passes_hidden(tmp_path=None):
    from tests.test_validate_program import GOOD
    build(); st = TaskStore("rot13::gcc-O2-sym")
    st.record_case("a", ["hello"], b"")
    r = submit_program(st, GOOD, hidden_seed=7)
    assert r["accepted"], r
```

- [ ] **Step 3: Run** `uv run pytest tests/test_hidden.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/engine.py src/reschema/validate/program.py tests/test_hidden.py && git commit -m "feat: hidden-test replay gate (anti-hardcoding)"`

---

### Task 8: Disassembly slice (`disasm/slice.py`)

**Files:**
- Create: `src/reschema/disasm/__init__.py`, `src/reschema/disasm/slice.py`
- Test: `tests/test_disasm.py`

- [ ] **Step 1: Slicer**

```python
"""Disassemble one function from a binary using manifest address + .text bounds."""
from pathlib import Path
import capstone
from elftools.elf.elffile import ELFFile


def disasm_function(binary: str | Path, addr: int, max_bytes: int = 256) -> str:
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        text = elf.get_section_by_name(".text")
        base, data = text["sh_addr"], text.data()
    off = addr - base
    blob = data[off:off + max_bytes]
    # stop after first `ret` following 4 insns (dead-simple function bound)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    lines, seen = [], 0
    for i in md.disasm(blob, addr):
        lines.append(f"0x{i.address:x}:\t{i.mnemonic}\t{i.op_str}")
        seen += 1
        if i.mnemonic == "ret" and seen >= 3:
            break
    return "\n".join(lines)
```

- [ ] **Step 2: Test**

```python
import json
from reschema.corpus.generate import build, OUT_ROOT
from reschema.disasm.slice import disasm_function

def test_rot13_disasm():
    build()
    m = json.loads((OUT_ROOT / "manifest.json").read_text())
    t = next(x for x in m if x["seed"] == "rot13" and x["opt"] == "-O2" and not x["stripped"])
    txt = disasm_function(t["binary"], t["functions"]["rot13"])
    assert "\t" in txt and ("ret" in txt or "jmp" in txt)
```

- [ ] **Step 3: Run** `uv run pytest tests/test_disasm.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/disasm tests/test_disasm.py && git commit -m "feat: function disassembly slicer"`

---

### Task 9: Call driver (`driver/calling.py`)

**Files:**
- Create: `src/reschema/driver/__init__.py`, `src/reschema/driver/spec.py`, `src/reschema/driver/calling.py`
- Test: `tests/test_driver.py`

- [ ] **Step 1: Param spec (`spec.py`)**

```python
"""Agent-declared param specs: how to generate/marshal/compare args."""
from __future__ import annotations
import random, struct
from dataclasses import dataclass

@dataclass
class Param:
    name: str
    kind: str               # "i32" | "buffer_i32" | "cstring"
    direction: str = "in"   # "in" | "out" | "in_out"
    length_param: str | None = None
    range: tuple[int, int] = (-100, 100)

    @classmethod
    def from_json(cls, d: dict) -> "Param":
        r = d.get("range", [-100, 100])
        return cls(d["name"], d["kind"], d.get("direction", "in"),
                   d.get("length_param"), (r[0], r[1]))
```

- [ ] **Step 2: Calling (`calling.py`)**

```python
"""Invoke a function: original in qiling, model natively via ctypes."""
from __future__ import annotations
import ctypes, random, struct
from pathlib import Path
from qiling import Qiling
from .spec import Param

SENTINEL = 0x1000000  # mapped return-address trap — far from 0x400000 static image base
BAREGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]  # SysV int-arg order


def gen_inputs(params: list[Param], rng: random.Random, n: int) -> list[dict]:
    cases = []
    for _ in range(n):
        case = {}
        # pass 1: scalars first so length_param values exist before buffers reference them
        for p in params:
            lo, hi = p.range
            if p.kind == "i32":
                case[p.name] = rng.randint(lo, hi)
        # pass 2: buffers/strings
        for p in params:
            lo, hi = p.range
            if p.kind == "cstring":
                ln = rng.randint(0, 16)
                case[p.name] = bytes(rng.randint(97, 122) for _ in range(ln)) + b"\0"
            elif p.kind == "buffer_i32":
                ln = case.get(p.length_param, rng.randint(1, 8))
                if p.direction != "out":
                    case[p.name] = [rng.randint(lo, hi) for _ in range(ln)]
                else:
                    case[p.name] = ln  # out buffer: length marker, allocated zeroed
        cases.append(case)
    return cases


def _marshal(p: Param, val, write):
    """write() allocs memory, returns pointer."""
    if p.kind == "i32":
        return val, None
    if p.kind == "cstring":
        return write(val), None
    if p.kind == "buffer_i32":
        n = len(val) if isinstance(val, list) else val  # int => out buffer of n elems
        data = struct.pack(f"<{n}i", *val) if isinstance(val, list) else b"\x00" * 4 * n
        return write(data), n


def call_original(binary: str, addr: int, params: list[Param], case: dict) -> dict:
    ql = Qiling([binary], "/", verbose=0)
    ql.mem.map(SENTINEL, 0x1000)
    sp = ql.os.stack_address + 0x800  # reuse qiling's own mapped stack; room downward
    ptrs = {}
    top = sp - 0x400
    def write(data: bytes) -> int:
        nonlocal top
        top -= (len(data) + 15) & ~15
        ql.mem.write(top, data)
        return top
    regvals = []
    for p in params:
        v, _ = _marshal(p, case[p.name], write)
        regvals.append(v)
        if p.kind in ("buffer_i32", "cstring"):
            ptrs[p.name] = v
    for reg, v in zip(BAREGS, regvals):
        setattr(ql.reg, reg, v)
    ql.reg.rsp = sp                # set rsp BEFORE stack_write (it is rsp-relative)
    ql.stack_write(0, SENTINEL)
    ql.reg.rip = addr
    ql.run(begin=addr, end=SENTINEL, timeout=3_000_000)
    out = {"ret": ctypes.c_int32(ql.reg.rax).value, "mem": {}}
    for p in params:
        if p.kind == "buffer_i32" and p.direction in ("out", "in_out", "in") and p.name in ptrs:
            n = len(case[p.name]) if isinstance(case[p.name], list) else case[p.name]
            out["mem"][p.name] = list(struct.unpack(f"<{n}i", bytes(ql.mem.read(ptrs[p.name], 4 * n))))
        if p.kind == "cstring" and p.direction != "in" and p.name in ptrs:
            buf = bytes(ql.mem.read(ptrs[p.name], len(case[p.name])))
            out["mem"][p.name] = buf
    return out


def call_model_native(so_path: str, func: str, params: list[Param], case: dict) -> dict:
    lib = ctypes.CDLL(so_path)
    fn = getattr(lib, func)
    fn.restype = ctypes.c_int32
    keep = []
    args, watched = [], {}
    for p in params:
        if p.kind == "i32":
            args.append(ctypes.c_int32(case[p.name]))
        elif p.kind == "cstring":
            b = ctypes.create_string_buffer(bytes(case[p.name]), len(case[p.name]))
            keep.append(b); args.append(ctypes.cast(b, ctypes.c_char_p))
            watched[p.name] = (b, "cstring")
        elif p.kind == "buffer_i32":
            n = len(case[p.name]) if isinstance(case[p.name], list) else case[p.name]
            arr = (ctypes.c_int32 * n)(*(case[p.name] if isinstance(case[p.name], list) else [0] * n))
            keep.append(arr); args.append(arr)
            watched[p.name] = (arr, "buffer_i32")
    ret = fn(*args)
    out = {"ret": ret, "mem": {}}
    for name, (buf, kind) in watched.items():
        if kind == "buffer_i32":
            out["mem"][name] = list(buf)
        else:
            out["mem"][name] = bytes(buf)
    return out
```

- [ ] **Step 3: Test**

```python
import json, random, subprocess
from pathlib import Path
from reschema.corpus.generate import build, OUT_ROOT
from reschema.driver.spec import Param
from reschema.driver.calling import gen_inputs, call_original, call_model_native

MODEL = r'''
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_i32(s+i,-1000,1000); return s;
}'''

def _slot(seed, func):
    m = json.loads((OUT_ROOT / "manifest.json").read_text())
    t = next(x for x in m if x["seed"] == seed and x["opt"] == "-O2" and not x["stripped"] and x["compiler"] == "gcc")
    return t["binary"], t["functions"][func]

def test_sum_range_matches_native_model(tmp_path):
    build()
    binary, addr = _slot("calc", "sum_range")
    so = tmp_path / "m.so"
    subprocess.run(["gcc", "-O1", "-shared", "-fPIC", "-x", "c", "-", "-o", str(so)],
                   input=MODEL.encode(), check=True)
    params = [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))]
    for case in gen_inputs(params, random.Random(1), 4):
        assert case["lo"] <= case["hi"]
        a = call_original(binary, addr, params, case)
        b = call_model_native(str(so), "sum_range", params, case)
        assert a["ret"] == b["ret"]
```

- [ ] **Step 4: Run** `uv run pytest tests/test_driver.py -v` → pass. (Qiling nits likely: `ql.reg.rdi` setattr works; `ql.run(begin=..., end=...)` OK; `ql.stack_write(0, SENTINEL)` writes at rsp offset 0 — set `ql.reg.rsp = STACK` first or pass rsp-relative offset.)

- [ ] **Step 5: Commit**

`git add src/reschema/driver tests/test_driver.py && git commit -m "feat: SysV call driver (qiling original + ctypes native model)"`

---

### Task 10: Function-mode validation (`validate/function.py`)

**Files:**
- Create: `src/reschema/validate/function.py`
- Test: `tests/test_validate_function.py`

- [ ] **Step 1: Differential validator**

```python
"""Differential validation of a candidate C function vs original machine code."""
from __future__ import annotations
import random, subprocess
from dataclasses import dataclass
from pathlib import Path
from ..driver.spec import Param
from ..driver.calling import gen_inputs, call_original, call_model_native

N_FUZZ = 64


@dataclass
class FnVerdict:
    ok: bool
    divergence: dict | None = None


def validate_function(binary: str, addr: int, func: str, params: list[Param],
                      c_source: str, so_path: Path, seed: int = 1,
                      compare: tuple = ("ret", "mem")) -> FnVerdict:
    """compare: v1 compares return value + spec-declared memory only (no syscalls)."""
    tmp = so_path.with_suffix(".c"); tmp.write_text(c_source)
    r = subprocess.run(["gcc", "-O1", "-shared", "-fPIC", str(tmp), "-o", str(so_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return FnVerdict(False, {"stage": "compile", "stderr": r.stderr})
    for case in gen_inputs(params, random.Random(seed), N_FUZZ):
        want = call_original(binary, addr, params, case)
        got = call_model_native(str(so_path), func, params, case)
        if want != got:
            return FnVerdict(False, {"input": {k: (v if not isinstance(v, (bytes, list)) else str(v)[:80])
                                                for k, v in case.items()},
                                     "expected": str(want)[:400], "actual": str(got)[:400]})
    return FnVerdict(True)
```

- [ ] **Step 2: Test** — reuse `MODEL` good/bad for `sum_range`:

```python
BAD_SUM = r'#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){return 42;}'

def test_bad_function_rejected(tmp_path):
    ...  # call validate_function with BAD_SUM; assert not ok, divergence has "input"

def test_good_function_accepted(tmp_path):
    ...  # with MODEL good source; assert ok
```

(Write both tests fully, mirroring Task 9's fixture.)

- [ ] **Step 3: Run** `uv run pytest tests/test_validate_function.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/validate/function.py tests/test_validate_function.py && git commit -m "feat: differential function validator"`

---

### Task 11: Level-B engine glue + ledger + composition

**Files:**
- Modify: `src/reschema/engine.py` (add `open_function_task`, `submit_function`, `compose`)
- Test: `tests/test_engine_b.py`

- [ ] **Step 1: Engine additions**

```python
# append to engine.py
from .validate.function import validate_function
from .driver.spec import Param

DEFAULT_PARAM_SPECS: dict[str, list[dict]] = {}  # agent supplies; engine passes through

def open_function_task(store: TaskStore, func: str) -> dict:
    from .disasm.slice import disasm_function
    addr = store.meta["functions"][func]
    return {"task_id": store.meta["task_id"], "function": func, "address": hex(addr),
            "disasm": disasm_function(store.meta["binary"], addr)}

def experiment_function(store: TaskStore, func: str, params: list[dict], case: dict) -> dict:
    from .driver.calling import call_original
    ps = [Param.from_json(p) for p in params]
    return call_original(store.meta["binary"], store.meta["functions"][func], ps, case)

def submit_function(store: TaskStore, func: str, params: list[dict], c_source: str) -> dict:
    ps = [Param.from_json(p) for p in params]
    v = validate_function(store.meta["binary"], store.meta["functions"][func],
                          func, ps, c_source, store.dir / f"{func}.so")
    led = store.ledger(); led["submissions"] += 1
    if not v.ok:
        led["rejections"] += 1; store.save_ledger(led)
        return {"accepted": False, "divergence": v.divergence}
    if func not in led["accepted"]:
        led["accepted"].append({func: c_source})
    store.save_ledger(led)
    return {"accepted": True}

def compose(store: TaskStore) -> tuple[bool, str]:
    """Concatenate accepted function sources; compile-check as one program model."""
    led = store.ledger()
    src = "\n".join(next(iter(f.values())) for f in led["accepted"] if isinstance(f, dict))
    if not src:
        return False, "#error nothing accepted"
    ok, err = compile_model(src + "\n#include <stdio.h>\nint main(void){return 0;}\n",
                            store.dir / "composed")
    return ok, err
```

- [ ] **Step 2: Test** — B-end-to-end minus MCP: open `sum_range`, experiment once, submit bad (rejected), submit good (accepted), `compose` compiles.

- [ ] **Step 3: Run** `uv run pytest tests/test_engine_b.py -v` → pass.

- [ ] **Step 4: Commit**

`git add src/reschema/engine.py tests/test_engine_b.py && git commit -m "feat: level-B engine glue, ledger, composition check"`

---

### Task 12: MCP server (`mcp/server.py`)

**Files:**
- Create: `src/reschema/mcp/__init__.py`, `src/reschema/mcp/server.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: Server**

```python
"""ReSchema MCP tool server: strict judge, thin wrapper over engine."""
from __future__ import annotations
from mcp.server.fastmcp import FastMCP
from ..engine import TaskStore, submit_program, open_function_task, experiment_function, submit_function, compose

mcp = FastMCP("reschema")

@mcp.tool()
def corpus_build() -> list[str]:
    """Build the synthetic corpus; return task IDs."""
    from ..corpus.generate import build
    return [t["task_id"] for t in build()]

@mcp.tool()
def task_open(task_id: str, function: str | None = None) -> dict:
    """Open a task: metadata (and, for function mode, disasm slice)."""
    st = TaskStore(task_id)
    if function:
        return open_function_task(st, function)
    m = st.meta
    return {"task_id": task_id, "seed": m["seed"], "compiler": m["compiler"],
            "opt": m["opt"], "stripped": m["stripped"], "functions": m["functions"]}

@mcp.tool()
def experiment(task_id: str, argv: list[str] | None = None, stdin: str = "",
               function: str | None = None, params: list[dict] | None = None,
               case: dict | None = None) -> dict:
    """Run a ground-truth experiment: record binary behavior (or call a function)."""
    st = TaskStore(task_id)
    if function:
        return experiment_function(st, function, params or [], case or {})
    label = f"e{len(st.recorded())}"
    return st.record_case(label, argv or [], stdin.encode())

@mcp.tool()
def submit_model(task_id: str, c_source: str, function: str | None = None,
                 params: list[dict] | None = None, hidden_seed: int = 1) -> dict:
    """Submit a C world-model. Program mode replays all traces + hidden tests; function mode differential-fuzzes."""
    st = TaskStore(task_id)
    if function:
        return submit_function(st, function, params or [], c_source)
    modes = ("argv",) if "rot13" in task_id else ("stdin",)
    return submit_program(st, c_source, hidden_seed=hidden_seed, modes=modes)

@mcp.tool()
def status(task_id: str) -> dict:
    """Replay metrics + accepted functions ledger."""
    st = TaskStore(task_id)
    return {"task_id": task_id, "recorded_cases": len(st.recorded()), "ledger": st.ledger()}

def main():
    mcp.run()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: In-memory MCP test `tests/test_mcp.py`** (real MCP path via SDK memory transport)

```python
import anyio
import json
from mcp.shared.memory import create_connected_server_and_client_session
from reschema.mcp.server import mcp

def call(tool, **kw):
    async def go():
        async with create_connected_server_and_client_session(mcp._mcp_server) as s:
            r = await s.call_tool(tool, kw)
            return json.loads(r.content[0].text)
    return anyio.run(go)

def test_mcp_tools_smoke():
    ids = call("corpus_build")
    assert any("rot13" in i for i in ids)
    meta = call("task_open", task_id="rot13::gcc-O2-sym")
    assert meta["functions"]["rot13"]
    tr = call("experiment", task_id="rot13::gcc-O2-sym", argv=["abc"])
    assert bytes.fromhex(tr["stdout"]).decode() == "nop\n"
    st = call("status", task_id="rot13::gcc-O2-sym")
    assert st["recorded_cases"] >= 1
```

- [ ] **Step 3: Run** `uv run pytest tests/test_mcp.py -v` → pass. (If SDK internals moved (`mcp._mcp_server`), fall back: spawn stdio `python -m reschema.mcp.server` via `StdioServerParameters`.)

- [ ] **Step 4: Commit**

`git add src/reschema/mcp tests/test_mcp.py && git commit -m "feat: MCP server (5 tools)"`

---

### Task 13: Dogfood integration test

**Files:**
- Create: `tests/test_dogfood.py`

- [ ] **Step 1: Scripted agent over the MCP path**

```python
"""Scripted mini-agent: completes a level-B task with a rejection-repair cycle via MCP calls."""
import json
import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from reschema.mcp.server import mcp

PARAMS = [{"name": "lo", "kind": "i32", "range": [-20, 10]},
          {"name": "hi", "kind": "i32", "range": [10, 30]}]
WRONG = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}'
RIGHT = '''#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''

def _call(tool, **kw):
    async def go():
        async with create_connected_server_and_client_session(mcp._mcp_server) as s:
            r = await s.call_tool(tool, kw)
            return json.loads(r.content[0].text)
    return anyio.run(go)

def test_dogfood_rejection_repair_cycle():
    _call("corpus_build")
    tid = "calc::gcc-O2-sym"
    opened = _call("task_open", task_id=tid, function="sum_range")
    assert "disasm" in opened
    probe = _call("experiment", task_id=tid, function="sum_range",
                  params=PARAMS, case={"lo": -5, "hi": 30})
    assert isinstance(probe["ret"], int)
    r1 = _call("submit_model", task_id=tid, function="sum_range", params=PARAMS, c_source=WRONG)
    assert not r1["accepted"]  # rejection with divergence
    r2 = _call("submit_model", task_id=tid, function="sum_range", params=PARAMS, c_source=RIGHT)
    assert r2["accepted"]
    st = _call("status", task_id=tid)
    assert st["ledger"]["rejections"] == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_dogfood.py -v` → pass.

- [ ] **Step 3: Run full suite** `uv run pytest -v` → all pass.

- [ ] **Step 4: Commit**

`git add tests/test_dogfood.py && git commit -m "test: scripted-agent dogfood over MCP (rejection-repair cycle)"`

---

### Task 14: CI + README + AGENTS.md

**Files:**
- Create: `.github/workflows/ci.yml`, `README.md`, `AGENTS.md`

- [ ] **Step 1: CI**

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: sudo apt-get update && sudo apt-get install -y gcc-multilib musl-tools
      - run: uv sync
      - run: uv run pytest -x -q
```

- [ ] **Step 2: README** — elevator pitch, quickstart (`uv sync`, `uv run reschema-corpus` equivalent `uv run python -m reschema.corpus.generate`, how to connect MCP client: `uv run python -m reschema.mcp.server` over stdio), pointer to spec.

- [ ] **Step 3: AGENTS.md** — conventions: TDD, negative tests first-class, canonical-before-diff rule, v1 non-goals pointer to spec §12.

- [ ] **Step 4: Run** `uv run pytest -q && uvx ruff check src tests` → clean.

- [ ] **Step 5: Commit**

`git add .github README.md AGENTS.md && git commit -m "chore: CI, README, AGENTS.md"`

---

## Self-review notes (plan author)

- Spec §3–§5 → Tasks 3–7, 12. Spec §4 function mode → Tasks 9–11. Spec §6 corpus → Task 2. Spec §7 layout → all paths above. Spec §8 guardrails → flaky gate (T5), canonicalizer (T4), compile-reject (T6/T10), sanitizer version note lives in canonical.py docstring. Spec §9 negative tests → T6 BAD, T7 HARD, T10 BAD_SUM, T13 WRONG. Spec §11 MVP criteria → T13 dogfood (criterion 1), T11 compose + T7 hidden (criterion 2), full suite (criterion 3).
- Deferred per spec §12: multi-arch, packing, symbolic checks; also deferred inside v1 code: struct-by-value args, float/vector regs, syscall comparison in level-B, >6 args.
