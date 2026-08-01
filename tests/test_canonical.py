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
