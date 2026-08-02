import pytest

from reschema.corpus.generate import build
from reschema.driver.spec import Param
from reschema.engine import TaskStore, open_function_task, submit_function


@pytest.fixture(scope="module")
def manifest():
    return build()


def _store(task_id):
    st = TaskStore(task_id)
    st._path("ledger.json").unlink(missing_ok=True)
    return st


def test_template_content_derived_from_driver_constants(manifest):
    t = open_function_task(_store("calc::gcc-O2-sym"), "sum_range")
    tpl = t["abi_template"]
    assert "#include <stdint.h>" in tpl
    assert "sysv_abi" in tpl and "noinline" in tpl
    assert '"kind": "i32"' in tpl and "buffer_i32" in tpl and "cstring" in tpl
    assert "static" in tpl  # static-helper compose rule
    # arity 2 for sum_range: two sketched entries, names free-form
    assert tpl.count('"kind": "i32", "range": [-100, 100]') >= 2


def test_template_returns_void_sketch_has_ret_note(manifest):
    t = open_function_task(_store("calc::gcc-O2-sym"), "scale_buf")
    assert '"ret": "void"' in t["abi_template"]


def _sketch_json(tpl):
    import json as _json
    import re as _re

    m = _re.search(r"\[\s*\{.*?\}\s*\]", tpl, _re.DOTALL)
    return _json.loads(m.group(0)) if m else None


def test_void_sketch_is_a_valid_void_spec(manifest):
    # A pasted sketch must satisfy the engine's own void floor (>=1 memory
    # channel), never an all-i32 shape the spec stage would reject outright.
    t = open_function_task(_store("calc::gcc-O2-sym"), "scale_buf")
    sketch = _sketch_json(t["abi_template"])
    assert sketch[0]["kind"] == "buffer_i32"
    assert sketch[0]["length_param"] == "arg1"
    assert sketch[0]["ret"] == "void"

    t = open_function_task(_store("rot13::gcc-O2-sym"), "rot13")
    sketch = _sketch_json(t["abi_template"])
    assert sketch[0]["kind"] == "cstring"
    assert sketch[0]["ret"] == "void"


# Models written verbatim from the template contract (include + RESCHEMA_FN
# macro + static helpers) must compile and validate with NO ABI-stage rejection.
def test_scalar_from_template_accepted(manifest):
    st = _store("calc::gcc-O2-sym")
    tpl = open_function_task(st, "sum_range")["abi_template"]
    model = (
        tpl
        + "\nRESCHEMA_FN int32_t sum_range(int32_t lo, int32_t hi) {\n"
        + "    int32_t s = 0;\n"
        + "    for (int32_t i = lo; i <= hi; i++) { int32_t v = s + i; s = v < -1000 ? -1000 : v > 1000 ? 1000 : v; }\n"
        + "    return s;\n}\n"
    )
    r = submit_function(
        st,
        "sum_range",
        [
            Param("lo", "i32", range=(-20, 10)).to_json(),
            Param("hi", "i32", range=(10, 30)).to_json(),
        ],
        model,
        seed=1,
        n_fuzz=8,
    )
    assert r["accepted"], r


def test_buffer_i32_void_from_template_accepted(manifest):
    st = _store("calc::gcc-O2-sym")
    tpl = open_function_task(st, "scale_buf")["abi_template"]
    model = (
        tpl
        + "\nRESCHEMA_FN void scale_buf(int32_t *buf, int32_t n, int32_t factor) {\n"
        + "    for (int32_t i = 0; i < n; i++) { int32_t v = buf[i] * factor; buf[i] = v < -100 ? -100 : v > 100 ? 100 : v; }\n"
        + "}\n"
    )
    r = submit_function(
        st,
        "scale_buf",
        [
            Param(
                "buf",
                "buffer_i32",
                direction="in_out",
                length_param="n",
                range=(51, 100),
                ret="void",
            ).to_json()
        ]
        + [
            Param("n", "i32", range=(3, 4)).to_json(),
            Param("factor", "i32", range=(2, 5)).to_json(),
        ],
        model,
        seed=2,
        n_fuzz=8,
    )
    assert r["accepted"], r


def test_cstring_i32_from_template_accepted(manifest):
    st = _store("check::gcc-O2-sym")
    tpl = open_function_task(st, "pw_hash")["abi_template"]
    model = (
        tpl
        + "\nRESCHEMA_FN uint32_t pw_hash(const char *s) {\n"
        + "    uint32_t h = 5381;\n"
        + "    for (; *s; s++) h = h * 33u + (uint8_t)*s;\n"
        + "    return h;\n}\n"
    )
    r = submit_function(
        st, "pw_hash", [Param("s", "cstring").to_json()], model, seed=3, n_fuzz=8
    )
    assert r["accepted"], r


def test_cstring_void_from_template_accepted(manifest):
    st = _store("rot13::gcc-O2-sym")
    tpl = open_function_task(st, "rot13")["abi_template"]
    model = (
        tpl
        + "\nRESCHEMA_FN void rot13(char *s) {\n"
        + "    for (char *p = s; *p; p++) { char c = *p;\n"
        + "        if (c >= 97 && c <= 122) *p = (char)(97 + (c - 97 + 13) % 26);\n"
        + "        else if (c >= 65 && c <= 90) *p = (char)(65 + (c - 65 + 13) % 26);\n"
        + "    }\n}\n"
    )
    r = submit_function(
        st,
        "rot13",
        [Param("in_out", "cstring", direction="in_out", ret="void").to_json()],
        model,
        seed=4,
        n_fuzz=8,
    )
    assert r["accepted"], r
