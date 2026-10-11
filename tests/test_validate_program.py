"""`compare`: the program gate's host-side judgment of a model's canonical
traces against ground truth. The model's own compile and replay run in a job
(test_program_replay.py; real models in the golden fixture's vp-* cases).
Here the model's raw trace is a stub, so every divergence shape is pinned
without qiling."""

from reschema.exec.canonical import canonicalize
from reschema.validate import program
from reschema.validate.program import compare


def replay_against(_model, traces):
    """The model's traces as the job yields them (canonicalized recordings,
    `program.record` stubbed per test), compared like the checker does."""
    return compare(
        traces,
        [canonicalize(program.record(None, t["argv"][1:], b"")) for t in traces],
    )


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


def _filewrite_style_events(write_count_arg="0x4", exit_code=0):
    # canonicalized filewrite-shaped trace: open out.bin(fd 3), write 4 bytes to it,
    # write 1 byte to stdout, exit. Canonicalized: fd from write-open -> FD_0.
    o_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x241", "0x1b6"],
    }
    o_x = {
        "phase": "exit",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x241", "0x1b6"],
        "result": "0x3",
    }
    w_f = {"phase": "enter", "sc": "write", "args": ["FD_0", "ADDR_7", write_count_arg]}
    w_f_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["FD_0", "ADDR_7", write_count_arg],
        "result": write_count_arg,
    }
    w_1 = {"phase": "enter", "sc": "write", "args": ["0x1", "ADDR_8", "0xa"]}
    w_1_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["0x1", "ADDR_8", "0xa"],
        "result": "0xa",
    }
    x_e = {"phase": "enter", "sc": "exit_group", "args": ["0x0"]}
    return [o_e, o_x, w_f, w_f_x, w_1, w_1_x, x_e]


def test_event_divergence_slice_tolerates_fd_reuse(tmp_path, monkeypatch):
    # read-only open returns fd 3 and closes it; the real write-open reuses fd 3.
    # Slice must anchor at the WRITE-INTENT opener (canonicalizer agrees: FD_0),
    # not the earlier read-only open of the same literal.
    ro_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x0", "0x0"],
    }
    ro_x = {
        "phase": "exit",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x0", "0x0"],
        "result": "0x3",
    }
    c_x = {"phase": "enter", "sc": "close", "args": ["0x3"]}
    o_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_7", "0x241", "0x1b6"],
    }
    o_x = {
        "phase": "exit",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_7", "0x241", "0x1b6"],
        "result": "0x3",
    }
    w_e = {"phase": "enter", "sc": "write", "args": ["FD_0", "ADDR_8", "0x4"]}
    w_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["FD_0", "ADDR_8", "0x4"],
        "result": "0x4",
    }
    stored = canonicalize(
        {
            **_trace(
                "6f6f0a",
                0,
                [
                    ro_e,
                    ro_x,
                    c_x,
                    o_e,
                    o_x,
                    w_e,
                    w_x,
                    {"phase": "enter", "sc": "write", "args": ["0x1", "ADDR_9", "0xa"]},
                    {
                        "phase": "exit",
                        "sc": "write",
                        "args": ["0x1", "ADDR_9", "0xa"],
                        "result": "0xa",
                    },
                    {"phase": "enter", "sc": "exit_group", "args": ["0x0"]},
                ],
            ),
            "files_written": {"out.bin": "61626364"},
        }
    )
    monkeytrace = {
        **_trace(
            "6f6f0a",
            0,
            [
                ro_e,
                ro_x,
                c_x,
                o_e,
                o_x,
                w_e,
                w_x,
                {"phase": "enter", "sc": "write", "args": ["0x1", "ADDR_9", "0xa"]},
                {
                    "phase": "exit",
                    "sc": "write",
                    "args": ["0x1", "ADDR_9", "0xa"],
                    "result": "0xa",
                },
                {"phase": "enter", "sc": "exit_group", "args": ["0x0"]},
            ],
        ),
        "files_written": {"out.bin": "deadbeef"},
    }
    monkeypatch.setattr("reschema.validate.program.record", lambda *a, **k: monkeytrace)
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "files-mismatch"
    sl = v.divergence["dep_slice"]
    enters = [e for e in sl if e["sc"] == "openat" and e["phase"] == "enter"]
    assert [e["args"][2] for e in enters] == ["0x241"]  # write-intent opener ONLY
    assert sl[-1]["sc"] == "write" and sl[-1]["args"][0] == "FD_0"


