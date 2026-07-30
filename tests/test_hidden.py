import pytest

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


@pytest.fixture(scope="module")
def store():
    build()
    st = TaskStore("rot13::gcc-O1-sym")  # separate slot: other modules share the O2 dir
    st.record_case("a", ["hello"], b"")
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


def test_hidden_inputs_deterministic():
    a = gen_hidden_inputs("rot13::gcc-O2-sym")
    assert a == gen_hidden_inputs("rot13::gcc-O2-sym")  # reproducible run-to-run
    assert a != gen_hidden_inputs("check::gcc-O2-sym")  # seeded by task_id


def test_hidden_inputs_stdin_mode():
    for argv, stdin in gen_hidden_inputs("check::gcc-O2-sym", modes=("stdin",)):
        assert argv == [] and stdin.endswith(b"\n")
