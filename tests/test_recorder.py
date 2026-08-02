import json
import subprocess
from pathlib import Path

import pytest

from reschema.corpus.generate import OUT_ROOT, build
from reschema.exec.recorder import record


@pytest.fixture(scope="module")
def manifest():
    return build()


def _slot(manifest, seed, opt="-O2", compiler="gcc", stripped=False):
    return next(
        x["binary"]
        for x in manifest
        if x["seed"] == seed
        and x["opt"] == opt
        and x["compiler"] == compiler
        and x["stripped"] == stripped
    )


def test_rot13_golden(manifest):
    t = record(_slot(manifest, "rot13"), ["hello"], b"")
    assert t["exit_code"] == 0
    assert bytes.fromhex(t["stdout"]).decode() == "uryyb\n"


def test_check_rejects_wrong_password(manifest):
    t = record(_slot(manifest, "check"), [], b"hunter2\n")
    assert t["exit_code"] == 1
    assert t["stdout"] == b"NOPE\n".hex()
    scs = [e["sc"] for e in t["events"] if e["phase"] == "enter"]
    assert "write" in scs and "exit_group" in scs


def test_check_accepts_correct_password_stdin_roundtrip_and_determinism(manifest):
    # pre-image `txy"od` for djb2-5381 -> 0x1F33E35F, verified natively against
    # .reschema/corpus/check/gcc-O2-sym/prog: `printf 'txy"od\n' | ./prog` -> "OK", exit 0.
    pw = b'txy"od\n'
    t = record(_slot(manifest, "check"), [], pw)
    assert t["exit_code"] == 0
    assert t["stdout"] == b"OK\n".hex()
    assert bytes.fromhex(t["stdin_hex"]) == pw
    t2 = record(_slot(manifest, "check"), [], pw)
    for key in ("stdout", "stderr", "exit_code", "events"):
        assert t2[key] == t[key]


def test_manifest_has_expected_slot(manifest):
    assert len(manifest) == 48
    assert OUT_ROOT.joinpath("manifest.json").exists()


def test_manifest_filewrite_slot(manifest):
    fw = next(
        x for x in manifest if x["seed"] == "filewrite" and x["compiler"] == "gcc"
    )
    assert "xform_byte" in fw["functions"]


def _xform(data: bytes) -> bytes:
    return bytes(b ^ ((i * 31 + 7) & 0xFF) for i, b in enumerate(data))


def test_filewrite_files_written_captured_and_sandboxed(
    manifest, monkeypatch, tmp_path
):
    # files_written carries the guest's file bytes; the write must be intercepted
    # in memory — agent-controlled C must never touch the host fs.
    monkeypatch.chdir(tmp_path)
    stdin = b"ab\n"
    t = record(_slot(manifest, "filewrite"), [], stdin)
    assert t["exit_code"] == 0
    assert t["files_written"] == {"out.bin": _xform(stdin).hex()}
    out = bytes.fromhex(t["stdout"])
    h = 5381
    for b in _xform(stdin):
        h = (h * 33 + b) & 0xFFFFFFFF
    assert out == f"3 bytes -> out.bin djb2={h:08x}\n".encode()
    assert list(tmp_path.iterdir()) == []  # host fs untouched


def test_existing_seed_files_written_empty(manifest):
    t = record(_slot(manifest, "rot13"), ["hello"], b"")
    assert t["files_written"] == {}  # 36 existing slots: no write-open, no entry


def test_guest_filesystem_mutation_contained(tmp_path):
    # The open family is intercepted in memory; every OTHER host-mutating file
    # op qiling emulates (unlink, mkdir, rename, chmod, ...) must be contained
    # by the record rootfs, never reaching the real fs.
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    src = f"""#include <stdio.h>
#include <sys/stat.h>
int main(void){{ remove("{victim}"); mkdir("/reschema_probe_dir", 0777); return 0; }}
"""
    prog = tmp_path / "probe"
    subprocess.run(
        ["gcc", "-static", "-x", "c", "-", "-o", str(prog)],
        input=src.encode(),
        check=True,
    )
    try:
        t = record(prog, [])
        assert t["exit_code"] == 0
        assert victim.exists()  # unlink contained
        assert not Path("/reschema_probe_dir").exists()  # mkdir contained
    finally:
        subprocess.run(
            ["rm", "-rf", "/reschema_probe_dir"], check=False
        )  # leak cleanup


def test_record_nonexistent_binary_reports_crash():
    t = record("/does/not/exist", [])
    assert t["exit_code"] == -1
    assert any(e["sc"] == "crash" for e in t["events"])


def test_record_timeout_reports_fault(tmp_path):
    prog = tmp_path / "spin"
    subprocess.run(
        ["gcc", "-static", "-x", "c", "-", "-o", str(prog)],
        input=b"int main(void){for(;;){}}\n",
        check=True,
    )
    t = record(prog, [], timeout_us=300_000)
    assert t["exit_code"] == -1
    assert any(e["sc"] == "timeout" for e in t["events"])


def test_record_result_is_json_serializable(manifest):
    t = record(_slot(manifest, "rot13"), ["hello"], b"")
    json.dumps(t)
