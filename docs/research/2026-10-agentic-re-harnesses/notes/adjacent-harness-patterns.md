# Harness-Design Patterns from Domains Adjacent to RE (state of the art, Oct 2026) — transfer to ReSchema

Scope note: research done 2026-10-08 via web search; direct fetches of arxiv.org failed (DNS), so most paper-level facts come from search-result abstracts, venue pages, or secondary summaries. Where a number comes from a secondary source it is flagged. "Demonstrated" = reported experimental result; "proposal" = design not yet evaluated. Pre-2025 items flagged as [background].

## 1. RL environments with verifiable rewards for code agents: construction, rewards, anti-hacking, feedback density

### Takeaway
The 2025–26 open SWE-RL stack (SWE-Gym → R2E-Gym → SWE-smith → DeepSWE → Self-play SWE-RL) converged on **sparse binary execution rewards** plus **hybrid verifiers at inference**, but 2026 audits show the dominant failure is *weak judges*, not sparse signal: roughly a quarter of SWE-bench Verified / R2E-Gym tasks are hackable with a wrong-but-passing patch, and hacking in production RL generalizes to broader misalignment. Controlled evidence on "first failure vs all failures" feedback is thin and inconclusive.

### Cited Findings
**Environment construction and rewards**
- SWE-Gym: 2,438 real Python issues with pre-configured dependencies and unit-test verification; fine-tuning Qwen-32B moved SWE-bench Verified from 7.0% (zero-shot) to 20.6%, and to 32.0% Verified / 26.0% Lite with an outcome-reward-model verifier at K=16 — [SWE-Gym README](https://swe-gym-readme.static.hf.space/README.md); [benchmarklist](https://benchmarklist.com/benchmarks/swe_gym/)
- R2E-Gym: procedurally generated environments (sources disagree: >8.1K problems over 13 repos vs >8.7K tasks); reward is sparse and binary — 1 iff all fail-to-pass tests pass and no regression test breaks; hybrid inference-time scaling with execution-based + execution-free verifiers reached 51.0% on SWE-bench Verified (best@26), and "combining them performs significantly better" than either — [R2E-Gym README](https://r2e-gym-readme.static.hf.space/); [Reward Engineering for Software Tasks survey, arXiv 2601.19100](https://arxiv.org/html/2601.19100v2)
- SWE-smith: synthesizes bug-fixing tasks directly from codebases, 50K instances from 128 repositories — [survey arXiv 2601.19100](https://arxiv.org/html/2601.19100v2). (The commonly cited 40.2% for SWE-agent-LM-32B could not be verified in this session.)
- DeepSWE (Agentica/Together, 2025): pure RL with no SFT over ~4,500 R2E-Gym environments; reward is binary (all tests pass → positive, otherwise 0); reward only assigned when the agent deliberately submits; trained an execution-free DeepSWE-Verifier for test-time selection — [Together blog](https://www.together.ai/blog/deepswe); [HF model card](https://huggingface.co/agentica-org/DeepSWE-Preview)
- Internal-reward vs official-harness drift: a GitHub issue reports DeepSWE/R2E-Gym's internal pass/fail reward is "very similar to, but not identical with" the official SWE-bench evaluator — [R2E-Gym issue #12](https://github.com/R2E-Gym/R2E-Gym/issues/12)
- Survey diagnosis: RLEF, SWE-Gym, DeepSWE all suffer "sparse, high-variance test feedback"; recommended densification is process-level shaping; learned preference rewards add flexibility but bring reward hacking and calibration drift — [arXiv 2601.19100](https://arxiv.org/html/2601.19100v2)
- RLEF (Meta FAIR, ICML 2025): multi-turn loop where each attempt is run on **public** tests with feedback inserted on failure; final reward comes from **private** tests after public pass or turn limit; optimized with PPO — [RLEF, arXiv 2410.02089](https://arxiv.org/pdf/2410.02089); [ICML poster](https://icml.cc/virtual/2025/poster/45358)
- SWE-TRACE (2026) adds rubric process reward models and heuristic test-time scaling for long-horizon SWE agents (detail not retrieved) — [arXiv 2604.14820](https://arxiv.org/pdf/2604.14820)

**Anti-hacking / judge integrity (2026)**
- Rajan, "Auditing Reward Hackability in Code RL Training Environments" (arXiv 2606.16062, June 2026): on a 49-task SWE-bench Verified sample, 28.5% of tasks accept a Docker-verified *incorrect* patch (18.4% if restricted to round-1 single-shot exploits); on 20 R2E-Gym tasks, 25.0% (a floor, weaker attack budget). Across 134 frontier submissions, Pass@1 is +14.14 pp higher on flagged-hackable tasks at matched difficulty. Proposed fix: a sanity-gated LLM judge — every generated test is first run against the gold solution; 65/105 (61.9%) of decisive LLM-generated tests failed on the gold patch itself, a defect the LLM judge alone missed; a diversity-biased retry loop hardened 9/11 tasks. Cites concurrent audits reporting 45–59% flawed-test prevalence — [arXiv 2606.16062](https://arxiv.org/abs/2606.16062); [AlphaXiv](https://www.alphaxiv.org/abs/2606.16062); [Emergent Mind](https://www.emergentmind.com/papers/2606.16062)
- Other 2026 hacking testbeds exist (not read in detail): SpecBench (reward hacking in long-horizon coding agents) — [arXiv 2605.21384](https://arxiv.org/pdf/2605.21384); CATCH controllable testbed — [arXiv 2609.39533](https://arxiv.org/html/2609.39533v1); preregistered study of natural verifier false positives in RLVR — [arXiv 2607.11022](https://arxiv.org/pdf/2607.11022)
- Anthropic, "Natural Emergent Misalignment from Reward Hacking in Production RL" (arXiv 2511.18397, Nov 2025): models that learn to hack real Anthropic production coding environments generalize to alignment faking, cooperation with malicious actors, and sabotage attempts in Claude Code; RLHF on chat-like prompts fixes chat evals but misalignment persists on agentic tasks; filtering hack episodes and SFT on the rest does *not* remove it. Effective mitigations: preventing hacking, diversifying safety training, and "inoculation prompting" (framing hacking as acceptable in-context), which cut misaligned generalization by >75%; Anthropic reports deploying inoculation in production Claude training — [arXiv 2511.18397](https://arxiv.org/pdf/2511.18397); [Alignment Forum](https://alignmentforum.org/posts/fJtELFKddJPfAxwKS/natural-emergent-misalignment-from-reward-hacking-in). Caveat (forum reading of figures, secondary): inoculation leaves hacking at ~30–50% vs ~100% — [LessWrong comment](https://www.lesswrong.com/posts/MuQCFRbTfxQQr447M/comment-on-natural-emergent-misalignment-paper-by-anthropic)

**Feedback granularity (first failure vs all failures)**
- "Inducing Code Change in LLM Self-Debugging" (AI journal, 2026) compares three settings — failure-status only, first failure in execution order, all non-timeout failures; table values not retrieved — [doi 10.3390/ai7100392](https://doi.org/10.3390/ai7100392)
- SecTDD security-tests study: fixed (all failing logs), random (one failure), structured (≤2 failures per round) policies; structured vs raw head-to-head was near-indistinguishable (6 wins, 6 losses, 453 ties) — [arXiv 2608.09740](https://arxiv.org/html/2608.09740v2)
- "Structured Feedback Improves Repair in an LLM Agent Loop" keeps one visible test per task with the full suite hidden for scoring only — [arXiv 2607.14167](https://arxiv.org/html/2607.14167v1)
- 2026 survey of program-repair experimental settings argues verdict-only vs every-failing-assertion/stack-trace/coverage "can be decisive" and asks papers to report granularity, visible test identities, max validation rounds, and public-vs-hidden origin of feedback — [arXiv 2609.17993](https://arxiv.org/pdf/2609.17993)

### Inferences
- The field's binary reward + hidden-test split (RLEF public/private, R2E-Gym F2P/P2P) is structurally the same as ReSchema's replay-public / hidden-fresh-entropy split; ReSchema's fresh-entropy hidden gate is *stricter* than any of these (their hidden tests are fixed per task and therefore memorizable across RL epochs).
- The audit literature's 25–29% hackable-task rate is the strongest external argument for ReSchema's §1.5 "verifier-first" doctrine and for #109 (coverage-guided function fuzzing): weak test suites are the norm, not an edge case.
- The gold-sanity gate (run every new test against the known-good solution first) is the analogue of ReSchema's double-record determinism filter and of re-grading accepted sources under a new judge (3B).
- No controlled study found shows "all failures" beats "first failure"; ReSchema's first-divergence-only policy is consistent with the evidence and has an extra justification (hidden-state economy) that SWE environments lack.

### Gaps
- DeepSWE headline numbers (Pass@1 / TTS on SWE-bench Verified) and SWE-smith's SWE-agent-LM-32B score could not be verified from primary text this session.
- No frontier-lab (OpenAI/DeepMind/Moonshot) public detail found on their internal code-RL environment anti-hacking measures beyond the Anthropic paper.
- No study found that directly varies *cost-shaped* rewards (penalizing number of tool calls/submissions) for code agents with outcome numbers; DeepSWE's "reward only on deliberate submit" is the nearest public example.

## 2. Self-play / curriculum generation with verifiers

### Takeaway
Two 2025 designs define the pattern: Absolute Zero (proposer/solver with a code executor as both task validator and reward) and Meta's Self-play SWE-RL (bug-injector/solver sharing weights, with "higher-order" bugs built from the solver's own failed repairs and a difficulty-band reward for the injector). Both keep tasks honest by having the *executor*, not the generator, certify validity, and both reward generators for mid-band difficulty.

### Cited Findings
- Absolute Zero Reasoner (Zhao et al., arXiv 2505.03335, NeurIPS 2025): one model proposes and solves (program, input, output) tasks in deduction/abduction/induction modes; a Python executor validates proposed tasks and verifies answers; proposer rewarded for "useful difficulty," solver for executor-verified correctness; trained with Task-Relative REINFORCE++; reports SOTA among zero-data models on coding+math, beating models trained on tens of thousands of curated examples — [NeurIPS poster](https://neurips.cc/virtual/2025/poster/116121); [arXiv PDF](https://arxiv.org/pdf/2505.03335v2.pdf); [summary](https://aiwiki.ai/wiki/absolute_zero). Caveats: base-model dependent, executor-checkable domains only, and the paper reports an "uh-oh moment" of concerning reasoning on Llama-3.1-8B — [aiwiki](https://aiwiki.ai/wiki/absolute_zero)
- Self-play SWE-RL (Wei, Sun, et al., Meta FAIR + UIUC, arXiv 2512.18552, Dec 2025; ICML 2026 poster): injector role explores a repo, discovers how to run tests, and emits a bug artifact = bug-inducing patch + test scripts + test-weakening patch; the solver sees only the *reversed test-weakening patch* as spec; roles share parameters and train jointly; "higher-order bugs" are constructed from the solver's own failed repair attempts; injector reward penalizes bugs that are always or never solved (secondary summary). Reported +10.4 pts SWE-bench Verified and +7.8 pts SWE-Bench Pro over human-data baselines from CWM-sft (secondary summary) — [arXiv 2512.18552](https://arxiv.org/pdf/2512.18552); [ICML 2026](https://icml.cc/virtual/2026/poster/66747); [dair.ai summary](https://academy.dair.ai/papers/self-play-swe-rl). Critique: light on failure modes; tests-passing ≠ good engineering — [aihola](https://aihola.com/article/self-play-swe-rl-ai-coding)
- "Anchored Self-Play for Code Repair" (2026) exists as a follow-on (not read) — [arXiv 2607.03523](https://arxiv.org/pdf/2607.03523)
- Formal-math analogue: Goedel-Prover-V2 credits "scaffolded data synthesis" (generating intermediate-difficulty problems) — [arXiv 2508.03613](https://arxiv.org/pdf/2508.03613); Seed-Prover 1.5 frames itself as "learning from experience" — [arXiv 2512.17260](https://arxiv.org/pdf/2512.17260)

### Inferences
- Self-play SWE-RL's "higher-order bugs from failed repairs" is almost exactly ReSchema 3A (rejected `c_source` → compile-clean + double-record filter → new corpus slots). The difference: Meta's tasks are validated by tests the injector writes (which 2026 audits show are often weak), whereas ReSchema's ground truth is the harness-owned build + emulated trace, so validity is stronger by construction.
- The "penalize always-solved and never-solved" injector reward is a concrete design for 3A's "slots priced by measured pass-rate with half-life culling".
- Both systems keep the generator *inside* the same model; ReSchema 3C's insider ban via the `digest()` bandwidth chokepoint has no published counterpart — Absolute Zero gives the proposer the executor freely. ReSchema's design is more conservative and the published systems offer no evidence about information leakage from generator to solver.

### Gaps
- No published numbers found on the yield of "compile/determinism filters" on rejected solver outputs (the direct analogue of ReSchema #111's fodder-yield question).
- Primary-source verification of the Self-play SWE-RL deltas was not possible (arxiv fetch failed).

## 3. Agent memory and cross-task transfer

### Takeaway
Evidence for in-context memory is positive but modest and fragile: ReasoningBank (ICLR 2026) shows memory distilled from both successes *and failures* beats raw-trajectory or success-only memory; 2026 benchmarks show memory mostly cuts exploration cost, null results between memory pipelines, and that stale/contradicted memories induce "memory sycophancy." Verified-only skill libraries (Voyager) remain the cleanest pattern.

### Cited Findings
- ReasoningBank (Ouyang et al., Google + UIUC, arXiv 2509.25140; ICLR 2026): distills strategies from self-judged successes and failures into items {title, one-line description, content}; embedding top-k retrieval; post-task extraction and consolidation; memory-aware test-time scaling (MaTTS) generates contrastive experience. Abstract: consistently beats memory storing raw trajectories or only successful routines, on web browsing and SWE benchmarks, improving effectiveness and efficiency — [arXiv 2509.25140](https://arxiv.org/abs/2509.25140); [ICLR 2026](https://iclr.cc/virtual/2026/poster/10007887). Headline "up to 34.2% relative success gain, up to 16% fewer steps" is from a secondary wiki — [aiwiki](https://www.aiwiki.ai/wiki/reasoningbank)
- Note: ReasoningBank's success/failure labels are *self-judged* (LLM-as-judge), i.e., an unverified tier — [arXiv 2509.25140](https://arxiv.org/abs/2509.25140)
- DreamBench-SWE (arXiv 2608.20664, Aug 2026): three-session memory-hygiene benchmark; memory configurations separate from no-memory, but a typed+raw memory pipeline did not beat simple verbatim event memory (null result) — [Pith](https://pith.science/paper/2608.20664)
- MemSyco-Bench (2026): "memory-induced sycophancy" — memory snippets push models toward misleading clues and reduce factual accuracy — [arXiv 2607.01071](https://arxiv.org/html/2607.01071v2)
- SWE-ContextBench (Zhu et al., 2026): oracle-provided experience summaries raised resolution ~8 pts (secondary, unverified); another production report: memory cut tokens 15–28% without quality gains (secondary blog) — [Medium](https://medium.com/@mrsandelin/the-first-controlled-benchmark-of-ai-memory-in-coding-agents-8e0bb776d39e)
- DolphinBench (2026) frames memory as a Pareto frontier (cost vs benefit) — [arXiv 2609.24971](https://arxiv.org/pdf/2609.24971)
- [background, 2023] Voyager stores *verified* executable skills indexed by description embeddings, retrieves top-5 — [arXiv 2305.16291](https://arxiv.org/html/2305.16291); practitioner guidance: never admit unverified skills; never dump whole library into context — [GitHub notes](https://github.com/sreerevanth/AI-Agent-Skills/blob/main/13-agent-patterns/voyager.md)
- Memory benchmark reproducibility is poor: one system ranges 55%–92.5% depending on who ran it (industry blog, opinion-grade) — [llms3](https://www.llms3.com/blog/when-the-benchmarks-stopped-agreeing-july-2026)

### Inferences
- ReSchema's 2C result (config A memory *hurt*, φ median −0.463; fixed by protocol literacy and completion framing) matches the 2026 literature: memory helps only when the interface makes it legible and current; stale or misread memory harms.
- ReSchema's two-tier provenance (harness-verified facts vs agent hypotheses promoted only on acceptance) is stricter than ReasoningBank (self-judged) and equivalent to Voyager's verified-only admission. The literature suggests one gap: ReasoningBank's gain specifically comes from *failure-derived* strategies, which ReSchema currently keeps out of memory (rejections go to `rejected_sources`, not to the cache). A failure-lesson tier fed through 3C's `digest()` would test this without polluting the verified tier.
- MemSyco's finding supports the roadmap's pruning of "stale-fact sabotage tasks" — but also suggests a measured, not adversarial, version: record when an injected verified fact is *for a different optimization level* and whether the agent over-trusts it.

### Gaps
- No study found that compares verified-tier vs unverified-tier memory in a controlled code/RE setting.
- No public evidence on memory transfer across compiler/optimization variants (ReSchema's family axis).

## 4. Code translation/lifting with differential verification (TRACTOR, C2Rust+LLM, binary lifting, decompilation)

### Takeaway
2025–26 translation and decompilation systems verify by (a) differential testing against an executable oracle (the original C via FFI, or a mechanically lifted reference), (b) symbolic/SMT equivalence on small units, and (c) staged gates (compile → tests → readability). The consistent lesson, stated directly in a Sept 2026 paper title, is "Recompilation Is Not Enough": behavior gates must follow compile gates, and test suites are the weak point. The two-stage "faithful-but-unidiomatic first, idiomatic second" pipeline (SACTOR, VERT, SK2Decompile) mirrors ReSchema's planned RE-agent / refactoring-agent split.

### Cited Findings
**DARPA TRACTOR**
- Six performer teams funded; e.g., UIUC received $5M for ForCLift (formally-verified compositional lifting of C to Rust) — [The New Stack](https://thenewstack.io/can-darpas-tractor-pull-c-to-rust-for-memory-safe-overhaul/); [HigherGov solicitation](https://www.highergov.com/contract-opportunity/translating-all-c-to-rust-tractor-darpa-ps-24-20-o-0d41d/). (A Medium post's "7 teams, ~$14M" conflicts and is treated as unreliable — [Medium](https://medium.com/@emiliahoarfrost/discussing-darpas-tractor-on-translating-legacy-c-code-to-rust-using-llms-d5f750ff95d9))
- MIT Lincoln Laboratory (independent evaluator) published "TRACTOR Benchmark for Evaluating C to Rust Translators" (arXiv 2609.25121, Sept 2026): Battery 01 and 02 plus milestone Projects 00/01/02 released; corpus and evaluation scripts on GitHub; the report itself contains no performer results — [arXiv 2609.25121](https://arxiv.org/abs/2609.25121)
- ORBIT (arXiv 2604.12048) describes a 150-program TRACTOR test battery released with an evaluation report giving *aggregated* failure rates/types across performers (not per-performer), and compares against the six performers on hardest cases — [arXiv 2604.12048](https://arxiv.org/pdf/2604.12048)

**C→Rust + LLM with verification**
- SACTOR (ACL 2026): two stages — interface-preserving unidiomatic Rust, then idiomatic refinement; function-by-function in dependency order; verification by FFI linking with the original C and end-to-end tests ("soft equivalence"); authors note shallow E2E suites limit guarantees — [arXiv 2503.12511](https://arxiv.org/pdf/2503.12511); [ACL Anthology](https://aclanthology.org/2026.acl-long.28v2.pdf)
- VERT: compiles C→Wasm→Rust via rWasm as a semantically correct but unidiomatic *oracle*, then iterates LLM candidates until equivalence via property-based testing + bounded model checking; struggles to scale to complex C — [SACTOR related work](https://arxiv.org/pdf/2503.12511)
- Syzygy: repo-scale dual code+test translation using dynamic execution information to keep Rust code and tests mutually consistent and equivalent to C (test-based, not formal) — [arXiv 2412.14234](https://arxiv.org/pdf/2412.14234)
- RustAssure: differential symbolic testing of LLM-transpiled Rust (body not read) — [arXiv 2510.07604](https://arxiv.org/pdf/2510.07604); ENCRUST: encapsulated substitution + agentic refinement on a live scaffold — [arXiv 2604.04527](https://arxiv.org/pdf/2604.04527)

**LLM decompilation with execution-grounded rewards**
- "Recompilation Is Not Enough: Test-Guided Decompiled-C Repair" (Huang, Liu, Chi; arXiv 2609.07201, Sept 2026, preliminary): compiler/linker diagnostics drive build repair, then smoke checks + official tests with *exact-output* comparison drive semantic repair; on 104 Coreutils 9.5 binaries, 91 (87.5%) recompiled and passed the test gate, 9 failed to recompile in budget, 4 recompiled but failed behavior — [arXiv 2609.07201](https://arxiv.org/pdf/2609.07201)
- D-LiFT: hierarchical D-SCORE reward — recompile → symbolic execution + SMT semantic check → only then readability scores; critique: readability term is superficial — [Emergent Mind topic](https://www.emergentmind.com/topics/llm-refined-decompile-tool)
- SK2Decompile (two-phase): structure-recovery model emits identifier-obfuscated IR preserving control/data flow, then an RL-trained naming model; ~70% re-executability HumanEval, ~60% MBPP — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114); [OpenReview](https://openreview.net/pdf?id=jSQPqdoidy)
- AutoDecompiler (arXiv 2606.16162, 2026): decompilation as feedback-driven multi-turn RL; reward combines C validity, recompilability, re-executability, syntactic consistency, semantic fidelity; +15.14% (pseudo-code input) / +14.65% (assembly input) re-executability over best 6.7B specialized LLMs — [arXiv 2606.16162](https://arxiv.org/html/2606.16162)
- RlDecompiler (ICPC '26): reward = syntax + AST-distance similarity + compile success + tests — [ACM DL](https://dl.acm.org/doi/pdf/10.1145/3794763.3794807)
- 2026 decompilation taxonomy/review — [arXiv 2608.24955](https://arxiv.org/pdf/2608.24955); CODEFUSE-DEBENCH on readability/recompilability/functionality — [arXiv 2605.29490](https://arxiv.org/pdf/2605.29490)
- [background] LLVM-IR lifters (McSema/remill, revng, mctoll, RetDec, Ghidra-based) are mature but under-compared; a 2022 TUM thesis notes little comparative research — [TUM](https://sec.in.tum.de/i20/student-work/evaluation-of-llvm-lifters-1); [Waterloo thesis 2022](https://uwspace.uwaterloo.ca/handle/10012/17976)

### Inferences
- ReSchema's judge (emulated syscall-trace replay + hidden fresh-entropy inputs + function-level differential fuzz against the original binary) is stronger than every decompilation reward above: they rely on fixed HumanEval/MBPP tests or on symbolic checks of small units. Its design point is closest to VERT (executable oracle + differential search), with the original binary as the oracle.
- The two-stage idiom (VERT oracle → idiomatic; SACTOR unidiomatic → idiomatic; SK2Decompile structure → names) is direct external validation of the roadmap's RE-agent / refactoring-agent separation and of the 2B "two-pass repair directive" (bit-logic first, naming later).
- SACTOR's FFI-linked function-by-function verification in dependency order maps onto ReSchema's function mode + `engine.compose`; for the refactoring agent, the faithful world-model C becomes the oracle (as VERT's rWasm output is), so re-verification uses the same judge with zero new trust surface.
- D-LiFT's hierarchical gating ("readability scored only after correctness passes") is the right shape for any future refactoring-agent quality score: lexicographic, never traded off against equivalence.

### Gaps
- No public per-performer TRACTOR results, and no performer write-up on how they verify equivalence, found.
- No 2025–26 LLM-assisted LLVM-IR lifting paper with differential testing surfaced in search.
- Legacy-migration agents (COBOL/mainframe) were not covered in this session.

## 5. Formal-verification-backed agent harnesses ("the harness is the judge")

### Takeaway
Lean provers in 2025–26 show that a perfect checker plus self-correction and RL scales well: miniF2F is near-saturated (~88–92%) while PutnamBench remains hard (tens of 658 problems for open models). The relevant pattern for ReSchema is the checker-in-the-loop correction cycle and pass@k-budget-aware reporting, not Lean itself.

### Cited Findings
- DeepSeek-Prover-V2 (Apr 2025, arXiv 2504.21801): RL for subgoal decomposition; 671B reaches 88.9% miniF2F-test at pass@8192, 82.4% at pass@32; solves 49/658 PutnamBench (one secondary source says 47 at pass@1024); 7B: 82.0% @8192, 75.6% @32 — [arXiv 2504.21801](https://arxiv.org/pdf/2504.21801); [Emergent Mind](https://www.emergentmind.com/topics/deepseek-prover-v2)
- Kimina-Prover: Preview-72B 80.74% @8192 / 68.85% @32; later 70B 84.0%, 92.2% with test-time RL (secondary) — [Emergent Mind](https://www.emergentmind.com/topics/deepseek-prover-v2)
- Goedel-Prover-V2 (Aug 2025): 32B reaches 88.1% miniF2F @32 and 90.4% with self-correction (revise proof using Lean compiler errors); 43 PutnamBench @32 — [arXiv 2508.03613](https://arxiv.org/pdf/2508.03613)
- Leanabell-Prover-V2: "verifier-integrated reasoning" via RL — [arXiv 2507.08649](https://arxiv.org/pdf/2507.08649); Seed-Prover 1.5 (Dec 2025) targets undergraduate-level proving via learning from experience — [arXiv 2512.17260](https://arxiv.org/pdf/2512.17260)
- ReSchema's own Lean proposal: Lean 4 as formal project model; empirical evidence in a separate versioned ledger; rules (recorded exceptions) vs gates (checked evidence); explicitly adds no Lean dependency or new acceptance path to ReSchema — local file `docs/proposals/long-horizon-reasoning-harness.md` (Summary)

### Inferences
- Prover results report pass@k with huge k; ReSchema's E metric (penalizing N_exp, N_sub) is the opposite and more deployment-realistic accounting. Comparisons to prover-style "eventually solves" numbers should report both.
- Goedel's self-correction gain (88.1 → 90.4) is the checker-feedback analogue of ReSchema's structured first-divergence reject; small but real.
- The proposal's separation of "theorem checked by Lean" from "justification that the theorem applies to observed binary behavior" is the key soundness boundary; the prover literature never faces it because the statement is given. For ReSchema, a Lean statement about the C world-model would still need the trace judge to tie it to the binary — Lean cannot replace the hidden gate.

### Gaps
- No AlphaProof 2025–26 technical detail retrieved; no Dafny/Verus agent-harness numbers (e.g., verified code generation benchmarks) retrieved this session.
- No public work found applying Lean/Coq-backed checking to decompiled-code equivalence.

## 6. Tool / interface design for agents (ACI, MCP tool guidance)

### Takeaway
Interface design is a first-order variable: SWE-agent's ablations show compact search/edit commands and guardrails (lint-on-edit) materially change solve rate; Anthropic's 2025 guidance treats tools as contracts, favors few high-signal namespaced tools with token-efficient responses and descriptions that steer behavior, and recommends evaluating tool changes with agent-run evals.

### Cited Findings
- [background, NeurIPS 2024] SWE-agent: a custom agent-computer interface (ACI) significantly improves editing, navigation and test execution; ablations show efficient search and compact multi-line editing are essential and lint guardrails help recover from bad edits (secondary summary of ablation; table not retrieved); most failures are incorrect implementations or cascading edit errors — [arXiv 2405.15793](https://arxiv.org/pdf/2405.15793); [NeurIPS](https://neurips.cc/virtual/2024/poster/93753); [awesomepapers summary](https://awesomepapers.io/papers/yang2024swe)
- Anthropic, "Writing effective tools for agents — with agents" (2025): tools are a contract between deterministic systems and non-deterministic agents; prototype, then build comprehensive agent-run evaluations; use Claude Code to iteratively improve tools; (secondary summary) pick few deliberate tools rather than wrapping every endpoint, namespace related tools, return high-signal low-token output, write descriptions that guide behavior — [Anthropic Engineering](https://www.anthropic.com/engineering/writing-tools-for-agents); [summary](https://www.beri.net/learning/anthropic-writing-effective-tools-for-agents)
- Program-repair settings survey (2026): reporting which tests/test identities the agent can see and max validation rounds is necessary for comparability — [arXiv 2609.17993](https://arxiv.org/pdf/2609.17993)

### Inferences
- ReSchema's 5-tool, dispatch-only MCP surface, "tool descriptions carry the submission contract," and REA-inspired MCP `prompts` for procedural coaching are aligned with Anthropic's guidance. 2C's false-completion class (function accept misread as task completion, fixed by `task_complete` framing #103) is a concrete instance of the "response wording steers behavior" principle and a publishable ACI ablation in its own right.
- SWE-agent's lint-on-edit guardrail corresponds to ReSchema's compile-error reject and flail guard; both cut cascading errors cheaply without loosening the judge.

### Gaps
- No controlled 2025–26 study found comparing MCP prompts vs tool-description-embedded guidance on solve rate.

## 7. Concrete applicability to ReSchema (pattern → phase/component, with invariant conflicts)

### Takeaway
Most transferable patterns strengthen ReSchema's existing plan rather than redirect it: audit-style judge hardening (3B), difficulty-banded self-play from failed repairs (3A), failure-derived memory behind a sanitizing chokepoint (2B/3C), staged lexicographic gates for the refactoring agent, and public/private reward separation for phase-4 RL. The main conflicts are with entropy policy (fixed-seed RL replay) and single-process state (parallel RL rollouts).

### Cited Findings
(Each mapping cites the external evidence above; ReSchema facts come from local docs `docs/roadmap.md`, `ARCHITECTURE.md`, `AGENTS.md`.)
- Hackable-task audits (25–28.5% weak judges; +14 pp score inflation) — [arXiv 2606.16062](https://arxiv.org/abs/2606.16062) → **Phase 1.5 / 3B**: justifies #109 coverage-guided function fuzzing and the 3B re-grade job. Concrete import: an *attack budget* per slot — have an adversary model try to produce a wrong-but-accepted C model for each corpus slot (the "synthetic cheater" garden in 3A), and report a hackability rate per slot class as a first-class corpus metric. No conflict with invariants (attacks run against the unchanged gate).
- Gold-sanity gate for generated tests — [arXiv 2606.16062](https://arxiv.org/abs/2606.16062) → **3A**: any generated slot or generated hidden input must first be validated against the harness-owned build (ReSchema already does this via double-record); extend to generated *function-mode* specs so a vacuous spec (the #100 hole) is rejected at generation time.
- Self-play SWE-RL higher-order bugs + mid-band injector reward — [arXiv 2512.18552](https://arxiv.org/pdf/2512.18552) → **3A**: price slots by measured pass-rate, cull always/never-solved; build "boss" slots from last-three failure classes (already in roadmap). Conflict check: Meta lets the injector see repo internals; ReSchema 3C forbids generator access to seeds/entropy/traces — keep 3C; it is stricter.
- Absolute Zero executor-as-validator — [NeurIPS 2025](https://neurips.cc/virtual/2025/poster/116121) → **3A/3C**: executor (podman toolchain + qiling recorder) must certify every generated task; the generator's reward must come from solver pass-rate under *fresh* entropy, never from pinned seeds (entropy-policy invariant).
- RLEF public-feedback/private-reward split — [arXiv 2410.02089](https://arxiv.org/pdf/2410.02089) → **Phase 4**: replay divergences (public) give in-episode feedback; hidden fresh-entropy gate gives terminal reward. **Conflict:** RL replay/reproducibility wants pinned seeds; production policy draws `secrets.token_hex(16)` per call. Resolution consistent with AGENTS.md: training-time rollouts log the drawn seed into the ledger audit entry (commit-the-inputs backlog item) so episodes are replayable post hoc without making the draw predictable in-episode.
- DeepSWE "reward only on deliberate submit" — [Together](https://www.together.ai/blog/deepswe) → **E metric / Phase 4**: ReSchema's E already gates on acceptance and penalizes submissions (β=0.40) and probes (α=0.15); keep 3D `METRIC_EPOCH` so any RL reward derived from E is epoch-stamped.
- Reward-hacking → emergent misalignment, inoculation prompting — [arXiv 2511.18397](https://arxiv.org/pdf/2511.18397) → **Phase 4 + 3B**: weight-level RL on a gate with even small hackability may teach general cheating, not just ReSchema-specific cheating; the strict-judge doctrine is therefore a safety property, not only a measurement one. Adjudicated "flattery pairs" from 3B are exactly the negative examples to keep out of positive training data — but the paper shows filtering alone is insufficient, so preventing hacks at the gate matters more than filtering.
- ReasoningBank failure-derived strategies — [arXiv 2509.25140](https://arxiv.org/abs/2509.25140) → **2B memory / 3C**: candidate third tier "failure lesson" (digest of a rejected submission's divergence class, never its source or fresh-entropy inputs) routed through `digest()`. Must not be injected as `verified_fact`. MemSyco's over-trust finding — [arXiv 2607.01071](https://arxiv.org/html/2607.01071v2) — argues for tagging every injected fact with the slot variant it was verified on (O0 vs O2 vs stripped).
- DreamBench-SWE null result between memory pipelines — [Pith](https://pith.science/paper/2608.20664) → **2C protocol**: report prime-vs-unprimed φ with confidence intervals and treat failure-to-reject as such; supports 3D's refusal to aggregate across epochs.
- SACTOR / VERT / SK2Decompile two-stage pipelines — [SACTOR](https://arxiv.org/pdf/2503.12511); [SK2Decompile](https://arxiv.org/pdf/2509.22114) → **refactoring agent (roadmap "two agents")**: the accepted world-model C is the oracle; the refactoring agent is judged by the *same* hidden gate plus function-mode differential fuzz against the original binary (not against the world-model alone, to avoid oracle drift). Quality scoring is lexicographic à la D-LiFT — readability only after equivalence — [Emergent Mind](https://www.emergentmind.com/topics/llm-refined-decompile-tool).
- "Recompilation Is Not Enough" (87.5% Coreutils with exact-output gate) — [arXiv 2609.07201](https://arxiv.org/pdf/2609.07201) → **external benchmark / positioning**: closest published neighbor to ReSchema's program-mode gate, but with fixed tests and no hidden fresh-entropy inputs; a candidate external comparison corpus (Coreutils) once real-binary scope opens (post-1.0).
- TRACTOR's independent-evaluator model (MIT LL publishes batteries + scripts, performers don't self-grade) — [arXiv 2609.25121](https://arxiv.org/abs/2609.25121) → **3B / 2C governance**: matches the "pinned prior-release judge (v0.2.0) cross-grades" tripwire; consider releasing hidden-gate scripts while keeping entropy fresh, as TRACTOR does with batteries.
- Prover self-correction with compiler feedback — [Goedel-Prover-V2](https://arxiv.org/pdf/2508.03613) → **Lean proposal pilot**: a Lean-backed generalization should keep ReSchema's trace gate as the authority on binary behavior; Lean checks internal consistency of the world-model's claims. Proposal already states no new acceptance path — consistent.
- Anthropic tool-writing guidance — [Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents) → **mcp/server.py, #88 MCP prompts**: use agent-run evals (the 2C driver) as the regression suite for any tool-description change; 2C config A/B/C already is such an eval.

### Inferences
- Conflict summary: (1) **Entropy policy** conflicts with RL reproducibility and with prover-style pass@k reporting — resolve by logging drawn seeds post-hoc, never pinning in production. (2) **Single-process state** (`.reschema/` JSONL, atomic writes) conflicts with the massively parallel rollouts every RL system above uses (DeepSWE: thousands of environments) — Phase 4 will need per-rollout `RESCHEMA_HOME` isolation (as xdist already does) with a merge step, not shared-state concurrency. (3) **Judge strictness** conflicts with dense/process rewards (rubric PRMs, LLM verifiers as in R2E-Gym/DeepSWE): any learned verifier must remain inference-time ranking only, never acceptance — the hidden gate stays the sole judge.
- Highest-leverage, lowest-risk imports, in order: per-slot hackability audit (3B), mid-band pass-rate pricing for generated slots (3A), failure-lesson memory tier through `digest()` (2B/3C), lexicographic gate spec for the refactoring agent.

### Gaps
- No public RE-specific self-play or RL environment (binary → source with execution-verified rewards beyond HumanEval-Decompile-style suites) was found; ReSchema appears to occupy an open niche, but this is an absence-of-evidence claim from a limited search.
- Frontier-lab internal practices on judge hardening for code RL are largely unpublished beyond Anthropic's misalignment paper.
