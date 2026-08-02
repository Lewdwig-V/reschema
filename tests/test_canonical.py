from reschema.exec.canonical import canonicalize


def _trace(events):
    return {
        "argv": ["/home/x/reschema/.reschema/corpus/rot13/gcc-O2-sym/prog", "abc"],
        "stdin_hex": "",
        "stdin_sha256": "",
        "stdout": "aa",
        "stderr": "",
        "exit_code": 0,
        "files_written": {},
        "events": events,
    }


def test_addresses_ordinal_mapped():
    t = _trace(
        [
            {
                "phase": "exit",
                "sc": "mmap",
                "args": ["0x0", "0x1000"],
                "result": "0x7f12ab000000",
            },
            {
                "phase": "exit",
                "sc": "mmap",
                "args": ["0x0", "0x1000"],
                "result": "0x7f12ab001000",
            },
            {"phase": "enter", "sc": "write", "args": ["0x1", "0x7f12ab000000", "0x5"]},
        ]
    )
    c = canonicalize(t)
    assert c["events"][0]["result"] == "ADDR_0"
    assert c["events"][1]["result"] == "ADDR_1"
    assert c["events"][2]["args"] == [
        "0x1",
        "ADDR_0",
        "0x5",
    ]  # fd 1 untouched, same addr same ordinal


def test_small_hex_not_address():
    t = _trace([{"phase": "enter", "sc": "write", "args": ["0x1", "0x100", "0x5"]}])
    assert canonicalize(t)["events"][0]["args"] == [
        "0x1",
        "0x100",
        "0x5",
    ]  # <6 hex digits = not an address


def test_argv_hostpath_normalized():
    t = _trace([])
    c = canonicalize(t)
    assert c["argv"][0] == "prog" and c["argv"][1] == "abc"


def test_input_not_mutated():
    t = _trace(
        [{"phase": "exit", "sc": "brk", "args": ["0x0"], "result": "0x55aa00000000"}]
    )
    canonicalize(t)
    assert t["events"][0]["result"] == "0x55aa00000000"  # original untouched


def test_non_address_results_pass_through():
    t = _trace(
        [
            {"phase": "exit", "sc": "mmap", "args": [], "result": "0x0"},  # failure
            {"phase": "exit", "sc": "exit_group", "args": ["0x0"], "result": "None"},
        ]
    )
    c = canonicalize(t)
    assert c["events"][0]["result"] == "0x0"
    assert c["events"][1]["result"] == "None"


def _fd_trace():
    """filewrite-shaped: open out.bin -> fd 3, write it, close it, write stdout."""
    o_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["0xffffffffffffff9c", "0x480013", "0x241", "0x1b6"],
    }
    o_x = {
        "phase": "exit",
        "sc": "openat",
        "args": ["0xffffffffffffff9c", "0x480013", "0x241", "0x1b6"],
        "result": "0x3",
    }
    w_f = {"phase": "enter", "sc": "write", "args": ["0x3", "0x480100", "0x6"]}
    c_f = {"phase": "enter", "sc": "close", "args": ["0x3"]}
    w_1 = {"phase": "enter", "sc": "write", "args": ["0x1", "0x480200", "0x15"]}
    return _trace([o_e, o_x, w_f, c_f, w_1])


def test_fd_ordinals_from_write_open():
    c = canonicalize(_fd_trace())
    assert c["events"][2]["args"][0] == "FD_0"  # write to out.bin
    assert c["events"][3]["args"][0] == "FD_0"  # close same fd
    assert c["events"][4]["args"][0] == "0x1"  # stdout fd stays literal (ABI)


def test_readonly_opens_do_not_shift_fd_ordinals():
    t = _fd_trace()
    ro_e = {
        "phase": "enter",
        "sc": "openat",
        "args": ["0xffffffffffffff9c", "0x480013", "0x0", "0x0"],
    }
    ro_x = {"phase": "exit", "sc": "openat", "args": ro_e["args"], "result": "0x3"}
    # a model's spurious READ-only open before the real one must not renumber it:
    t["events"] = [ro_e, ro_x, *t["events"]]
    c = canonicalize(t)
    assert c["events"][4]["args"][0] == "FD_0"  # out.bin is still the first WRITE fd


def test_absolute_hostpaths_ordinal_mapped():
    t = _trace(
        [
            {
                "phase": "fault",
                "sc": "crash",
                "args": ["QlErrorCoreUnmapped: /tmp/reschema-rootfs-abc/prog died"],
            },
            {
                "phase": "fault",
                "sc": "crash",
                "args": ["again /tmp/reschema-rootfs-abc/prog and /etc/hostname"],
            },
        ]
    )
    c = canonicalize(t)
    assert c["events"][0]["args"] == ["QlErrorCoreUnmapped: PATH_0 died"]
    assert c["events"][1]["args"] == ["again PATH_0 and PATH_1"]


from reschema.corpus.generate import build
from reschema.exec.recorder import record


def _v1(trace):
    # v1 rules as frozen reference: ADDR ordinals + argv[0] basename, nothing else.
    import re
    from pathlib import Path

    addr_of = {}

    def m(a):
        if a not in addr_of:
            addr_of[a] = f"ADDR_{len(addr_of)}"
        return addr_of[a]

    ADDR = re.compile(r"0x[0-9a-f]{6,}")
    evs = []
    for e in trace["events"]:
        e = dict(e)
        e["args"] = [m(a) if ADDR.fullmatch(a) else a for a in e["args"]]
        if (
            "result" in e
            and isinstance(e["result"], str)
            and ADDR.fullmatch(e["result"])
        ):
            e["result"] = m(e["result"])
        evs.append(e)
    t = dict(trace)
    t["events"] = evs
    t["argv"] = [Path(t["argv"][0]).name, *t["argv"][1:]]
    return t


def test_v2_byte_identical_for_no_open_seeds():
    m = build()
    for seed, argv, stdin in (
        ("rot13", ["hello"], b""),
        ("check", [], b"hunter2\n"),
        ("calc", [], b""),
    ):
        binary = next(
            x["binary"]
            for x in m
            if x["seed"] == seed
            and x["compiler"] == "gcc"
            and x["opt"] == "-O2"
            and not x["stripped"]
        )
        tr = record(binary, argv, stdin)
        assert tr["files_written"] == {}  # these seeds never open files
        assert canonicalize(tr) == _v1(tr), f"v2 changed canonical bytes for {seed}"


def test_v2_fd_ordinals_on_real_filewrite_trace():
    m = build()
    binary = next(
        x["binary"]
        for x in m
        if x["seed"] == "filewrite"
        and x["compiler"] == "gcc"
        and x["opt"] == "-O2"
        and not x["stripped"]
    )
    c = canonicalize(record(binary, [], b"hello\n"))
    by_sc = [e for e in c["events"] if e["sc"] == "write" and e["phase"] == "enter"]
    fds = [e["args"][0] for e in by_sc]
    assert "FD_0" in fds and "0x1" in fds  # out.bin writes ordinalized, stdout literal
    assert canonicalize(record(binary, [], b"hello\n")) == c  # deterministic
