import pytest

from reschema.corpus.generate import build
from reschema.engine import TaskStore
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
