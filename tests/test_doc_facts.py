"""Doc-facts guard: numbers and lists the prose states, pinned to the code.

ARCHITECTURE.md's maintenance policy ("any PR that changes behavior described
here must update this file") is reviewer-enforced; this test is the mechanical
backstop for the facts that are cheap to derive. It caught README/ARCHITECTURE
still saying 4 seeds / 48 slots after pkfmt made it 5 / 60.

Pure: no corpus, no podman, no emulation. Historical mentions (decision-record
"History:" notes, per-seed slot counts) are deliberately not matched — only
the corpus-matrix phrasings below are.
"""

import re
from pathlib import Path

import anyio
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from reschema.corpus.generate import COMPILERS, FUNCS, OPTS
from reschema.engine import HIDDEN_N
from reschema.exec.canonical import CANONICALIZER_VERSION
from reschema.mcp.server import server
from reschema.validate.function import N_FUZZ

REPO = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "ARCHITECTURE.md", "AGENTS.md")
WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}
NUM = r"(\d+|" + "|".join(WORDS) + r")"


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else WORDS[tok.lower()]


def _hits(pattern: str) -> list[tuple[str, int, re.Match]]:
    out = []
    for name in DOCS:
        for lineno, line in enumerate((REPO / name).read_text().splitlines(), 1):
            for m in re.finditer(pattern, line, re.IGNORECASE):
                out.append((name, lineno, m))
    return out


def _check(pattern: str, expected, convert=_n) -> None:
    hits = _hits(pattern)
    assert hits, f"pattern {pattern!r} matched nothing — guard went vacuous"
    bad = [
        f"{f}:{ln}: {m.group(0)!r}"
        for f, ln, m in hits
        if convert(m.group(1)) != expected
    ]
    assert not bad, f"docs disagree with code (expected {expected}):\n" + "\n".join(bad)


def test_slot_count():
    slots = len(FUNCS) * len(COMPILERS) * len(OPTS) * 2  # sym + stripped
    # "60-slot corpus/matrix" and the "... = **60 build slots**" derivation.
    _check(NUM + r"-slot\b", slots)
    _check(r"=\s*\**" + NUM + r" (?:build )?slots", slots)


def test_seed_count_and_lists():
    seeds = set(FUNCS)
    _check(NUM + r" seeds ×", len(seeds))
    _check(NUM + r" seeds \(", len(seeds))
    # Every enumerated seed list in prose names exactly the shipped seeds.
    for f, ln, m in _hits(r"seeds \(([^)]*)\)"):
        listed = set(re.findall(r"`([a-z0-9_]+)`", m.group(1)))
        assert listed == seeds, f"{f}:{ln}: lists {sorted(listed)}"
    # README corpus table: one row per seed.
    readme = (REPO / "README.md").read_text()
    table = readme.split("## Corpus", 1)[1].split("\n## ", 1)[0]
    rows = set(re.findall(r"^\| `([a-z0-9_]+)` \|", table, re.MULTILINE))
    assert rows == seeds, f"README corpus table rows {sorted(rows)}"


def test_tool_count():
    async def go():
        async with InMemoryTransport(server) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return len((await s.list_tools()).tools)

    n_tools = anyio.run(go)
    _check(NUM + r" (?:MCP )?tools\b", n_tools)


def test_tuning_constants():
    _check(r"HIDDEN_N\s*=\s*(\d+)", HIDDEN_N, int)
    _check(r"N_FUZZ\s*=\s*(\d+)", N_FUZZ, int)


def test_canonicalizer_version():
    v = CANONICALIZER_VERSION
    _check(r'CANONICALIZER_VERSION\s*=\s*"([\d.]+)"', v, str)
    _check(r"rules v(\d+\.\d+)", v, str)
    _check(r"\(v(\d+\.\d+)\)", v, str)
