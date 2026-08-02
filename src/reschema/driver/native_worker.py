"""Level-B native worker: compiles and executes agent C INSIDE the container.

Pure stdlib (the image carries no harness deps); job JSON on stdin, result JSON
on stdout. Runs under rootless podman with --network none --read-only --tmpfs
/tmp; /work is the only writable mount (the task scratch dir). Per-case calls
fork so a segfaulting or hanging model yields a structured crash at its index
instead of killing the validation round.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from reschema.driver.spec import Param

CASE_TIMEOUT_S = 5


def _call(so_path: str, fname: str, params: list[Param], case: dict) -> dict:
    # Fresh image per call: dlopen path-caches, so reusing so_path bleeds
    # statics/globals across calls. ponytail: copied per call, never dlclosed.
    fd, tmp = tempfile.mkstemp(suffix=".so")
    os.close(fd)
    lib = ctypes.CDLL(shutil.copyfile(so_path, tmp), mode=os.RTLD_NOW)
    os.unlink(tmp)
    fn = getattr(lib, fname)
    fn.restype = ctypes.c_int32
    keep, args, watched = [], [], {}
    for p in params:
        v = case[p.name]
        if p.kind == "i32":
            args.append(ctypes.c_int32(v))
        elif p.kind == "cstring":
            b = ctypes.create_string_buffer(bytes(v), len(v))
            keep.append(b)
            args.append(ctypes.cast(b, ctypes.c_char_p))
            watched[p.name] = (b, "cstring")
        elif p.kind == "buffer_i32":
            n = len(v) if isinstance(v, list) else v  # int => out buffer of n elems
            arr = (ctypes.c_int32 * n)(*(v if isinstance(v, list) else [0] * n))
            keep.append(arr)
            args.append(arr)
            watched[p.name] = (arr, "buffer_i32")
    out = {"ret": fn(*args), "mem": {}}
    for name, (buf, kind) in watched.items():
        # Direction-agnostic readback (mirrors call_original's mem keys).
        out["mem"][name] = list(buf) if kind == "buffer_i32" else bytes(buf).hex()
    return out


def _run_case(so_path: str, fname: str, params: list[Param], case: dict) -> dict:
    """One case in a fork: model crash/hang is report data, never our death."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            out = _call(so_path, fname, params, case)
            os.write(w, json.dumps(out).encode())
        finally:
            os._exit(0)
    os.close(w)
    deadline = time.monotonic() + CASE_TIMEOUT_S
    status = None
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(r)
            return {"crash": {"timeout": True}}
        time.sleep(0.005)
    try:
        data = os.read(r, 1 << 20)
    finally:
        os.close(r)
    if status is not None and os.WIFSIGNALED(status):
        return {"crash": {"signal": os.WTERMSIG(status)}}
    try:
        return json.loads(data) if data else {"crash": {"error": "no output"}}
    except ValueError:
        return {"crash": {"error": f"unparseable child output: {data[:100]!r}"}}


def _compile(c_path: str, so_path: str) -> dict | None:
    r = subprocess.run(
        ["gcc", "-O1", "-shared", "-fPIC", c_path, "-o", so_path],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return None if r.returncode == 0 else {"stage": "compile", "stderr": r.stderr}


def _symbol_check(so_path: str, fname: str) -> dict | None:
    fd, tmp = tempfile.mkstemp(suffix=".so")
    os.close(fd)
    try:
        lib = ctypes.CDLL(shutil.copyfile(so_path, tmp), mode=os.RTLD_NOW)
    except OSError as e:
        os.unlink(tmp)
        return {"stage": "link", "detail": f"{type(e).__name__}: {e}"}
    os.unlink(tmp)
    if not hasattr(lib, fname):
        return {"stage": "symbol", "detail": f"'{fname}' not defined by submission"}
    return None


def _validate(job: dict) -> dict:
    params = [Param.from_json(p) for p in job["params"]]
    workdir = f"/work/{job['fname']}"
    err = _compile(workdir + ".c", workdir + ".so")
    if err:
        return err
    err = _symbol_check(workdir + ".so", job["fname"])
    if err:
        return err
    cases = []
    for case in job["cases"]:
        cases.append(
            {
                **{
                    p.name: (
                        bytes.fromhex(case[p.name])
                        if p.kind == "cstring"
                        else case[p.name]
                    )
                    for p in params
                }
            }
        )
    results = []
    for case in cases:
        results.append(_run_case(workdir + ".so", job["fname"], params, case))
        if "crash" in results[-1]:
            break  # validator rejects on the first crash; later cases are moot
    return {"ok": True, "results": results}


def _compile_jobs(job: dict) -> dict:
    """Batch compiles in one container: corpus slots, model binaries. Sources are
    inline (c_source -> /work/<out>.c) or mounted paths (/app/... or absolute)."""
    results = []
    for j in job["jobs"]:
        if "src_path" in j:
            src = j["src_path"]
            src = src if src.startswith("/") else f"/app/{src}"
        else:
            src = f"/work/{j['out']}.c"
            with open(src, "w") as f:
                f.write(j["c_source"])
        r = subprocess.run(
            [j["compiler"], *j["flags"], src, "-o", f"/work/{j['out']}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        results.append({"out": j["out"], "rc": r.returncode, "stderr": r.stderr})
    return {"results": results}


def _compile_link(job: dict) -> dict:
    for name in job["objects"]:
        p = f"/work/{name}"
        with open(p + ".c", "w") as f:
            f.write(job["sources"][name])
        r = subprocess.run(
            [
                "gcc",
                "-O1",
                "-static",
                "-fno-pie",
                "-no-pie",
                "-g0",
                "-c",
                p + ".c",
                "-o",
                p + ".o",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            return {"ok": False, "stderr": r.stderr}
    r = subprocess.run(
        [
            "gcc",
            "-O1",
            "-static",
            "-fno-pie",
            "-no-pie",
            "-g0",
            *[f"/work/{n}.o" for n in job["objects"]],
            "-o",
            f"/work/{job['out']}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return {"ok": r.returncode == 0, "stderr": r.stderr}


def main():
    job = json.load(sys.stdin)
    if job["mode"] == "validate":
        with open(f"/work/{job['fname']}.c", "w") as f:
            f.write(job["c_source"])
        out = _validate(job)
    elif job["mode"] == "compile-link":
        out = _compile_link(job)
    elif job["mode"] == "compile":
        out = _compile_jobs(job)
    else:
        out = {"stage": "internal", "detail": f"unknown mode {job['mode']!r}"}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