def test_event_divergence_carries_dependency_slice(tmp_path, monkeypatch):
    # stored: one 4-byte write to out.bin; model: same bytes in two 2-byte
    # writes — same content/stdout (io/files clean), OBS chunk shape diverges.
    # divergence must embed the backward chain: openat opener + the write.
    stored = canonicalize(
        {
            **_trace("6f6f0a", 0, _filewrite_style_events("0x4")),
            "files_written": {"out.bin": "61626364"},
        }
    )
    o_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x241", "0x1b6"],
    }
    o_x = {
        "phase": "exit",
        "sc": "openat",
        "args": ["ADDR_5", "ADDR_6", "0x241", "0x1b6"],
        "result": "0x3",
    }
    w1_e = {"phase": "enter", "sc": "write", "args": ["FD_0", "ADDR_7", "0x2"]}
    w1_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["FD_0", "ADDR_7", "0x2"],
        "result": "0x2",
    }
    w2_e = {"phase": "enter", "sc": "write", "args": ["FD_0", "ADDR_8", "0x2"]}
    w2_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["FD_0", "ADDR_8", "0x2"],
        "result": "0x2",
    }
    w_o = {"phase": "enter", "sc": "write", "args": ["0x1", "ADDR_9", "0xa"]}
    w_o_x = {
        "phase": "exit",
        "sc": "write",
        "args": ["0x1", "ADDR_9", "0xa"],
        "result": "0xa",
    }
    model_trace = {
        **_trace(
            "6f6f0a",
            0,
            [
                o_e,
                o_x,
                w1_e,
                w1_x,
                w2_e,
                w2_x,
                w_o,
                w_o_x,
                {"phase": "enter", "sc": "exit_group", "args": ["0x0"]},
            ],
        ),
        "files_written": {"out.bin": "61626364"},
    }
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: model_trace,
    )
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "event-divergence"
    sl = v.divergence["dep_slice"]
    assert sl[0]["sc"] == "openat" and sl[0]["phase"] == "enter"
    assert any(e["sc"] == "openat" and e.get("result") == "0x3" for e in sl)
    assert sl[-1]["sc"] == "write" and sl[-1]["args"][0] == "FD_0"
    assert len(sl) <= 6  # JSON-tight cap


def test_files_mismatch_carries_dependency_slice(tmp_path, monkeypatch):
    stored = canonicalize(
        {
            **_trace("6f6f6f0a", 0, _filewrite_style_events("0x4")),
            "files_written": {"out.bin": "61626364"},
        }
    )
    monkeypatch.setattr(
        "reschema.validate.program.record",
        lambda *a, **k: {
            **_trace("6f6f6f0a", 0, _filewrite_style_events("0x4")),
            "files_written": {"out.bin": "deadbeef"},
        },
    )
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "files-mismatch"
    sl = v.divergence["dep_slice"]
    assert sl[0]["sc"] == "openat" and sl[0]["phase"] == "enter"
    assert any(e["sc"] == "openat" and e.get("result") == "0x3" for e in sl)
    assert sl[-1]["sc"] == "write" and sl[-1]["args"][0] == "FD_0"


def test_io_mismatch_has_no_dependency_slice(tmp_path, monkeypatch):
    stored = canonicalize(_trace("ff0a", 0, []))
    monkeypatch.setattr(
        "reschema.validate.program.record", lambda *a, **k: _trace("666f6f0a", 0, [])
    )
    v = replay_against(tmp_path / "m", [stored])
    assert not v.ok and v.reason == "io-mismatch"
    assert "dep_slice" not in v.divergence  # stdout mismatch: no fd chain worth slicing


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
