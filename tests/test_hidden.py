import random
from itertools import islice
from unittest.mock import Mock

import pytest
from conftest import wipe_task

import reschema.engine as eng
from reschema.engine import STDIN_DRIVEN, TaskStore, submit_program
from reschema.validate.program import hidden_input_stream


def gen_hidden_inputs(
    task_id: str, n: int = 8, modes: tuple = ("argv",), seed: str | None = None
) -> list[tuple[list[str], bytes]]:
    """Deterministic fresh inputs given a seed; submissions pass fresh entropy."""
    rng = random.Random(seed if seed is not None else f"hidden:{task_id}")
    return list(islice(hidden_input_stream(rng, modes), n))


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


# Task dirs are persisted across runs: leftover trace/ledger files would break
# stage attribution, so each fixture starts from a clean dir (wipe_task).


@pytest.fixture(scope="module", autouse=True)
def _corpus_once(built_corpus):
    pass


@pytest.fixture(scope="module")
def store():
    st = TaskStore("rot13::gcc-O1-sym")  # separate slot: other modules share the O2 dir
    wipe_task(st)
    st.record_case("a", ["hello"], b"")
    return st


@pytest.fixture(scope="module")
def check_store():
    st = TaskStore(
        "check::gcc-O1-sym"
    )  # stdin-driven; no other module owns a check dir
    wipe_task(st)
    st.record_case("a", [], b"hello\n")  # STORED case fed via stdin bytes
    return st


# Equivalent model: reads stdin via getchar instead of fread — same observable
# behavior (file bytes, stdout line, stdio chunking) through a different code path.
GOOD_FW = r"""
#include <stdio.h>
#include <stdint.h>
int main(void){
 unsigned char buf[8192]; size_t n=0; int c;
 while((c=getchar())!=EOF && n<sizeof buf) buf[n++]=(unsigned char)c;
 uint32_t h=5381u;
 for(size_t i=0;i<n;i++){ buf[i]=(unsigned char)(buf[i] ^ (unsigned char)((uint32_t)i*31u+7u)); h=h*33u+buf[i]; }
 FILE*f=fopen("out.bin","wb"); if(!f){puts("open failed");return 2;}
 fwrite(buf,1,n,f); fclose(f);
 printf("%zu bytes -> out.bin djb2=%08x\n", n, h);
 return 0;
}
"""

# Claims the file on stdout but never creates it: must fail on the FILE channel
# (io channels are clean), at the recorded stage.
NOFILE_FW = r"""
#include <stdio.h>
#include <stdint.h>
int main(void){
 unsigned char buf[8192]; size_t n=0; int c;
 while((c=getchar())!=EOF && n<sizeof buf) buf[n++]=(unsigned char)c;
 uint32_t h=5381u;
 for(size_t i=0;i<n;i++){ buf[i]=(unsigned char)(buf[i] ^ (unsigned char)((uint32_t)i*31u+7u)); h=h*33u+buf[i]; }
 printf("%zu bytes -> out.bin djb2=%08x\n", n, h);
 return 0;
}
"""

# Memorizes the recorded case ("hello\n"): correct file+stdout ONLY for that input;
# other inputs get correct stdout but WRONG file bytes — hidden must catch it.
MEMO_FW = r"""
#include <stdio.h>
#include <string.h>
#include <stdint.h>
int main(void){
 unsigned char buf[8192]; size_t n=0; int c;
 while((c=getchar())!=EOF && n<sizeof buf) buf[n++]=(unsigned char)c;
 if(n==6 && !memcmp(buf,"hello\n",6)){
   /* pre-image of xform("hello\n"), like check's crackme pre-image */
   static const unsigned char gt[6]={0x6f,0x43,0x29,0x08,0xec,0xa8};
   uint32_t h=5381u; for(unsigned i=0;i<6;i++) h=h*33u+gt[i];
   FILE*f=fopen("out.bin","wb"); fwrite(gt,1,6,f); fclose(f);
   printf("6 bytes -> out.bin djb2=%08x\n", h);
   return 0;
 }
 uint32_t h=5381u;
 for(size_t i=0;i<n;i++){ buf[i]=(unsigned char)(buf[i] ^ (unsigned char)((uint32_t)i*31u+7u)); h=h*33u+buf[i]; }
 static const unsigned char z[2]={'z','z'};
 FILE*f=fopen("out.bin","wb"); fwrite(z,1,2,f); fclose(f);  /* wrong bytes */
 printf("%zu bytes -> out.bin djb2=%08x\n", n, h);
 return 0;
}
"""


