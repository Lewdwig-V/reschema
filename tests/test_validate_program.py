import pytest

from reschema.corpus.generate import build
from reschema.engine import TaskStore
from reschema.exec.canonical import canonicalize
from reschema.validate.program import compile_model, replay_against

GOOD = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
BAD = '#include <stdio.h>\nint main(void){puts("uryyb");return 0;}'

# Awkward-but-equivalent: precomputes into a local buffer instead of mutating argv,
# still emits stdout with one stdio buffered write.
AWKWARD_BUFFERED = r"""
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv){ if(argc<2){ puts("usage: rot13 WORD"); return 2; }
char buf[256]; int i; for(i=0; argv[1][i] && i<255; i++){
 char c=argv[1][i];
 if(c>='a'&&c<='z') c='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z') c='A'+(c-'A'+13)%26;
 buf[i]=c; }
buf[i]=0; puts(buf); return 0; }
"""

# Same stdout bytes, but emitted as one raw write syscall PER BYTE plus newline.
UNBUFFERED = r"""
#include <unistd.h>
#include <string.h>
int main(int argc, char **argv){ if(argc<2){ write(2,"usage\n",6); return 2; }
char buf[256]; int i; for(i=0; argv[1][i] && i<255; i++){
 char c=argv[1][i];
 if(c>='a'&&c<='z') c='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z') c='A'+(c-'A'+13)%26;
 buf[i]=c; }
for(i=0; buf[i]; i++) write(1, buf+i, 1);
write(1, "\n", 1); return 0; }
"""


@pytest.fixture(scope="module")
def manifest():
    return build()


@pytest.fixture(scope="module")
def store(manifest):
    st = TaskStore("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    st.record_case("b", ["Hello"], b"")
    return st


def test_good_model_accepted(store, tmp_path):
    ok, err = compile_model(GOOD, tmp_path / "m")
    assert ok, err

    v = replay_against(tmp_path / "m", store.recorded())
    assert v.ok, v.divergence


def test_hardcoded_model_rejected_with_divergence(store, tmp_path):
    ok, _ = compile_model(BAD, tmp_path / "b")
    assert ok

    v = replay_against(tmp_path / "b", store.recorded())
    assert not v.ok
    assert v.reason == "io-mismatch"
    assert v.divergence["argv"] == ["abc"]  # first recorded case diverges


def test_compile_error_rejected(tmp_path):
    ok, err = compile_model("int main( {", tmp_path / "bad")
    assert not ok and err


def test_awkward_but_buffered_model_accepted(store, tmp_path):
    # Pins: structural style differences are invisible to validation when the
    # observable channel (stdout bytes + write-family event shape) matches.
    ok, err = compile_model(AWKWARD_BUFFERED, tmp_path / "awk")
    assert ok, err

    v = replay_against(tmp_path / "awk", store.recorded())
    assert v.ok, v.divergence


def test_unbuffered_writes_rejected(store, tmp_path):
    # Pins: write CHUNK SHAPE is observable. The write `count` arg and the number
    # of write syscalls are compared, so a model that dribbles bytes one syscall
    # at a time is rejected even with byte-identical stdout — models must buffer
    # stdout the way stdio does. Observed: seed emits "nop\n" as one write(,,4),
    # model as four write(,,1) — count differs, so it's event-divergence at the
    # very first event, not event-length.
    ok, err = compile_model(UNBUFFERED, tmp_path / "unb")
    assert ok, err

    v = replay_against(tmp_path / "unb", store.recorded())
    assert not v.ok
    assert v.reason == "event-divergence"
    assert v.divergence["argv"] == ["abc"]
    assert v.divergence["first_diverging_event_index"] == 0
    assert v.divergence["expected"]["args"][-1] == "0x4"  # seed's single 4-byte write
    assert v.divergence["actual"]["args"][-1] == "0x1"  # model's 1-byte dribble


def _trace(stdout_hex, exit_code, events):
    # Minimal trace dict in recorder schema, for replay_against without qiling.
    return {
        "argv": ["orig"],
        "stdin_hex": "",
        "stdout": stdout_hex,
        "stderr": "",
        "exit_code": exit_code,
        "files_written": {},
        "events": events,
    }


def test_files_written_compared_byte_exact(tmp_path, monkeypatch):
    # Expected trace carries a file; candidate must produce identical path+bytes.
    stored = canonicalize(
        {**_trace("666f6f0a", 0, []), "files_written": {"out.bin": "c0ffee"}}
    )
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: {
            **_trace("666f6f0a", 0, []),
            "files_written": {"out.bin": "c0ffee"},
        },
    )
    v = replay_against(tmp_path / "m", [stored])
    assert v.ok, v.divergence


def test_files_mismatch_reasons(tmp_path, monkeypatch):
    # Hardcoder reproduces stdout/exit but skips the file channel: reject
    # files-mismatch, not io-mismatch (io channels are clean).
    stored = canonicalize(
        {**_trace("666f6f0a", 0, []), "files_written": {"out.bin": "c0ffee"}}
    )
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: _trace("666f6f0a", 0, []),  # no file written
    )
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "files-mismatch"
    assert v.divergence["argv"] == []
    assert v.divergence["expected"] == {"out.bin": "c0ffee"}
    assert v.divergence["actual"] == {}

    # Same path, wrong bytes is also files-mismatch.
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: {
            **_trace("666f6f0a", 0, []),
            "files_written": {"out.bin": "deadbeef"},
        },
    )
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "files-mismatch"


def test_compile_infra_errors_rejected(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import Mock

    for exc in (subprocess.TimeoutExpired(cmd="gcc", timeout=60), OSError("no gcc")):
        monkeypatch.setattr(
            "reschema.validate.program.subprocess.run", Mock(side_effect=exc)
        )
        ok, err = compile_model(GOOD, tmp_path / "infra")
        assert not ok and err.startswith("compile infra:")


def test_io_mismatch_decoded_previews_and_actual_fault(tmp_path, monkeypatch):
    fault = {"phase": "fault", "sc": "crash", "args": ["QlErrorCoreUnmapped"]}
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: _trace("666f6f", -1, [fault]),
    )

    v = replay_against(tmp_path / "m", [_trace("ff0a", 0, [])])
    assert not v.ok and v.reason == "io-mismatch"
    assert v.divergence["expected"]["stdout_decoded"] == "ÿ\n"  # latin-1 previews
    assert v.divergence["actual"]["stdout_decoded"] == "foo"
    assert v.divergence["actual_fault"]["sc"] == "crash"  # where the model died


def test_event_length_reason_pinned(tmp_path, monkeypatch):
    # Pins: event-length = same observable prefix, different OBS event COUNT. Only
    # reachable via fault (-1) traces: normally-exiting models end in exit_group,
    # so any count difference misaligns the final event and lands on divergence first.
    w_e = {"phase": "enter", "sc": "write", "args": ["0x1", "0x400000", "0x4"]}
    w_x = {**w_e, "phase": "exit", "result": "0x4"}
    w0_e = {"phase": "enter", "sc": "write", "args": ["0x1", "0x400000", "0x0"]}
    w0_x = {**w0_e, "phase": "exit", "result": "0x0"}
    fault = {"phase": "fault", "sc": "timeout", "args": []}
    stored = canonicalize(_trace("6e6f700a", -1, [w_e, w_x, fault]))
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: _trace("6e6f700a", -1, [w_e, w_x, w0_e, w0_x, fault]),
    )

    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "event-length"
    assert v.divergence["expected_len"] == 2
    assert v.divergence["actual_len"] == 4
