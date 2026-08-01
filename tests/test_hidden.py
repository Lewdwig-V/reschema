from unittest.mock import Mock

import pytest

import reschema.engine as eng
from reschema.corpus.generate import build
from reschema.engine import TaskStore, submit_program
from reschema.validate.program import gen_hidden_inputs

GOOD = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
# Memorizes the recorded case: prints "uryyb" (= rot13("hello")) for every input.
HARD = '#include <stdio.h>\nint main(void){puts("uryyb");return 0;}'

# Memorizes the recorded stdin case: check prints "NOPE"/exit 1 on a wrong password,
# so the model echoes that only for the recorded input and emits garbage otherwise.
HARD_STDIN = r"""
#include <stdio.h>
#include <string.h>
int main(void){ char buf[64];
 if(!fgets(buf,sizeof buf,stdin)) return 2;
 buf[strcspn(buf,"\n")]=0;
 if(!strcmp(buf,"hello")){ puts("NOPE"); return 1; }
 puts("zzz"); return 0; }
"""


def _wipe(st):
    # Task dirs are persisted across runs: leftover trace/ledger files would break
    # stage attribution, so each fixture starts from a clean dir.
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st._path("ledger.json").unlink(missing_ok=True)


@pytest.fixture(scope="module")
def store():
    build()
    st = TaskStore("rot13::gcc-O1-sym")  # separate slot: other modules share the O2 dir
    _wipe(st)
    st.record_case("a", ["hello"], b"")
    return st


@pytest.fixture(scope="module")
def check_store():
    build()
    st = TaskStore("check::gcc-O1-sym")  # stdin-driven; no other module owns a check dir
    _wipe(st)
    st.record_case("a", [], b"hello\n")  # STORED case fed via stdin bytes
    return st


def test_generalizing_model_passes_hidden(store):
    r = submit_program(store, GOOD)
    assert r["accepted"], r


def test_hardcoded_model_fails_hidden(store):
    r = submit_program(store, HARD)
    assert not r["accepted"]
    assert r["stage"] == "hidden"  # passed recorded replay, caught by hidden inputs
    assert r["reason"] == "io-mismatch"
    assert r["divergence"]["argv"] != ["hello"]  # divergence is on an unseen input


def test_hardcoded_stdin_model_fails_hidden(check_store):
    # stdin path end-to-end: the ((), stdin_hex) collision key shields the recorded
    # stdin case from redraw, but fresh hidden stdin inputs still catch the hardcoder.
    r = submit_program(check_store, HARD_STDIN)
    assert not r["accepted"]
    assert r["stage"] == "hidden"
    assert r["reason"] == "io-mismatch"


def test_hidden_seed_fresh_per_submission(store, monkeypatch):
    # Production hidden draws take fresh entropy per submission (unguessable), and a
    # stream that can't yield inputs is a loud rejection, never a vacuous pass.
    rngs = []
    monkeypatch.setattr(
        eng, "hidden_input_stream", lambda rng, modes: rngs.append(rng) or iter([])
    )
    toks = Mock(side_effect=["aa" * 16, "bb" * 16])
    monkeypatch.setattr(eng.secrets, "token_hex", toks)
    r = submit_program(store, GOOD)
    assert not r["accepted"] and r["reason"] == "hidden-starvation"
    submit_program(store, GOOD)
    assert toks.call_count == 2  # entropy drawn on every submission...
    assert rngs[0].getstate() != rngs[1].getstate()  # ...so streams differ run-to-run


def test_hidden_inputs_deterministic():
    a = gen_hidden_inputs("rot13::gcc-O2-sym", seed="s1")
    assert a == gen_hidden_inputs("rot13::gcc-O2-sym", seed="s1")  # pinned by seed
    assert a != gen_hidden_inputs("rot13::gcc-O2-sym", seed="s2")  # seed drives the draw


def test_hidden_inputs_stdin_mode():
    for argv, stdin in gen_hidden_inputs("check::gcc-O2-sym", modes=("stdin",)):
        assert argv == [] and stdin.endswith(b"\n")
