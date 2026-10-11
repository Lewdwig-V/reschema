"""Warranted checkers for ReSchema (M8). Shared: the candidate's file view."""

from __future__ import annotations

import json
from collections.abc import Mapping

FILES = ("model.c", "params.json", "cases.json")


def candidate_files(candidate: Mapping[str, bytes]) -> dict[str, bytes] | str:
    """The candidate as files, or why it is malformed.

    Warranted's sandbox captures only /work/result.json (plus notes and the
    workspace), so the worker's `submit` helper writes the files into it as
    {name: text}. Those entries join any files the candidate carries directly
    (a scripted environment's); a name given both ways is malformed, so one
    candidate never has two readings."""
    files = {n: candidate[n] for n in FILES if n in candidate}
    raw = candidate.get("result.json")
    if raw is None:
        return files
    try:
        bundle = json.loads(raw)
    except (ValueError, RecursionError):
        return "result.json is not JSON"
    if (
        type(bundle) is not dict
        or not set(bundle) <= set(FILES)
        or not all(type(v) is str for v in bundle.values())
    ):
        return f"result.json maps file names {list(FILES)} to their text"
    if set(bundle) & set(files):
        return "a file is given both in result.json and beside it"
    # surrogatepass: a lone surrogate becomes bytes the UTF-8 checks reject
    return files | {n: v.encode(errors="surrogatepass") for n, v in bundle.items()}