@pytest.fixture(scope="module")
def fw_store():
    st = TaskStore("filewrite::gcc-O1-sym")  # no other module owns a filewrite dir
    wipe_task(st)
    st.record_case("a", [], b"hello\n")
    return st


def test_generalizing_file_model_passes_hidden(fw_store):
    r = submit_program(fw_store, GOOD_FW)
    assert r["accepted"], r


def test_missing_file_model_fails_recorded(fw_store):
    r = submit_program(fw_store, NOFILE_FW)
    assert not r["accepted"]
    assert r["stage"] == "recorded"
    assert r["reason"] == "files-mismatch"  # io is clean; only the file channel differs
    assert r["divergence"]["expected"] != {}
    assert r["divergence"]["actual"] == {}


def test_memorized_file_model_fails_hidden(fw_store):
    r = submit_program(fw_store, MEMO_FW)
    assert not r["accepted"]
    assert r["stage"] == "hidden"  # recorded case replayed clean, hidden caught it
    assert r["reason"] == "files-mismatch"
    # hidden draws carry stdin, so the expected file is a non-empty transform —
    # proving the hidden suite feeds real stdin, not empty draws.
    assert r["divergence"]["expected"].get("out.bin") not in (None, "")
    assert r["divergence"]["actual"]["out.bin"] == "7a7a"  # "zz"


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
    assert a != gen_hidden_inputs(
        "rot13::gcc-O2-sym", seed="s2"
    )  # seed drives the draw


def test_hidden_inputs_stdin_mode():
    for argv, stdin in gen_hidden_inputs("check::gcc-O2-sym", modes=("stdin",)):
        assert argv == [] and stdin.endswith(b"\n")


def test_filewrite_is_stdin_driven():
    # Hidden draws must carry stdin (the seed ignores argv); otherwise both gate
    # and model see empty input and the hidden suite proves nothing.
    assert "filewrite" in STDIN_DRIVEN


def test_hidden_inputs_filewrite_byte_mode():
    # filewrite consumes raw bytes (fread): hidden draws must span the byte
    # domain — NUL + non-printable guaranteed per draw, not just line text.
    kw = {"modes": ("stdin-bytes",), "seed": "s1"}
    a = gen_hidden_inputs("filewrite::gcc-O2-sym", **kw)
    assert a == gen_hidden_inputs("filewrite::gcc-O2-sym", **kw)  # pinned by seed
    for argv, stdin in a:
        assert argv == []
        assert b"\0" in stdin
        assert any(b >= 0x80 for b in stdin)


# C-string/line overfit: passes recorded + text hidden draws, but strlen
# truncates at the first NUL — wrong out.bin (and stdout) for real byte stdin.
OVERFIT_FW = r"""
#include <stdio.h>
#include <string.h>
#include <stdint.h>
int main(void){
 char buf[8192];
 if(!fgets(buf, sizeof buf, stdin)) return 2;
 size_t n = strlen(buf);
 uint32_t h = 5381u;
 for(size_t i=0;i<n;i++){ buf[i]=(char)(buf[i] ^ (char)((uint32_t)i*31u+7u)); h=h*33u+(unsigned char)buf[i]; }
 FILE*f=fopen("out.bin","wb"); fwrite(buf,1,n,f); fclose(f);
 printf("%zu bytes -> out.bin djb2=%08x\n", n, h);
 return 0;
}
"""


def test_cstring_overfit_model_fails_hidden(fw_store):
    r = submit_program(fw_store, OVERFIT_FW)
    assert not r["accepted"], "text-only hidden draws let the strlen overfit through"
    assert r["stage"] == "hidden"  # recorded "hello\n" contains no NUL — passes
    # stdout prints the truncated length: clean divergence on every NUL draw.
    assert r["reason"] == "io-mismatch"
