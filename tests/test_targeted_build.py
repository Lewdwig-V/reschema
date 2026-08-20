import json
import shutil

import pytest

import reschema.corpus.generate as genmod
from reschema.corpus.generate import build


# Every test in this file builds against its OWN corpus root: build() mutates
# the tree it writes (rmtree/merge/prune flows below are destructive), and a
# shared root makes test outcomes order-dependent (fatal under pytest-xdist).
@pytest.fixture
def root(tmp_path):
    return tmp_path / "corpus"


def _entry(manifest, task_id):
    return next(x for x in manifest if x["task_id"] == task_id)


def _manifest_file(root):
    return root.joinpath("manifest.json").read_text()


# Merge/prune/order semantics are fully exercised by REAL compiles of tiny
# stub seeds — the build+convergence logic cannot read sizes — while a full
# 60-slot matrix rebuild per test costs ~30s per worker. Stub seed sources
# define exactly the function names the registry expects, and the manifests
# they generate are still REAL (addresses/sizes come from real ELF binaries).
_STUB_SOURCES = {
    "rot13": (
        "__attribute__((sysv_abi, noinline)) int rot13_char(int c){ return c; }\n"
        "__attribute__((sysv_abi, noinline)) int rot13(char *s){ return 0; }\n"
        "int main(void){ return 0; }\n"
    ),
    "check": (
        "__attribute__((sysv_abi, noinline)) unsigned pw_hash(const char *s){ return 2; }\n"
        "__attribute__((sysv_abi, noinline)) int check_pw(const char *s){ return 1; }\n"
        "int main(void){ return 0; }\n"
    ),
    "filewrite": (
        "__attribute__((sysv_abi, noinline)) int xform_byte(int b, int i){ return b ^ i; }\n"
        "int main(void){ return 0; }\n"
    ),
}


@pytest.fixture
def stub_seeds(tmp_path, monkeypatch):
    d = tmp_path / "stubseeds"
    d.mkdir()
    for name, src in _STUB_SOURCES.items():
        (d / f"{name}.c").write_text(src)
    monkeypatch.setattr(genmod, "SEEDS", d)
    return d


def test_full_build_default_unchanged(built_corpus):
    # counting the real default build: the session fixture already ran it
    assert len(built_corpus) == 60


def test_targeted_single_slot_matches_full_build(root, stub_seeds):
    full = build(root)
    want = json.dumps(_entry(full, "filewrite::gcc-O2-sym"))

    shutil.rmtree(root)
    targeted = build(root, seed_ids=["filewrite"], matrix=["gcc-O2-sym"])
    assert len(targeted) == 1
    assert json.dumps(_entry(targeted, "filewrite::gcc-O2-sym")) == want


def test_targeted_merge_is_canonical_and_coherent(root, stub_seeds):
    # Ordering/coherence assertions hold over the sym-only slice: the strip
    # round and half the slots add no evidence to the merge logic this tests
    # (strip correctness itself is pinned two tests down).
    build(root, matrix=["gcc-O2-sym", "clang-O2-sym"])
    ref_text = _manifest_file(root)

    shutil.rmtree(root)
    rot = build(root, seed_ids=["rot13"], matrix=["gcc-O2-sym", "clang-O2-sym"])
    assert len(rot) == 2
    assert {x["seed"] for x in json.loads(_manifest_file(root))} == {"rot13"}

    # merging a second seed preserves the first and lands in canonical order
    build(root, seed_ids=["check"], matrix=["gcc-O2-sym", "clang-O2-sym"])
    merged = json.loads(_manifest_file(root))
    assert {x["seed"] for x in merged} == {"rot13", "check"}
    assert [x["task_id"] for x in merged] == sorted(
        (x["task_id"] for x in json.loads(ref_text) if x["seed"] in {"rot13", "check"}),
        key=build_sort_key,
    )

    # rebuilding everything converges to the full-build manifest byte-for-byte
    build(root, matrix=["gcc-O2-sym", "clang-O2-sym"])
    assert _manifest_file(root) == ref_text


def build_sort_key(task_id):
    seed, rest = task_id.split("::")
    cc, opt, variant = rest.split("-")
    return (
        seed,
        ["gcc", "clang"].index(cc),
        ["O0", "O1", "O2"].index(opt.lstrip("-")),
        ["sym", "stripped"].index(variant),
    )


def test_full_build_prunes_stale_slots_but_targeted_preserves_them(root, stub_seeds):
    build(root)  # full: manifest from scratch
    mf = root / "manifest.json"
    ghost = {
        "task_id": "ghost::gcc-O2-sym",
        "seed": "ghost",
        "compiler": "gcc",
        "opt": "-O2",
        "stripped": False,
        "binary": "/nope/prog",
        "functions": {},
    }
    with_ghost = json.loads(mf.read_text()) + [ghost]
    mf.write_text(json.dumps(with_ghost))

    # targeted merges: a plight outside the filter scope must survive untouched
    build(root, seed_ids=["rot13"])
    assert any(x["seed"] == "ghost" for x in json.loads(mf.read_text()))

    # full rebuild regenerates from the current plan (stale slot pruned)
    build(root)
    assert not any(x["seed"] == "ghost" for x in json.loads(mf.read_text()))


def test_strip_runs_in_the_toolchain_image_and_never_on_host(monkeypatch, tmp_path):
    import reschema.corpus.generate as gen

    calls = []

    def fake_worker(job, workdir, timeout=None):
        calls.append(job)
        if job["mode"] == "compile":
            for j in job["jobs"]:
                (workdir / j["out"]).write_bytes(b"")
            return {
                "results": [
                    {"out": j["out"], "rc": 0, "stderr": ""} for j in job["jobs"]
                ]
            }
        if job["mode"] == "strip":
            return {
                "results": [{"file": f, "rc": 0, "stderr": ""} for f in job["files"]]
            }
        raise AssertionError(job)

    monkeypatch.setattr(gen.podrun, "run_worker", fake_worker)
    monkeypatch.setattr(
        gen,
        "_symtab",
        lambda _p: {n: (0x1000, 8) for fns in gen.FUNCS.values() for n in fns},
    )
    out = gen.build(tmp_path)
    assert len(out) == 60
    modes = [c["mode"] for c in calls]
    assert modes == ["compile", "strip"]
    strip_job = calls[1]
    assert len(strip_job["files"]) == 30  # all stripped variants, one pod round


def test_no_host_subprocess_for_strip(monkeypatch, tmp_path):
    import subprocess as sp

    import reschema.corpus.generate as gen

    seen: list[list[str]] = []

    def fake_worker(job, workdir, timeout=None):
        if job["mode"] == "compile":
            for j in job["jobs"]:
                (workdir / j["out"]).write_bytes(b"")
            return {
                "results": [
                    {"out": j["out"], "rc": 0, "stderr": ""} for j in job["jobs"]
                ]
            }
        return {
            "results": [
                {"file": f, "rc": 0, "stderr": ""} for f in job.get("files", [])
            ]
        }

    monkeypatch.setattr(gen.podrun, "run_worker", fake_worker)
    monkeypatch.setattr(
        gen,
        "_symtab",
        lambda _p: {n: (0x1000, 8) for fns in gen.FUNCS.values() for n in fns},
    )
    monkeypatch.setattr(
        sp,
        "run",
        lambda *a, **k: seen.append(list(a[0])) or sp.CompletedProcess(a[0], 0),
    )
    gen.build(tmp_path)
    assert not any("strip" in cmd for cmd in seen)
