# Neural / LLM Decompilation Methods (state of the art, as of Oct 2026)

> Method note: arxiv.org, huggingface.co and export.arxiv.org were unreachable from this environment (DNS failure / proxy 403), so primary PDFs could not be read in full. Figures below come from search-engine extracts of the primary arXiv/ICLR/NDSS/ACM pages, the LLM4Decompile GitHub README (fetched directly), and secondary summaries (Emergent Mind, The Moonlight, Lacuna, ResearchGate). Each bullet says which kind of source it is. **Essentially all numbers are self-reported by the authors**; the only independent cross-checks found are (a) Decompile-Bench re-running LLM4Decompile-End and others as baselines, (b) the "Recompile More, Preserve Less" fuzzing re-evaluation of eight systems, and (c) an IoT-domain study where LLM4Decompile does poorly. Background (pre-2025) items are marked **[bg]**.

## 1. Main model lines and headline results

### Takeaway
The field has gone from supervised assembly→C translation (LLM4Decompile-End, 2024) to refining decompiler output (Ref/ReF, Idioms, CodeInverter), then to two-phase and RL-with-verifiable-reward systems (SK2Decompile, ICLR 2026; D-LiFT; RlDecompiler; AutoDecompiler, 2026). The best self-reported HumanEval-Decompile average re-executability is about 69% (SK2Decompile). The O0→O3 drop is still about 30 points. 2026 critiques show that "re-executable on shipped tests" overstates behavioral equivalence.

### Cited Findings

