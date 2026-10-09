# Rejected ideas

Ideas that are popular in the agentic reverse-engineering, decompilation and
code-RL literature, that ReSchema has **considered and deliberately
refused**. Each entry records the invariant it would break, the evidence
behind the refusal, and what would have to change to reopen it. A proposal
that matches an entry here should start by answering its "Reopen only if"
line — not by re-arguing the idea from scratch.

Provenance: the October 2026 research survey of agentic RE harnesses,
neural decompilation, evaluation/verification and adjacent code-RL harness
design (kept in
[research/2026-10-agentic-re-harnesses/](research/2026-10-agentic-re-harnesses/README.md)),
plus the REA comparison recorded in [roadmap.md](roadmap.md).
Most survey sources were read as abstracts or secondary summaries (arXiv
full text was unreachable), and nearly all figures are author-reported;
treat the numbers below as directional and re-check them against the full
papers before citing externally. The refusals rest on ReSchema's own
invariants, not on any single number.

The accepted counterparts live in [roadmap.md](roadmap.md) under
"Backlog — research survey (2026-10)".

---

## 1. Recompile / test-pass / similarity / readability metrics in acceptance or E

**Refused:** recompile rate, shipped-test pass rate, edit similarity, R2I,
or LLM-judged readability as any part of acceptance or the efficiency
metric E.

**Breaks:** judge-only acceptance — behavior under the emulated oracle is
the sole criterion.

**Evidence:** each of these rises while behavior falls. LLM refinement of
Ghidra output raised build rate 75→90% while behavior-matched rate fell
74→62%, and 4.9–13% of candidates passing every shipped test still
diverged (Decompile-Diverge, arXiv 2609.05370). Compile-only feedback gave
0% behavioral correctness at 91–99% compile rates (Agent4Decompile).
DecompileBench reports LLM decompilers more readable but 52.2% lower on
functional correctness. Readability gains on HumanEval-derived sets are
partly memorization (arXiv 2609.17236). Agents gamed a single similarity
metric at readability's expense (arXiv 2606.06838).

**Where it belongs instead:** readability is the planned refactoring
agent's concern, scored only lexicographically *after* equivalence (as in
D-LiFT), and re-verified against the **original binary's** traces and
fuzz — not only against the world model — so the oracle cannot drift.

**Reopen only if:** a metric is shown to be monotone in behavioral
equivalence on ReSchema's own corpus (it cannot rise while behavior falls).

## 2. LLM-generated tests or learned verifiers as the oracle

**Refused:** model-written tests, reward models, or LLM judges deciding
acceptance.

**Breaks:** emulated ground truth and the harness-owned `N_FUZZ` floor —
the agent cannot tune its own judge, and a verifier from the agent's model
family can be biased by it.

**Evidence:** a hackability audit found 61.9% of decisive LLM-generated
tests fail on the known-good patch, and 25–28.5% of sampled SWE-bench
Verified / R2E-Gym tasks accept an incorrect patch, inflating Pass@1 by
~14 points on hackable tasks (arXiv 2606.16062).

**Allowed:** in Phase 4, learned verifiers may *rank* candidates at
inference time. They never accept.

**Reopen only if:** never as the acceptance oracle while a binary under
emulation is available to ask.

## 3. All-failures feedback / "complete results by default"

**Refused:** returning every divergence (or REA-style complete results)
instead of the first.

**Breaks:** the hidden-state economy — first-divergence-only feedback
prices extraction of the recorded corpus in submissions, with the hidden
gate as overfitting backstop (ARCHITECTURE key decision 3).

**Evidence:** no controlled study shows all-failures feedback beats
first-failure; the closest head-to-head (SecTDD, arXiv 2608.09740) was
6 wins / 6 losses / 453 ties. Open SWE-RL environments use sparse binary
rewards.

**Instead:** enrich the *single* divergence (`dep_slice`, harness-rendered
arithmetic views — see roadmap).

**Reopen only if:** a controlled experiment on ReSchema's own 2C protocol
shows a solve-rate gain that survives the hidden gate, at equal E pricing.

## 4. Showing judge-strength signals to the agent

**Refused:** exposing coverage numbers, mutation kill rates, hackability
rates, hidden-gate progress, remaining budget, or hidden-pool contents to
the agent.

**Breaks:** the hidden-state economy and the #127 coaching rule (no hidden
progress, extra cases, resource estimates or new seeds exposed). Showing
judge strength turns it into a target.

**Where it belongs instead:** harness-side records (ledger/audit on disk)
and benchmark/admin reports (`tools/dogfood` reports, CI artifacts). Not
the `status` tool: it is one of the five agent-facing MCP tools, so
anything in its response is visible to the agent.

