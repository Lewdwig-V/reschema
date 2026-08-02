import json
import shutil

from reschema.corpus.generate import OUT_ROOT, build


def _entry(manifest, task_id):
    return next(x for x in manifest if x["task_id"] == task_id)


def _manifest_file():
    return OUT_ROOT.joinpath("manifest.json").read_text()


def test_targeted_single_slot_matches_full_build():
    full = build()
    want = json.dumps(_entry(full, "filewrite::gcc-O2-sym"))

    shutil.rmtree(OUT_ROOT)
    targeted = build(seed_ids=["filewrite"], matrix=["gcc-O2-sym"])
    assert len(targeted) == 1
    assert json.dumps(_entry(targeted, "filewrite::gcc-O2-sym")) == want


def test_targeted_merge_is_canonical_and_coherent():
    shutil.rmtree(OUT_ROOT)
    build()  # full-build manifest is the byte-for-byte coherence reference
    ref_text = _manifest_file()

    shutil.rmtree(OUT_ROOT)
    rot = build(seed_ids=["rot13"])
    assert len(rot) == 12
    assert {x["seed"] for x in json.loads(_manifest_file())} == {"rot13"}

    # merging a second seed preserves the first and lands in canonical order
    build(seed_ids=["check"])
    merged = json.loads(_manifest_file())
    assert {x["seed"] for x in merged} == {"rot13", "check"}
    assert [x["task_id"] for x in merged] == sorted(
        (x["task_id"] for x in json.loads(ref_text) if x["seed"] in {"rot13", "check"}),
        key=build_sort_key,
    )

    # rebuilding everything converges to the full-build manifest byte-for-byte
    build()
    assert _manifest_file() == ref_text


def build_sort_key(task_id):
    seed, rest = task_id.split("::")
    cc, opt, variant = rest.split("-")
    return (
        seed,
        ["gcc", "clang"].index(cc),
        ["O0", "O1", "O2"].index(opt.lstrip("-")),
        ["sym", "stripped"].index(variant),
    )


def test_full_build_default_unchanged():
    shutil.rmtree(OUT_ROOT)
    m = build()
    assert len(m) == 48