**LLM4Decompile family (HKPolyU; Tan et al.) [bg 2024, successors 2025]**
- End series (V1.5: direct asm→C) and Ref series (V2: refines Ghidra pseudocode). README table, HumanEval-Decompile re-executability averaged over O0–O3: 1.3B-v1.5 27.3%, 6.7B-v1.5 45.4%, 1.3B-v2 46.0%, 6.7B-v2 52.7%, 9B-v2 64.9%, 22B-v2 63.6% — [LLM4Decompile GitHub](https://github.com/albertan017/LLM4Decompile)
- HumanEval-Decompile = 164 C functions × 4 opt levels, standard C library only. The README also uses ExeBench with 2,621 real-project functions. A 6.7b-"uo" model trained without opt-level knowledge averages about 0.219 — [LLM4Decompile GitHub](https://github.com/albertan017/LLM4Decompile)
- In the original 2024 paper, the 6.7B model averages 21% and is described as "50% improvement over GPT-4". The paper also introduced the re-compilability and re-executability metrics — [arXiv 2403.05286](https://arxiv.org/pdf/2403.05286) **[bg]**
- Release timeline: V1.5 (2024-05), V2 Ref (2024-06), 9B-v2 (2024-09), decompile-ghidra-100k (2024-10), Decompile-Bench (2025-05-20), SK²Decompile (2025-10-04) — [LLM4Decompile GitHub](https://github.com/albertan017/LLM4Decompile)

**Decompile-Bench (May 2025)** — this work also gives a semi-independent re-run of baselines
- About 2M binary–source function pairs for training plus 70K for evaluation, from real-world code — [LLM4Decompile GitHub](https://github.com/albertan017/LLM4Decompile); [arXiv 2505.12668](https://arxiv.org/pdf/2505.12668)
- Average re-executability (HumanEval / MBPP): LLM4Decompile-DCBench 20.89 / 24.93; IDA 18.22 / 24.49; LLM4Decompile-End 16.22 / 20.54; GPT-4.1-mini 13.42 / 19.89; Ghidra 13.57 / 16.02. Scores fall sharply from O0 to O1–O3 — [arXiv 2505.12668 via search extract](https://arxiv.org/pdf/2505.12668)
- Note that this setting puts LLM4Decompile-End at 16.22%, below the README's 45.4% for 6.7B-v1.5. Eval protocols clearly differ, so numbers across papers are not comparable. The authors of LLM4Decompile also say another group's ExeBench numbers differ from theirs because that group used assembly-level inputs and extra information — [search extract of 2403.05286 / codeproject mirror](https://www.codeproject.com/News/64601/LLM4Decompile)
- Not to be confused with **DecompileBench** (ACL Findings 2025, arXiv 2505.11340), which scores recompilation, runtime consistency and readability on real-world functions. It reports Hex-Rays at about 58.3% average recompilation success, with LLM-based approaches stronger on code quality — [arXiv 2505.11340](https://arxiv.org/pdf/2505.11340); [ACL Anthology](https://aclanthology.org/2025.findings-acl.1194.pdf)

**SK2Decompile (Tan et al., arXiv 2509.22114; ICLR 2026)**
- Two phases. Phase 1 recovers the "skeleton": control flow and structure with generic/obfuscated identifiers. Phase 2 adds the "skin": identifier names and types. RL rewards target compilability in Phase 1 and semantic naming in Phase 2 — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114); [ICLR 2026 PDF](https://proceedings.iclr.cc/paper_files/paper/2026/file/9132583dd869acb0cafeb0ed25eb031a-Paper-Conference.pdf); [Emergent Mind](https://www.emergentmind.com/topics/sk2decompile)
- HumanEval re-executability: O0 86.59, O1 70.59, O2 61.31, O3 57.52, average **69.00**, against GPT-5-mini at 56.75 (+21.6% relative). MBPP average 59.63 against 47.23 (+26.3%). On GitHub2025, readability measured by R2I (AST-based) is 74.99 against 57.97 for Idioms (+29.4%) — [search extract of arXiv/ICLR PDF](https://arxiv.org/pdf/2509.22114); [Lacuna](https://lacuna.tiptreesystems.com/work/sk2decompile-llm-based-two-phase-binary-decompilation-from-skeleton-to-skin/wrk_c8dc32b11014b883d9afb1060384a70a) (self-reported)

**ReF Decompile (Feng et al., arXiv 2502.12221, Feb 2025)**
- Relabeling replaces jump-target addresses with labels. A function-call/tool strategy lets the model query the binary for variable types and missing data (e.g., .rodata contents) — [arXiv 2502.12221](https://arxiv.org/pdf/2502.12221); [GitHub ReF-Dec](https://github.com/AlongWY/ReF-Dec)
- HumanEval-Decompile re-executability: O0 85.37, O1 56.10, O2 51.83, O3 52.43, average **61.43** (6.7B). Readability score 3.69. An extended version reports a 62.50% average — [GitHub ReF-Dec / arXiv extract](https://github.com/AlongWY/ReF-Dec)

**Idioms (Dramko, Le Goues, Schwartz — arXiv 2502.04536; NDSS 2026)**
- Input is deterministic decompiler output. The model jointly predicts the function's source and definitions of the user-defined types (UDTs) it uses — [arXiv 2502.04536](https://arxiv.org/pdf/2502.04536); [NDSS 2026 paper](https://www.ndss-symposium.org/ndss-paper/idioms-a-simple-and-effective-framework-for-turbo-charging-local-neural-decompilation-with-well-defined-types/)
- 54.4% accuracy on ExeBench. A secondary summary gives baselines of 46.3% for LLM4Decompile and 37.5% for Nova (baselines unverified against the primary). On the new REALTYPE dataset it scores 95–205% higher than Nova and LLM4Decompile. Neighboring-function context improves UDT accuracy by up to 63%. Correctness metrics drop 55–68% once realistic UDTs are introduced — [arXiv 2502.04536](https://arxiv.org/pdf/2502.04536); [The Moonlight review](https://www.themoonlight.io/en/review/idioms-neural-decompilation-with-joint-code-and-type-prediction)

**Nova (Jiang et al., ICLR 2025; arXiv 2311.13721)**
- Assembly-pretrained LLM with hierarchical attention (intra-instruction, preceding-instruction, inter-instruction) and contrastive learning; released at 1.3B and 6.7B (lt-asset). Reports +14.84 to +21.58 points Pass@1/Pass@10 over prior binary decompilers. Nova-6.7B averages Pass@1 34.36 and Pass@10 46.91 on HumanEval-Decompile — [arXiv 2311.13721](https://arxiv.org/html/2311.13721); [ICLR 2025 PDF](https://proceedings.iclr.cc/paper_files/paper/2025/file/ef283d62b4bce30854a8d4827f331229-Paper-Conference.pdf)

**CodeInverter Suite (Liu et al., arXiv 2503.07215)**
- CIW is a prompt workflow that adds CFGs and data-mapping tables. CIM is a 1.3B/6.7B model pair trained on 8.69M samples. CIW reportedly adds about 9.16% re-executability and 15.26% re-compilability across LLMs. CIM-6.7B is said to beat models over 100× larger by about 11.03% re-executability. The repo table shows about 42.9% average on 32-bit HumanEval with CIW — [arXiv 2503.07215](https://arxiv.org/html/2503.07215); [GitHub](https://github.com/LiuPeiP-CS/CodeInverter)

**DecLLM (Wong et al., ISSTA 2025)**
- An iterative LLM repair loop over decompiler output, using static recompilation errors and dynamic runtime feedback as oracles. With GPT-3.5/GPT-4 it reaches an "upper bound of around 70%" recompilation success on C benchmarks and real binaries. The intro cites 74% for GPT-3.5, against 8% rule-based, 10% readability-LLM and 17.3% non-iterative LLM (the two figures are internally inconsistent in the extracts). CodeQL results on the recompiled code were "largely consistent" with ground-truth source — [ACM DL 10.1145/3728958](https://dl.acm.org/doi/10.1145/3728958); [ISSTA page](https://conf.researchr.org/details/issta-2025/issta-2025-papers/80/DecLLM-LLM-Augmented-Recompilable-Decompilation-for-Enabling-Programmatic-Use-of-Dec)
- Predecessor DeGPT and "Refining Decompiled C Code with LLMs" (Wong et al., 2023) **[bg]** — [arXiv 2310.06530](https://arxiv.org/pdf/2310.06530)

**ReSym (Xie et al., CCS 2024, Distinguished Paper) [bg]**
- Two fine-tuned models: VarDecoder for local variable names and types, FieldDecoder for struct field accesses. A Prolog reasoning step aggregates many LLM queries and checks their consistency to suppress hallucination — [Purdue PDF](https://www.cs.purdue.edu/homes/lintan/publications/resym-ccs24.pdf); [GitHub lt-asset/resym](https://github.com/lt-asset/resym)

**GenNm (Xu et al., NDSS 2025)**
- Generative variable-name recovery (CodeGemma-2B / CodeLlama-7B/34B) with symbol-preference optimization and caller/callee name context. Reports +5.6 to +11.4 points accuracy over prior SOTA, with gains on names unseen in training — [arXiv 2306.02546](https://arxiv.org/html/2306.02546v4); [NSF PAR](https://par.nsf.gov/servlets/purl/10585391); [AE repo](https://github.com/XZ-X/gennm-ndss-ae)

**RL-with-verifiable-reward decompilers (2025–26)**
- **D-LiFT** (Zou et al., arXiv 2506.10125). D-SCORE is gated: it first recompiles the candidate (D-helix recompiler), then checks semantic equivalence by symbolic execution, and scores readability only if both pass. That score drives RL fine-tuning and best-of-n selection at inference. Reports 55.3% more improved functions on coreutils and util-linux. A later paper criticizes symbolic execution for timing out and failing on complex memory models — [arXiv 2506.10125](https://arxiv.org/html/2506.10125v3); [CoDe-R arXiv 2604.12913](https://arxiv.org/pdf/2604.12913)
- **RlDecompiler** (ICPC '26). Ghidra static context (strings, float constants, relabeled basic blocks) goes into the prompt, followed by RL fine-tuning with a multi-faceted reward — [ACM DL](https://dl.acm.org/doi/pdf/10.1145/3794763.3794807)
- **AutoDecompiler** (arXiv 2606.16162, June 2026). Decompilation is treated as multi-turn refinement. Each turn's output is checked for validity, recompiled, re-executed and I/O-tested. Per-turn reward is a weighted sum of validity, execution, syntactic and semantic terms. "Progress-aware trajectory rewarding" plus "turn-aware advantage reweighting" encourage useful revisions and suppress regressions. Compiler errors, execution failures and failed tests are turned into stage-specific feedback. It reports average re-executability gains of +15.14 points (pseudocode input) and +14.65 points (assembly input) over the best 6.7B specialized LLMs, using substantially less training data — [arXiv 2606.16162](https://arxiv.org/html/2606.16162)
- SK2Decompile also uses RL (compilability reward and naming reward); see above.

**Frontier general LLMs as decompilers**
- GPT-4.1-mini scores 13.42% average re-executability on HumanEval in the Decompile-Bench setting — [arXiv 2505.12668](https://arxiv.org/pdf/2505.12668)
- GPT-5-mini is the strongest baseline in SK2Decompile, at 56.75% HumanEval / 47.23% MBPP average re-executability — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114)
- In an IoT-domain study, LLM4Decompile recompiled only 0.9% of outputs, below general-purpose LLMs — [arXiv 2608.06960](https://arxiv.org/pdf/2608.06960)

**Other 2025–26 entries surfaced (not deeply verified):** ALT4Decompile (abstract loop trees, [arXiv 2509.14646](https://arxiv.org/pdf/2509.14646)); CoDe-R (rationale-guided refinement, adaptive inference, [arXiv 2604.12913](https://arxiv.org/html/2604.12913)); FidelityGPT (RAG to correct decompilation distortions, [arXiv 2510.19615](https://arxiv.org/pdf/2510.19615)); PCodeTrans (pseudocode → compilable/executable, [arXiv 2603.14855](https://arxiv.org/pdf/2603.14855)); Superset Decompilation ([arXiv 2603.28002](https://arxiv.org/html/2603.28002)); Context-Guided Decompilation ([arXiv 2511.01763](https://arxiv.org/html/2511.01763v1)); constraint-guided multi-agent decompilation ([alphaXiv 2604.23940](https://www.alphaxiv.org/abs/2604.23940)); a 2026 taxonomy/literature review ([arXiv 2608.24955](https://arxiv.org/pdf/2608.24955)); and an LLM+compiler survey with a decompilation section ([arXiv 2601.02045](https://arxiv.org/pdf/2601.02045)).

### Inferences
- The specialized-model lead over frontier general LLMs is shrinking but still real on HumanEval-style benchmarks: SK2Decompile at 69.0 vs GPT-5-mini at 56.75. No published head-to-head with full frontier models (GPT-5, Claude Opus-class) on an execution-graded decompile benchmark turned up.
- The "Ref" pattern (start from Ghidra/IDA output) consistently beats pure End-to-end asm→C at equal size: 6.7B-v2 at 52.7 vs 6.7B-v1.5 at 45.4 in the same README. Each step up in the lineage (Ref → ReF tooling → SK2 two-phase RL) adds roughly 8–10 points of average re-executability.

### Gaps
- Exact per-O-level tables for LLM4Decompile V1.5/V2, AutoDecompiler's absolute numbers, and RlDecompiler's results could not be read (arXiv/HF blocked; README numbers live in images).
- No independent replication of SK2Decompile's 69% was found.
- The "17.5% → 86.2% on SynthDataBench" RL figure seen in a ResearchGate snippet could not be attributed to a specific paper and is excluded.

## 2. Iterative refinement loops: feedback types, round counts, failure modes

### Takeaway
Feedback loops follow a standard ladder: compiler/linker errors → runtime crash → I/O-test discrepancies → (rarely) a semantic-equivalence oracle. They raise recompilability far more reliably than behavioral fidelity. The key 2026 finding is that refinement can *trade fidelity for compilability*: an LLM invents fields, types, callees and guards to fill unknowns, then passes shipped tests while diverging on fuzzed inputs.

### Cited Findings
- DecLLM: an iterative loop with a recompile oracle plus runtime oracle takes recompilation from 8–17% (non-iterative baselines) to about 70–74% — [ACM DL](https://dl.acm.org/doi/10.1145/3728958)
- "Recompilation Is Not Enough: Test-Guided Decompiled-C Repair" (Huang, Liu, Chi; arXiv 2609.07201, Sept 2026). Two loops: compiler/linker diagnostics first, then a test gate (smoke checks plus official tests) that feeds discrepancy summaries into a second repair loop. On 104 Coreutils 9.5 binaries, 91 (87.5%) recompile and pass the test gate, 9 fail to recompile within the budget, and 4 recompile but fail tests. The evaluation is preliminary — [arXiv 2609.07201](https://arxiv.org/html/2609.07201v1)
- AutoDecompiler trains refinement behavior with RL. It needed explicit "regression suppression" via turn-aware advantage reweighting, which implies that naive multi-turn revision often breaks previously correct code — [arXiv 2606.16162](https://arxiv.org/html/2606.16162)
- **"When LLM Decompilers Recompile More and Preserve Less"** (Liu, Raff, Micinski; arXiv 2609.05370, Sept 2026):
  - Decompile-Diverge synthesizes a per-function driver, grows an input corpus by fuzzing the *original* function (AFL++), and compares a digest of bounded observable post-state.
  - Across 8 systems in 9 configurations, candidates that pass *every shipped test* still diverge on 4.9% of functions overall, and up to 13% for one system.
  - The shipped test suites of three corpora miss 3–45% of the divergence.
  - On 300 GitHub library functions plus 287 CVE-grounded functions, one refinement approach raises Ghidra's build rate from 75% to 90% while the matched rate *falls* from 74% to 62%. Up to one tenth of disclosed vulnerabilities lose their crash in the output.
  - Root cause: the LLM introduces fields, types, callees and guards that replace the visible unknowns traditional tools leave behind.
  - No term in current RL objectives asks whether the function agrees with the reference on inputs it was never shown.
  - Sources: [arXiv 2609.05370](https://arxiv.org/html/2609.05370v1); [The Moonlight review](https://www.themoonlight.io/en/review/when-llm-decompilers-recompile-more-and-preserve-less)
- "A Memorization Floor for LLM Refinement of Decompiled Code" (arXiv 2609.17236) argues that HumanEval-derived decompile benchmarks are contaminated (citing Riddell et al. 2024). It says no prior refinement evaluation separates memorization from repair, and proposes a memorization floor as a control — [arXiv 2609.17236](https://arxiv.org/pdf/2609.17236)
- D-LiFT's gate puts accuracy before readability (recompile, then symbolic-execution equivalence, and only then readability) and uses the same score for best-of-n selection — [arXiv 2506.10125](https://arxiv.org/html/2506.10125v3)

### Inferences
- Round count: none of the extracts gave a per-round curve. DecLLM and the Coreutils paper use a fixed repair *budget*. AutoDecompiler's regression-suppression machinery suggests returns diminish or go negative after a few turns unless regressions are explicitly penalized.
- The dominant failure mode is plausible-but-wrong code that overfits to the visible tests. This is exactly the attack class ReSchema's hidden fresh-entropy inputs and level-B differential fuzzing are built to reject. Decompile-Diverge is essentially an academic re-derivation of ReSchema's level-B oracle.

### Gaps
- No quantitative "how many rounds help" curve was retrievable for any of DecLLM, AutoDecompiler or the Coreutils repair loop.

## 3. Optimization levels, stripped binaries, cross-compiler and non-x86

### Takeaway
Every model shows a steep O0 → O1–O3 cliff (about 85% → about 52–61% for the best models). Evaluations are almost entirely x86-64 GCC on short functions. Stripping is handled indirectly via Ghidra/IDA input. ARM/MIPS and cross-compiler robustness remain largely untested for LLM decompilers.

### Cited Findings
- Per-level HumanEval re-executability:
  - SK2Decompile: 86.59 / 70.59 / 61.31 / 57.52 — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114)
  - ReF Decompile: 85.37 / 56.10 / 51.83 / 52.43 — [GitHub ReF-Dec](https://github.com/AlongWY/ReF-Dec)
- Decompile-Bench: re-executability falls sharply from O0 to O1–O3 for most tools, including IDA and Ghidra — [arXiv 2505.12668](https://arxiv.org/pdf/2505.12668)
- LLM4Decompile's model trained without opt-level knowledge ("uo") averages about 21.9%, against 45.4% for the opt-aware 6.7B-v1.5 — [GitHub](https://github.com/albertan017/LLM4Decompile)
- ExeBench ships ARM assembly, but the Tan et al. (LLM4Decompile) line fine-tunes on x86 only. angr-based traditional tools cover x86, ARM and MIPS — [search extract, arXiv 2608.06960 / related](https://arxiv.org/pdf/2608.06960)
- Benchmarks (HumanEval-Decompile, MBPP-C, ExeBench, AnghaBench) mostly offer "short functions, flat scalar signatures, and sparse input-output assertions" — [arXiv 2609.05370](https://arxiv.org/pdf/2609.05370)
- Nova's hierarchical attention and contrastive learning are motivated by making representations robust to the same source compiled at different optimization levels — [arXiv 2311.13721](https://arxiv.org/html/2311.13721)
- CodeInverter reports on 32-bit as well as 64-bit HumanEval variants — [GitHub CodeInverter](https://github.com/LiuPeiP-CS/CodeInverter)

### Inferences
- O0 is close to solved on toy benchmarks. O2/O3 (inlining, loop transforms, vectorization) is where 40% or more of functions still fail even for SOTA. ReSchema's O0→O1→O2→stripped family progression lines up with where the literature's difficulty actually lies.

### Gaps
- No LLM-decompiler paper with clang-vs-gcc cross-compiler results or ARM/MIPS execution-graded results was found in this pass.

## 4. Variable/type/struct recovery and naming, separated from correctness

### Takeaway
The clear 2025–26 trend is to *decouple* functional structure from naming and types: SK2Decompile (skeleton → skin), D-LiFT (accuracy gate before readability), and ReSym/GenNm as dedicated naming/type models. Idioms argues the opposite for types: user-defined struct types must be predicted *jointly* with code, because without UDT definitions the code cannot even be compiled correctly.

### Cited Findings
- SK2Decompile Phase 1 restores structure with generic identifiers under a compilability-oriented RL reward. Phase 2 restores names under a semantic-naming reward. The result is +29.4% R2I readability over Idioms alongside SOTA re-executability — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114); [Emergent Mind](https://www.emergentmind.com/topics/sk-decompile)
- Idioms predicts code plus UDT definitions jointly. Realistic UDTs cut correctness by 55–68%, and neighbor-function context improves UDT accuracy by up to 63% — [arXiv 2502.04536](https://arxiv.org/pdf/2502.04536)
- ReSym: separate VarDecoder and FieldDecoder, plus Prolog cross-query consistency to suppress hallucinated struct layouts — [GitHub resym](https://github.com/lt-asset/resym)
- GenNm: generative naming with caller/callee context and preference optimization; +5.6 to +11.4 points — [arXiv 2306.02546](https://arxiv.org/html/2306.02546v4)
- Invented fields and types are the documented root cause of behavioral divergence in LLM-refined output — [arXiv 2609.05370](https://arxiv.org/html/2609.05370v1)

### Inferences
- Correctness-first, idiom-later is now the consensus pipeline shape. ReSchema's existing two-pass repair directive matches SK2Decompile's decomposition, and ReSchema's judge is stricter than any reward in that line.
- Struct and type hallucination is the most dangerous "skin" error, because it silently changes memory layout. Any idiom pass must be re-verified against the same oracle, not just checked for compilation.

### Gaps
- No exact accuracy numbers were retrieved for ReSym's VarDecoder or FieldDecoder.

## 5. Training data and contamination

### Takeaway
Training data is synthetic and large (AnghaBench, ExeBench, Decompile-Bench at about 2M pairs, CodeInverter at 8.69M samples, decompile-ghidra-100k). The headline evaluation sets (HumanEval-/MBPP-derived) are contaminated and have weak tests. The newer mitigations are post-cutoff GitHub sets (GitHub2025) and memorization-floor controls.

### Cited Findings
- Decompile-Bench: about 2M train pairs and 70K eval pairs. Decompile-Bench-Eval adds GitHub repos released after 2025 "to mitigate data leakage" alongside HumanEval/MBPP binaries — [GitHub](https://github.com/albertan017/LLM4Decompile); [ResearchGate](https://www.researchgate.net/publication/391878840_Decompile-Bench_Million-Scale_Binary-Source_Function_Pairs_for_Real-World_Binary_Decompilation)
- decompile-ghidra-100k: 25K samples per opt level — [GitHub](https://github.com/albertan017/LLM4Decompile)
- CodeInverter trains on 8.69M samples — [arXiv 2503.07215](https://arxiv.org/html/2503.07215)
- HumanEval-derived decompile benchmarks are documented as contaminated (Riddell et al. 2024, as cited) — [arXiv 2609.17236](https://arxiv.org/pdf/2609.17236)
- Shipped test suites miss 3–45% of behavioral divergence — [arXiv 2609.05370](https://arxiv.org/html/2609.05370v1)
- SK2Decompile evaluates readability on GitHub2025, a post-cutoff set — [arXiv 2509.22114](https://arxiv.org/pdf/2509.22114)

### Inferences
- ReSchema's procedurally seeded corpus, hidden fresh-entropy inputs and canonicalized traces avoid both main contamination channels: memorized source and weak shipped tests. That is a selling point if ReSchema data is ever used as an external benchmark or training signal.

### Gaps
- Size and filtering details of ExeBench/AnghaBench were not re-verified in this pass. Both are background, well known from 2022-era work.

## 6. Transfer to ReSchema (without relaxing the judge)

### Takeaway
ReSchema already has the oracle the literature now says it lacks: hidden-input differential checking, which is Decompile-Diverge in effect. The ideas most worth borrowing are on the *context* and *training-signal* side: ReF-style relabeling and data-lookup context, Idioms-style UDT and neighbor context, the SK2/D-LiFT gated two-pass, and AutoDecompiler's regression-aware multi-turn reward for phase 4. The judge itself stays fixed.

### Cited Findings (anchors in ReSchema docs)
- The ReSchema roadmap already adapts four 2025–26 ideas into slot 2B: a two-pass repair directive (Skeleton→Skin), a syscall dependency slice, a call-topology digest, and a single-input probe. All of them are "coaching-context only — zero validator code" — `docs/roadmap.md` §"Research-derived slots"
- Phase 4 is "Preference Harvesting & Weight-Level RSI … Details TBD". Decompiler facets (Ghidra/Hopper) in `task_open` are parked post-1.0 as `inferred`-tier context, never verdict input — `docs/roadmap.md`

### Inferences (recommendations, ranked)
1. **Keep the judge, publicize the contrast.** The 2609.05370 numbers (4.9–13% of shipped-test-passing candidates diverge; build rate 75→90% while the match rate falls 74→62%) are external evidence for ReSchema's hidden fresh-entropy and level-B fuzzing design. They also justify the REA-backlog item "stratify hidden draws by outcome class". Do not adopt any recompile-only or shipped-test-only acceptance shortcut.
2. **ReF-style context in `task_open` (phase 2B, low cost).** Relabel jump targets (`L1:` instead of raw addresses) in the capstone disasm slice. Expose a read-only "fetch .rodata / global at address" facility so constants and tables need not be guessed. ReF attributes much of its gain to these two moves. Both are pure context with no verdict impact. They are consistent with the canonicalizer's ADDR-ordinal philosophy, but would need their own stamp if they change any canonicalized output.
3. **Idioms/ReSym-style type context for level B param specs.** Neighbor-function context improved UDT accuracy by up to 63% in Idioms. Feed caller and callee usage sites of a pointer param (offsets touched, widths) into `task_open` as `inferred` hints for the agent-declared param spec. ReSym's cross-query consistency check maps onto "require param-spec claims to be consistent across all call sites" as a *coaching* lint, not a gate.
4. **Gated two-pass, enforced by re-verification (SK2 / D-LiFT).** The existing two-pass directive should state that the idiom/skin pass is re-submitted to the *same* level-B oracle. The literature's main failure is invented fields and types during "readability" passes. For the planned refactoring agent: idiomatic rewrite proposals pass only if they are byte-for-byte behavior-equivalent to the verified world-model. That is D-LiFT's "readability scored only if accurate" in strict form.
5. **First-divergence payload as stage-specific feedback (AutoDecompiler).** AutoDecompiler's gains come from converting compiler, execution and test failures into stage-specific feedback. ReSchema's structured rejects already do this. One useful addition is a coaching hint on *regression*: when a resubmission now fails an input class a previous submission passed, report it. AutoDecompiler needed explicit regression suppression to make multi-turn work. Telemetry for this fits in the 2A ledger.
6. **Phase 4 training signal.**
   - ReSchema can emit what the RL decompiler papers lack: (accepted, rejected) model pairs on the same function, with a hidden-input verdict.
   - A preference pair should require the "chosen" side to pass the hidden gate at harvest time. Recommended reward shape: a strict pass/fail from the hidden oracle, plus a progress-aware per-turn shaping term (AutoDecompiler) that only rewards passing the *harness-drawn* inputs, never agent-visible ones.
   - Track a memorization floor (2609.17236) by also measuring unprimed-agent success on each seed, so cache/curriculum gains are not mistaken for skill.
7. **Phase 3 curriculum from the O-level cliff.** The O0→O3 drop of about 30 points and the 55–68% UDT drop suggest curriculum axes: opt level, inlining, struct-by-pointer params, then stripping. Struct-by-value and floats are v1 non-goals per AGENTS.md, so stay within int and pointer params.
8. **Do not adopt:** symbolic-execution equivalence as a gate (it times out on complex memory models per the CoDe-R critique of D-LiFT; ReSchema's differential fuzzing plus hidden draws is the chosen trust model). Also do not train an end-to-end asm→C model; the roadmap's leverage is agent context plus a verified oracle.

### Gaps
- Whether ReSchema's level-B digest {ret, mem} is stronger or weaker than Decompile-Diverge's "bounded observable post-state digest" could not be compared, because the full paper was unreadable here. Worth a direct read of arXiv 2609.05370 §method before citing it as equivalent.