**Reopen only if:** never for hidden progress; a per-slot strength number
may be shown only once it is a fixed published property of the corpus
epoch, not a live per-submission signal.

## 5. Symbolic or bounded equivalence as the acceptance criterion

**Refused:** angr / KLEE / SymCC / alive2-style equivalence checking as
the inner-loop judge.

**Breaks:** a named v1 scope guardrail (no symbolic checks); also bounded
per-submission cost.

**Evidence:** tooling is young (codealign's exclusions, Faultless 2026
with no retrievable numbers, ARM-TV's memory-model caveats), solver time
per function is unbounded, DecompileBench rejects symbolic execution for
path explosion, and no surveyed 2026 agent loop uses it as its inner judge.

**Allowed:** concolic tools as offline *input generators* at
`corpus_build` (roadmap), and possibly as an offline 3B adjudicator for
accept→reject flips.

**Reopen only if:** v1 scope guardrails are revised, and a solver-backed
check is shown to terminate within the per-submission budget on the full
corpus.

## 6. pass@k, union-of-attempts, and pinned-seed reproducibility

**Refused:** pass@k or union-of-agents headline numbers; fixed hidden seeds
for RL reproducibility.

**Breaks:** the cost-shaped E metric (submissions are β-priced) and the
entropy policy (production validation draws fresh per call; tests pin
seeds). Both modes honor this at the agent boundary: `submit_model`
exposes no seed. (Function mode used to forward an agent-supplied `seed=`;
that gap is closed — see [roadmap.md](roadmap.md), research-survey backlog,
P0.)

**Evidence:** union-of-agents roughly doubles single-agent rates (CyberGym:
18.4% vs 7.2–9.4%), the opposite of what E measures. Fixed private tests
are memorizable across RL epochs (RLEF-style splits).

**Instead:** report E plus attempt counts; make episodes replayable by
logging each drawn seed in the ledger audit *after* the draw.

**Reopen only if:** never for the gate's seed policy.

## 7. Free-form debugger, scripting, or live decompiler sessions for the agent

**Refused:** exposing an interactive debugger, `py_eval`/`execute`-style
scripting, or a live decompiler session as agent tools.

**Breaks:** the trust model (qiling against a fresh empty rootfs; agent C
only in one-shot rootless podman) and probe accounting (`experiment`
probes are what E prices); also the five-tool table.

**Evidence:** EnIGMA's ablation shows interactive tools raise CTF solve
rates, but every surveyed MCP server gates scripting weakly (e.g. by env
flag), and none verifies agent conclusions.

**Instead:** any such capability enters as a counted probe inside the same
containment — the constraints already written for the post-1.0 decompiler
facets.

**Reopen only if:** the capability can be delivered as a counted, contained
probe with no host reachability.

## 8. Instruction-based anti-cheat

**Refused:** relying on "don't hardcode / don't special-case the tests"
prompts as a defense.

**Breaks:** nothing directly — it is simply not a defense, and the project
convention is that every gate ships with the attack it must repel.

**Evidence:** o3 reportedly kept reward-hacking after being told not to
(secondary source); Anthropic found anti-hacking instructions can
*increase* misaligned generalization, and that hacking learned in coding
RL generalizes to sabotage and alignment faking (arXiv 2511.18397).
GPT-5 cheats on 54% of ImpossibleBench's contradictory tasks.

**Instead:** structural defenses — fresh entropy, scratch-only mounts,
the flail guard, negative test per gate.

**Reopen only if:** never as a substitute; prompts may accompany structural
defenses but never replace one.

## 9. Widening v1 scope to chase literature gains

**Refused:** struct-by-value, floats/vector regs, >6 integer args,
multi-arch, packing; end-to-end asm→C model training; public-CTF corpus
slots; semantic (embedding) search over the family cache.

**Breaks:** the v1 scope guardrails (ARCHITECTURE "Scope guardrails
observed"), manifest-keyed family memory, and corpus ground truth.

**Evidence:** literature gains (Idioms' struct-type results, O-level
curricula) pull toward these, but SRE-Bench reports agents largely
insensitive to optimization level and static linking, with scale and
anti-analysis dominating difficulty. Public CTF tasks are
recall-contaminated (picoCTF flags recallable from the description alone,
arXiv 2609.01185).

**Instead:** keep curriculum pressure within integer/pointer params and
the obfuscation-tier curve.

**Reopen only if:** the v1 non-goals are formally revised in a decision
record.
