# Research survey: agentic RE harnesses (2026-10)

Source material behind the "Backlog — research survey (2026-10)" sections of
[roadmap.md](../../roadmap.md) and behind [rejected-ideas.md](../../rejected-ideas.md).
Kept so that ideas not yet scheduled are not lost, and so a later proposal
can check what was already considered.

- [report.md](report.md): the synthesized report. It covers the state of the
  art as of October 2026, how ReSchema compares, twelve ranked ideas to
  adopt, and nine to refuse.
- `notes/`: the four research notes the report was built from. Each section
  is split into Takeaway, Cited Findings, Inferences and Gaps.
  - [agentic-re-tools-and-agents.md](notes/agentic-re-tools-and-agents.md):
    MCP servers for disassemblers and decompilers, autonomous RE and
    vulnerability-research agents, sandboxing.
  - [neural-decompilation-methods.md](notes/neural-decompilation-methods.md):
    LLM decompiler model lines, refinement loops, type recovery.
  - [evaluation-and-verification.md](notes/evaluation-and-verification.md):
    benchmarks, metrics, equivalence checking, reward hacking.
  - [adjacent-harness-patterns.md](notes/adjacent-harness-patterns.md):
    code-RL environments, self-play, memory, translation pipelines, Lean
    provers, tool design.

## How far to trust it

arXiv full text was unreachable when this was written. Most paper facts come
from search abstracts and secondary summaries, and nearly all figures are
author-reported and unreplicated 2026 preprints. The notes flag conflicting
numbers inline. Check the key figures against the full papers before citing
them outside this repo. That applies especially to the Decompile-Diverge
method section, the SRE-Bench tables and the hackability audit.

## Errata

These files are kept as written. Where they disagree with the code, or with
corrections made since, the code and the roadmap win:

1. **Level-B crash coverage.** `notes/agentic-re-tools-and-agents.md` §6
   says level B's `crash` field already covers "crash absence". It does not.
   `validate/function.py` skips every fuzz case on which the *original*
   faults, so a model that suppresses the original's crash is never
   compared on that input. The report corrects this, and it is a P0 roadmap
   gap.
2. **Seed pinning.** The report and notes state that production never pins
   seeds. In function mode, `submit_model` forwards an agent-supplied
   `seed=` to the fuzz draw. This was found in review of PR #137 and is a
   P0 roadmap gap.
3. **`status` is not a human-only surface.** The report and
   `notes/evaluation-and-verification.md` suggest putting judge-strength and
   regression telemetry "in `status` for humans". `status` is one of the
   five agent-facing MCP tools. That telemetry belongs in ledger records and
   benchmark/admin reports (rejected-ideas §4).
4. **`experiment` is not read-only.** `notes/agentic-re-tools-and-agents.md`
   §6 suggests annotating `experiment` as read-only and idempotent. It
   increments `probes`, and in program mode it persists traces that later
   validation replays. Only `status` qualifies as read-only (roadmap,
   "MCP tool annotations").
