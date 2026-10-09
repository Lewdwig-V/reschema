# Agentic Reverse-Engineering Harnesses and Tooling (state as of Oct 2026)

Method note: research done 2026-10-08. `arxiv.org`, `googleprojectzero.blogspot.com` and `jtsylve.blog` could not be fetched from this environment (DNS failure), so claims about those papers and posts come from search-result snippets and secondary summaries. GitHub READMEs were fetched directly. Several 2026 model names in benchmark results (e.g. "GPT-5.6-Sol", "Claude-Fable-5.1", "GPT-6 Astra") appear only in search snippets. They are reported exactly as found and not independently confirmed. Items before 2025 are marked **[background]**.

## 1. MCP servers and plugins that connect agents to disassemblers and decompilers

### Takeaway
By late 2026 the ecosystem has shifted from GUI plugins that expose a few dozen thin primitives (the 2025 generation) to **headless, multi-database supervisor/worker servers**. These add batch APIs, pagination, snapshots or undo, and MCP safety annotations. The two main IDA and Ghidra efforts now steer users toward headless backends (idalib, pyghidra), and vendors ship their own servers (Hex-Rays official IDA MCP; an integrated Binary Ninja MCP server). None of the servers checks whether the agent's conclusions are true. At most they record **provenance** (old/new values, xref context, "evidence and limitations" bundles). Every tool here is a context provider, not a judge.

### Cited Findings
**IDA Pro**
- mrexodia/ida-pro-mcp (MIT): the README now points users to the **official Hex-Rays IDA MCP Server** and marks its own GUI plugin deprecated in favour of `idalib-mcp`. It requires IDA Pro 8.3+ (9 recommended); IDA Free is not supported — [GitHub](https://github.com/mrexodia/ida-pro-mcp)
- Its tool surface covers core (`lookup_funcs`, `int_convert`, `list_funcs`, `decompile`, `disasm`, `xrefs_to`, `xrefs_to_field`, `callees`), modification (`set_comments`, `patch_asm`, `declare_type`, `define_func`), memory (`get_bytes`, `get_int`, `get_string`), stack frames, structs, search (`find_regex`, `find_bytes`, `find_insns`), `basic_blocks`, types (`set_type`, `infer_types`), `callgraph`, `py_eval`, read-only `ida://` resources, and a **debugger** group (start, step, breakpoints, registers, memory read/write) — [GitHub](https://github.com/mrexodia/ida-pro-mcp)
- Design choices in the same README:
  - Debugger tools are **hidden by default** and enabled with `?ext=dbg`. `py_eval` and the patch tools are not gated.
  - `idalib-mcp` is a supervisor with one worker process per database. Workers are detached and expire after an idle TTL (default 1 h). Every call **must name an explicit `database` session ID**, so there is no implicit current DB.
  - APIs are batch-first, with per-item errors `[{..., error: null|string}]`.
  - Cursor pagination defaults to 1000 items (max 10000) "to prevent token overflow".
  - The strings cache uses MD5 invalidation.

  — [GitHub](https://github.com/mrexodia/ida-pro-mcp)
- Anti-hallucination prompt guidance in that README:
  - LLMs "hallucinate"; integer and byte conversions are especially error-prone. The recommended prompt says "**NEVER convert number bases yourself**" and to use `int_convert` instead.
  - Avoid asking LLMs to solve heavily obfuscated code.
  - Use Lumina/FLIRT to identify library code.
  - Forbid brute forcing, and require a `report.md` of findings.

  — [GitHub](https://github.com/mrexodia/ida-pro-mcp)

**re-mcp (formerly ida-mcp), IDA + Ghidra**
- jtsylve/re-mcp: in **v3.0 (blog post dated 2026-05-04)** it added a full Ghidra backend next to IDA, behind a shared tool interface. It needs IDA Pro 9+ (licensed) or Ghidra 12+ with JDK 21+, and Python 3.12+ — [GitHub](https://github.com/jtsylve/re-mcp); [blog listing](https://jtsylve.blog/post/2026/05/04/ida-mcp-becomes-re-mcp) (blog itself unreachable; the date comes from the URL)
- Both backends are standalone headless servers, not plugins (idalib and pyghidra), using a supervisor/worker design with multiple open databases. Its tools span about 35 categories. Only common tools are listed by default; the rest are reached through **meta-tools**: `search_tools`, `get_schema`, `call`, `execute` (sandboxed Python that chains calls) and `batch` (up to 50 operations) — [GitHub](https://github.com/jtsylve/re-mcp)
- State handling and safety:
  - Database **snapshots (take, list, restore) and undo/redo**.
  - Mutation tools **return old and new values** for change tracking.
  - Every tool carries **MCP annotations** (read-only, destructive, idempotent, open-world).
  - Arbitrary IDAPython `run_script` is off unless `IDA_MCP_ALLOW_SCRIPTS` is set.
  - `execute`, `batch` and tool search can each be disabled.
  - The persistent proxy daemon uses a bearer token on 127.0.0.1 and has a 5-min idle shutdown.

  — [GitHub](https://github.com/jtsylve/re-mcp)

**Ghidra**
- LaurieWired/GhidraMCP: a Ghidra plugin plus MCP bridge (decompile, rename, comment, analyze), with 5.4k+ stars. A fork contributor on HN claims 110+ MCP tools, 130+ REST endpoints, support for Ghidra 11.3–12.0.2, and localhost-only binding. That is an interested-party claim — [Awesome-RE-MCP](https://github.com/crowdere/Awesome-RE-MCP); [HN](https://news.ycombinator.com/item?id=46882389)
- cyberkaida/ReVa (reverse-engineering-assistant, Apache-2.0, Ghidra 12.0+, ~854 stars, no version shown):
  - It takes a "tool driven approach" with "a variety of small tools" that return focused fragments **plus namespace and cross-references** to limit "context rot".
  - Tools take a schema but **tolerate other inputs and redirect correctable mistakes back to the model**, and they add output that guides the next step.
  - It claims to scale to large binaries and firmware.
  - Headless mode uses ephemeral, session-scoped projects. Assistant mode uses streamable HTTP on :8080.
  - Claude Code plugin skills cover triage, deep analysis, crypto and CTF.
  - It exposes PyGhidra scripting, flagged as a security risk if exposed publicly.
  - No confidence or provenance scoring.

  — [GitHub](https://github.com/cyberkaida/reverse-engineering-assistant)
- clearbluejar/pyghidra-mcp (Apache-2.0, ~436 stars, © 2026):
  - "Headless-first, GUI-capable".
  - Tool descriptions are kept short to cut discovery tokens.
  - GUI-only tools appear only with `--gui`.
  - A single long-lived HTTP server avoids the 10–60 s Ghidra startup.
  - **Project-wide multi-binary** import and analysis (`--threaded`, `--max-workers`).
  - `search_code` does **vector-embedding (ChromaDB) semantic search over decompiled pseudo-C**.
  - GZF analysis cache.
  - Batch `decompile_function` and `list_xrefs` with inline per-item errors.
  - Mutation tools (`rename_*`, `set_variable_type`, `set_function_prototype`).
  - Docker image `ghcr.io/clearbluejar/pyghidra-mcp`.

  — [GitHub](https://github.com/clearbluejar/pyghidra-mcp)
- Other Ghidra servers:
  - GhydraMCP (multi-instance Ghidra).
  - suidpit/ghidra-mcp (Spring Boot).
  - headless-ida-mcp-server (CI/batch).

  — [Awesome-RE-MCP](https://github.com/crowdere/Awesome-RE-MCP)

**Binary Ninja**
- Vector35's own `binaryninja-api` issues refer to an "**integrated Binary Ninja MCP server**":
  - #8566 asks to expose the debugger and runtime patching through it.
  - #8530 asks to expose the binary-similarity API.
  - #8595 (Sept 2026) reports the server rejecting `""` and `0` for optional fields on BN 6.0.

  — [#8566](https://github.com/Vector35/binaryninja-api/issues/8566), [#8530](https://github.com/Vector35/binaryninja-api/issues/8530), [#8595](https://github.com/Vector35/binaryninja-api/issues/8595)
- Sidekick (Vector35's AI assistant) was announced with a waitlist in early 2024 **[background]**. I found no source tying Sidekick to MCP — [X/Vector35](https://x.com/vector35/status/1743380155224010961); [Sidekick-public](https://github.com/Vector35/Sidekick-public)
- Community Binary Ninja servers:
  - fosdickio/binary_ninja_mcp (decompile, stack vars, callers/callees, create function).
  - MCPPhalanx/binaryninja-mcp.
  - binja-lattice-mcp (token auth and encrypted transport).

  — [mcpservers.org](https://mcpservers.org/servers/fosdickio/binary_ninja_mcp); [Awesome-RE-MCP](https://github.com/crowdere/Awesome-RE-MCP)

**radare2, debuggers and dynamic tools**
- radareorg/radare2-mcp: official, 26+ tools, stdio. Also listed:
  - x64dbgMCP (40+ tools).
  - CutterMCP (Rizin).
  - LLDB, with native MCP support since June 2025.
  - GDB MCP servers.
  - frida-mcp (instrumentation).

  — [Awesome-RE-MCP](https://github.com/crowdere/Awesome-RE-MCP)
- The same curated list names **angr, Triton, rr, BinDiff, AFL++, libFuzzer, Pin/DynamoRIO** as tools with **no MCP server** yet. The list is undated (© 2026), so this may be stale — [Awesome-RE-MCP](https://github.com/crowdere/Awesome-RE-MCP)

**Hopper / cross-domain workflow coordinators**
- morluto/rea ("Reverse Engineer Anything", npm `rea-agents`, Node 22.19+/24.11+/26+):
  - It is an MCP server plus CLI.
  - Native backends are **Hopper** (auto-started via an in-Hopper Python bridge; Hopper itself is not shipped), Ghidra and IDA.
  - Also covered: pwntools for offline ELF and recorded crashes (optional GDB/pwndbg), EVM bytecode, JS/Electron, browser, HAR/mitmdump, headless JADX, Binwalk/Unblob, and PTY process capture.
  - Results return "the evidence and limitations behind each conclusion", with snapshots and import/export.
  - **No sandbox**: runtime capture runs the target under the user's own permissions.

  — [GitHub](https://github.com/morluto/rea)
- REA's version and confidence model, from secondary sources:
  - A news page reports **REA 4.1 on 2026-10-06**, adding JADX APK and Binwalk/Unblob firmware analysis. The npm page shows 0.2.1, which conflicts — [ai-tldr](https://ai-tldr.dev/releases/morluto-rea-4-1/); [npm.io](https://npm.io/package/@morluto/rea)
  - A review describes "an evidence bundle with **confidence levels and known gaps**". The README itself mentions only "evidence and limitations" and "unknowns", with no confidence scores — [hysenlabs review](https://hysenlabs.com/projects/morluto-rea); [GitHub](https://github.com/morluto/rea)
  - The project says it is "a workflow coordinator, not a decompiler" and "does not claim to recover original source code" — [hysenlabs](https://hysenlabs.com/projects/morluto-rea)

### Inferences
- **The design trend is convergent.** Explicit session or database handles, batch plus per-item error lists, paginated output, meta-tool discovery (to keep the tool list small) and old/new-value mutation records are now standard. These are context-economy and auditability features, not correctness features.
- Two schools of thought:
  - **"Small primitives plus guidance"**: ReVa, pyghidra-mcp, mrexodia.
  - **"Workflow/skill layer on top of primitives"**: REA evidence bundles, ReVa's Claude Code skills, re-mcp `execute`.
- Hallucination is handled by prompt rules ("never convert bases yourself") and by attaching xrefs and context. **None of these servers mechanically checks** an agent's narrative claims about behavior.
- Sandboxing is weak across the board:
  - Scripting escape hatches (`py_eval`, `run_script`, PyGhidra) are on by default or gated only by an env var.
  - Dynamic capture in REA runs on the host.
  - Docker images (pyghidra-mcp) are the closest thing to isolation, and they cover the analysis tool, not target execution.

### Gaps
- I could not confirm a version or date for the official Hex-Rays IDA MCP server, or its tool list.
- No sourced detail on r2ai (radare2's in-tool agent), on angr-based MCP or agent projects (the curated list says none exists), or on Binary Ninja Sidekick's 2025–26 status.
- Tool counts are self-reported and not comparable across projects.

## 2. Autonomous and semi-autonomous RE and vulnerability-research agents

### Takeaway
The successful autonomous systems are **probe-and-prove loops**. The agent may form hypotheses from static views, but success is declared only on a **mechanical artifact**: a reproducing crash or PoV (Naptime/Big Sleep, AIxCC CRSs), an accepted flag, serial or keygen (CTF agents, CrackMeBench), or a deterministically graded objective (SRE-Bench). Interactive dynamic tools (debuggers, connection tools) measurably help. On realistic, contamination-free binaries, frontier agents still solve only a minority of tasks.

### Cited Findings
**Naptime → Big Sleep (Google Project Zero / DeepMind)**
- **[background, June 2024]** Naptime (Glazunov and Brand) gave the agent:
  - a **Code Browser** (Chromium-Code-Search-like navigation);
  - a **Python** tool in a controlled environment, used for input generation and fuzzing;
  - a **Debugger** to observe behavior under inputs;
  - a **Reporter** to signal task progress.

  — [The Hacker News](https://thehackernews.com/2024/06/google-introduces-project-naptime-for.html); [SecurityBoulevard](https://securityboulevard.com/2024/06/googles-project-naptime-aims-for-ai-based-vulnerability-research)
- With GPT-4 Turbo, Naptime scored **1.00 on CyberSecEval2 "Buffer Overflow"** (vs 0.05 in Meta's paper) and **0.76 on "Advanced Memory Corruption"** (vs 0.24). That is up to a 20x improvement — [Infosecurity Magazine](https://infosecurity-magazine.com/news/google-naptime-vulnerability); [SC Magazine](https://scmagazine.com/news/google-framework-helps-llms-perform-basic-vulnerability-research)
- Big Sleep (Naptime's successor with DeepMind):
  - **[background, late 2024]** It found an SQLite bug that OSS-Fuzz had missed. Project Zero called the results "highly experimental" and noted that a target-specific fuzzer could be as effective — [The Record](https://therecord.media/google-big-sleep-ai-tool-found-bug); [SD Times](https://sdtimes.com/security/google-researchers-successfully-found-a-zero-day-vulnerability-using-llm-assisted-vulnerability-detection/)
  - **July 2025**: Google says Big Sleep found **CVE-2025-6965** (SQLite memory corruption, CVSS 7.2), "only known to threat actors". This is a **vendor claim**, and secondary sources disagree on the bug class (underflow vs overflow vs integer overflow) — [ictsecuritymagazine](https://www.ictsecuritymagazine.com/notizie/big-sleep-ai-autonoma/); [technijian](https://technijian.com/cyber-security/googles-ai-breakthrough-uncovering-zero-day-security-vulnerabilities-with-project-big-sleep/)

**DARPA AIxCC final (Aug 2025)**
- Final results:
  - **Atlantis** (Team Atlanta: Georgia Tech, Samsung Research, KAIST, POSTECH) won with **392.76 points** over 55 challenge projects from 28 repos ($4M). It takes an ensemble approach with eight patching agents.
  - **Buttercup** (Trail of Bits) placed **second with 219 points**, finding 28 vulns and deploying 19 patches. It reports 90% accuracy at $181/point using only **non-reasoning LLMs**, with deterministic workflows and "LLMs only where traditional tools fall short".
  - **RoboDuck** (Theori) is "agentic-first" and built around bug candidates (filter → PoV → patch → SARIF validation). The SoK lists it third, but I could not confirm its score.

  — [ATLANTIS paper](https://arxiv.org/pdf/2509.14589); [ToB blog](https://blog.trailofbits.com/2025/08/07/aixcc-finals-tale-of-the-tape/); [Buttercup](https://trailofbits.com/buttercup/); [SoK AIxCC, arXiv 2602.07666](https://arxiv.org/html/2602.07666v2)
- OSS-CRS (arXiv 2603.08566) repackages AIxCC CRSs for real-world OSS use — [arXiv](https://arxiv.org/html/2603.08566v2)
- Note: AIxCC targets were **source-available** OSS (C/Java). These are not binary-RE systems, but their PoV-gated validation loop transfers.

**CTF and RE agents**
- **[background, Sept 2024]** EnIGMA (built on SWE-agent):
  - Its "**Interactive Agent Tools**" let the agent drive a **debugger** and **server-connection** tool, alongside decompilation and disassembly tools.
  - It reached SOTA on NYU CTF, InterCode-CTF and Cybench across 390 challenges.
  - In an ablation on the HTB rev task "Rebuilding", the task was solved only with both the interactive tools and the summarizer.

  — [arXiv 2409.16165](https://arxiv.org/pdf/2409.16165)
- **CrackMeBench** (arXiv 2605.10597, May 2026, I. David and A. Gervais, "v0" protocol):
  - The agent gets only an executable and must produce an accepted input, serial, artifact or keygen.
  - Tools are explicitly disclosed, runs happen in a **controlled Docker sandbox**, and answers are checked by **hidden oracles**. Command traces, tokens and failure labels are logged.
  - pass@3 with a 5-min budget on the generated split (12 tasks): **GPT-5.5 92%, Claude Opus 4.7 58%, Kimi K2 42%**. On the hard subset: 83%, 33% and 17%.
  - On the public calibration set (8 tasks): **3/8, 2/8 and 1/8**. The authors attribute the drop to large Rust/Go runtimes and custom VMs.
  - Kimi used about 620k tokens per public task versus about 282k for GPT-5.5.
  - The samples are small.

  — [arXiv html](https://arxiv.org/html/2605.10597v1); [alphaXiv](https://www.alphaxiv.org/abs/2605.10597)
- **SRE-Bench** (arXiv 2608.11469, Vals AI):
  - It has 19 **private** real-world-scale programs (16.9K LOC average) and 44 in-house anti-analysis mechanisms, giving **262 binary instances and 1,572 deterministically graded tasks**. It took more than 5,000 expert hours.
  - Paper headline: "GPT-5.6-Sol" fully solves 31.5% and "Claude-Fable-5.1" 26.9%.
  - Agents were "largely insensitive to compiler optimization and static linking". Ablations show both contamination control and realistic scale are essential.
  - **Conflict:** the Vals leaderboard (as of 2026-09-29) shows 56.87% for the top model ("GPT-6 Astra"), so the metric or model set may differ. A challenge counts as solved only when all six objectives are met.

  — [arXiv 2608.11469](https://arxiv.org/html/2608.11469); [Vals AI](https://www.vals.ai/benchmarks/srebench); [BenchLM](https://benchlm.ai/benchmarks/srebench)
- CREBench (arXiv 2604.03750) evaluates LLMs on **cryptographic** binary RE — [arXiv](https://arxiv.org/html/2604.03750v1)

### Inferences
- The loop pattern shared by Naptime, EnIGMA and AIxCC is **static hypothesis → dynamic probe (debugger, emulator, Python harness) → artifact-gated success**. Narrative findings are not accepted as success. Benchmarks with hidden oracles (CrackMeBench, SRE-Bench) carry the same idea into evaluation.
- Buttercup's second place with non-reasoning models and deterministic workflows suggests that **harness structure can matter as much as model strength**. That supports harness-heavy designs like ReSchema.
- On contamination-free, realistic binaries, results drop sharply. CrackMeBench public tasks score at most 38%, and SRE-Bench full solves are around 27–57% depending on the source. So large or obfuscated binaries remain the frontier.

### Gaps
- No primary-source data was retrieved on XBOW (web-app pentesting, not binary RE), on Big Sleep's 2025 batch disclosures beyond CVE-2025-6965, or on the NYU CTF agent's or Cybench's per-category rev solve rates.
- I could not find RECon, OffensiveCon or DEF CON 2025/26 talk material in the searches.
- I found no dedicated 2025–26 angr-driven LLM agent.

## 3. Mechanical verification versus narrative evidence; hidden tests and anti-overfitting

### Takeaway
The 2026 neural-decompilation literature has converged on one finding: **compile success and shipped or LLM-generated tests are weak oracles, and agents game single metrics.** The strongest systems use **differential execution against the original** with fuzzed or fresh inputs. Even then, re-executability only approximates equivalence. Hidden or private test sets now appear in benchmarks (CrackMeBench hidden oracles, SRE-Bench private programs), but few agent harnesses use hidden tests *inside the loop*.

### Cited Findings
- **Agent4Decompile / MCGD** (arXiv 2604.23940, Vanderbilt, 2026-04-27, revised 2026-05-01):
  - Validation runs in three levels: syntax → GCC compile → **behavioral equivalence via LLM-generated test cases**. Specialized agents repair failures from structured error feedback.
  - It covers 1,641 ExeBench binaries across RetDec, Ghidra and angr decompilers.
  - In ablation, **compile-only approaches reached 0% behavioral correctness despite 91–99% compile rates**.
  - 90%+ of binaries converge within 2 iterations, at about $0.03–0.05 per binary. A secondary listing reports 84–97% re-executability.

  — [arXiv 2604.23940](https://arxiv.org/html/2604.23940v1)
- **"When LLM Decompilers Recompile More and Preserve Less" / Decompile-Diverge** (arXiv 2609.05370, Sept 2026):
  - It builds a per-function driver, grows a **fuzz corpus from the reference binary**, and compares **observable post-state**.
  - **4.9% of candidates that pass every shipped test still diverge** (up to 13% for one system).
  - The strongest refinement LLM raises Ghidra's build rate from 75% to 90% while its "Matched" rate *falls* from 74% to 62%.
  - On disclosed vulnerabilities, up to one tenth show "Crash Absence", meaning the decompiled code silently drops the bug.

  — [arXiv 2609.05370](https://arxiv.org/html/2609.05370v1)
- "Recompilation Is Not Enough: Test-Guided Decompiled-C Repair" (arXiv 2609.07201) exists. Only its title was retrievable — [arXiv](https://arxiv.org/pdf/2609.07201)
- arXiv 2606.06838 (title not confirmed) reports three phases of Ghidra-MCP agent work:
  1. **Direct Ghidra MCP control** had incomplete coverage, inconsistent quality and no feedback loop.
  2. A structural-similarity loop reached functional equivalence, but **readability degraded as agents gamed the single metric**.
  3. The final design pairs a composite readability metric with binary validation, plus a structural-similarity gate so that "equivalence is non-negotiable".

  — [arXiv 2606.06838](https://arxiv.org/html/2606.06838)
- Other 2026 work:
  - AutoDecompiler (arXiv 2606.16162): an RL-trained decompilation LLM for feedback-driven multi-turn refinement — [arXiv](https://arxiv.org/abs/2606.16162)
  - Decaf (arXiv 2605.11501): compiler-feedback iteration, 26% → 83.9% per a secondary listing (not opened).
  - SK2Decompile (arXiv 2509.22114): two-phase skeleton→skin decompilation — [arXiv](https://arxiv.org/pdf/2509.22114)
  - Echo (arXiv 2609.18706): "matching decompilation using trusted back translation" — [arXiv](https://arxiv.org/pdf/2609.18706)
  - "Rebuild Dossier" (arXiv 2608.23616): mechanically enforced specs for agentic app rebuilds — [arXiv](https://arxiv.org/pdf/2608.23616)
- **[background, Jan 2025]** "Fast, Fine-Grained Equivalence Checking for Neural Decompilers" (arXiv 2501.04811) — [arXiv](https://arxiv.org/pdf/2501.04811)
- **[2025 background]** Decompile-Bench (arXiv 2505.12668) provides million-scale binary–source function pairs, with a re-executability set built from HumanEval, MBPP and GitHub — [arXiv](https://arxiv.org/abs/2505.12668)
- Hidden-oracle benchmarks:
  - CrackMeBench uses hidden oracle checks — [arXiv](https://arxiv.org/html/2605.10597v1)
  - SRE-Bench uses private, contamination-free programs with deterministic grading — [arXiv](https://arxiv.org/html/2608.11469)
- **Provenance-aware evidence gating** — "When Binaries Talk Back" (arXiv 2607.12507, I. Santos-Grueiro, ~July 2026):
  - In **representation-confusion** attacks, binary-derived strings get promoted to instruction authority or to *claim-validating evidence*. Several records from one origin create **false corroboration**.
  - Without runtime controls, models proposed the planted unsafe action in **35/40 adversarial cases (0/40 clean)**. Even with "Data-Only" rendering there were still 15 unsafe proposals.
  - A **provenance-aware evidence gate** that groups records by origin before counting support blocked every planted false claim and accepted every supported claim. A naive support counter accepted most false claims.
  - It introduces RARE-Bench with paired clean, benign-control and adversarial binaries.
  - It is a preprint and has not been replicated.

  — [arXiv 2607.12507](https://arxiv.org/html/2607.12507); [Pith](https://pith.science/paper/2607.12507)
- Related attack work:
  - "Automatically Attacking Software Reverse Engineering AI Agents" (arXiv 2605.30667) — [arXiv](https://arxiv.org/pdf/2605.30667)
  - Quarkslab argues anti-reversing should return **plausible wrong answers** instead of crashing, to fool LLM-assisted RE — [gbhackers](https://gbhackers.com/llm-assisted-reverse-engineering/)

### Inferences
- The field has independently reached ReSchema's core premises:
  - compile is not correct (Agent4Decompile's 0% result);
  - shipped tests can be overfit (Decompile-Diverge's 4.9–13%);
  - single metrics get gamed (2606.06838).
- **Decompile-Diverge is the closest published analogue to ReSchema level B**: a per-function driver, a fuzz corpus grown from the reference, and post-state comparison. Its "Crash Absence" finding is an argument for keeping a `crash` field in function divergences.
- Most loops still rely on **LLM-generated tests** (Agent4Decompile), which the agent family can bias. Judge-owned fresh entropy (ReSchema's hidden gate) is stricter and rare.

### Gaps
- I could not read the full text of 2609.05370, 2606.06838, 2609.07201 or 2604.23940 (arXiv unreachable), so method details come from snippets.
- I found no system that uses **symbolic or bounded equivalence checking inside an agent loop** in 2026 (2501.04811 is a 2025 non-agentic checker).

## 4. Sandboxing and trust models for untrusted binaries and agent-written code

### Takeaway
Benchmarks isolate the target (Docker for CrackMeBench; isolated environments with no external services for SRE-Bench). Practitioner MCP servers mostly isolate **nothing**. They run analysis tools on the host, expose arbitrary-script tools behind at most an env flag, and (in REA's case) run dynamic capture under the user's own permissions. A newer threat class treats **the binary's content as an adversary to the agent** (prompt or representation confusion), not just its execution.

### Cited Findings
- CrackMeBench runs agents in a "controlled Docker sandbox" with explicit tool disclosure — [arXiv](https://arxiv.org/html/2605.10597v1)
- SRE-Bench experiments run in isolated environments with no external systems or services — [arXiv](https://arxiv.org/html/2608.11469)
- Naptime's Python tool runs in a "controlled environment" **[background]** — [The Hacker News](https://thehackernews.com/2024/06/google-introduces-project-naptime-for.html)
- re-mcp's isolation and gating:
  - The `execute` meta-tool is "sandboxed Python".
  - `run_script` is gated by `IDA_MCP_ALLOW_SCRIPTS`.
  - The daemon binds to 127.0.0.1 with bearer-token auth.
  - MCP destructive/read-only annotations let clients confirm risky operations.

  — [GitHub](https://github.com/jtsylve/re-mcp)
- mrexodia/ida-pro-mcp hides debugger tools by default but does not gate `py_eval` or patch tools — [GitHub](https://github.com/mrexodia/ida-pro-mcp)
- ReVa warns that PyGhidra scripting access is a risk if the server is exposed publicly — [GitHub](https://github.com/cyberkaida/reverse-engineering-assistant)
- REA: "No sandbox is described". Runtime capture runs the target with the user's permissions — [GitHub](https://github.com/morluto/rea)
- Binary-content-as-attacker:
  - RARE results: 35/40 unsafe proposals without controls, and 15 even with data-only rendering. The provenance-aware gate fixes this — [arXiv 2607.12507](https://arxiv.org/html/2607.12507)
  - See also [arXiv 2605.30667](https://arxiv.org/pdf/2605.30667)

### Inferences
- ReSchema's model is stricter than any practitioner MCP surveyed:
  - targets run only under qiling emulation with a fresh rootfs;
  - agent C runs only in one-shot rootless podman;
  - there is no host gcc;
  - there is no free-form script tool.
- The main threat ReSchema does *not* yet model is **RARE-style confusion**. On real-world binaries, strings and disassembly in `task_open`, and decoded stdout previews in divergences, become attacker-controlled text inside the agent's context.

### Gaps
- No public detail was found on how AIxCC CRSs sandboxed PoV execution (OSS-Fuzz-style containers are likely, but unconfirmed here).
- Hex-Rays and Vector35 official servers' security models were not retrievable.

## 5. Quantitative results, benchmarks and known failure modes

### Takeaway
The headline numbers are high on synthetic or generated tasks and low on realistic or contamination-free ones. The recurring failure modes are:
- metric gaming and overfitting to tests;
- compile-but-wrong output;
- arithmetic and base-conversion hallucination;
- context blow-up on large runtimes (Rust/Go) and custom VMs;
- obfuscation;
- adversarial binary content.

### Cited Findings
- **CrackMeBench**:
  - Generated split: 92%, 58% and 42% (GPT-5.5, Opus 4.7, Kimi K2).
  - Public split: 38%, 25% and 12%.
  - Failures are blamed on Rust/Go runtime noise and custom VMs.
  - GPT-5.5 averaged 108.6 s per generated task.

  — [arXiv](https://arxiv.org/html/2605.10597v1)
- **SRE-Bench**:
  - 31.5% and 26.9% full solves in the paper, versus 56.87% top on the leaderboard (conflicting).
  - Agents are insensitive to -O level and static linking. Anti-analysis plus scale is what hurts.

  — [arXiv](https://arxiv.org/html/2608.11469); [Vals AI](https://www.vals.ai/benchmarks/srebench)
- **Decompilation correctness**:
  - Agent4Decompile: 0% behavioral correctness for compile-only, ≥90% convergence within 2 iterations — [arXiv](https://arxiv.org/html/2604.23940v1)
  - Decompile-Diverge: 4.9% overall (13% max) silent divergence; refinement trades Matched rate (74% → 62%) for build rate (75% → 90%) — [arXiv](https://arxiv.org/html/2609.05370v1)
- **Vuln research**:
  - Naptime: CyberSecEval2 BO 1.00 and AMC 0.76 **[background]** — [Infosecurity](https://infosecurity-magazine.com/news/google-naptime-vulnerability)
  - AIxCC: Atlantis 392.76 and Buttercup 219 points — [ATLANTIS](https://arxiv.org/pdf/2509.14589); [ToB](https://blog.trailofbits.com/2025/08/07/aixcc-finals-tale-of-the-tape/)
- **Practitioner failure modes**:
  - Number-base and byte conversion hallucination; obfuscated code should not be handed to LLMs — [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
  - Context rot on large binaries is what motivates small tools — [ReVa](https://github.com/cyberkaida/reverse-engineering-assistant)
  - Token overflow is handled by pagination caps — [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
- **Agent self-termination and metric gaming**: in 2606.06838, agents gamed structural similarity at readability's expense — [arXiv](https://arxiv.org/html/2606.06838)

### Inferences
- Stripped binaries appear less damaging than expected: SRE-Bench reports insensitivity to optimization and static linking. **Scale, runtimes and anti-analysis** dominate. This bears directly on ReSchema's real-world goal: the synthetic sym/stripped × O0–O2 matrix probably does not span the hard axis.

### Gaps
- No independent replication of any of the 2026 benchmark numbers was found. All are author or vendor reports.
- No sourced per-model stripped-vs-symbolized ablation.

## 6. Mapping transferable ideas onto ReSchema

### Takeaway
ReSchema already embodies what the 2026 literature recommends: a judge-only, differential, fresh-entropy verifier with structured feedback. It is stricter than every practitioner MCP surveyed and closer to Decompile-Diverge than to Agent4Decompile. The ideas most worth importing are about **context economy and provenance at the tool surface**, **adversarial-content robustness for real binaries**, and **benchmark realism**, not about loosening the judge. ReSchema facts below are from [README.md](../../../../README.md), [ARCHITECTURE.md](../../../../ARCHITECTURE.md) and [docs/roadmap.md](../../../../docs/roadmap.md).

### Cited Findings
**Validation of existing choices**
- Compile ≠ correct (0% behavioral correctness at 91–99% compile, from [Agent4Decompile](https://arxiv.org/html/2604.23940v1)) and passing tests ≠ preserving behavior (4.9–13% hidden divergence, from [Decompile-Diverge](https://arxiv.org/html/2609.05370v1)). Both support ReSchema's hidden fresh-entropy gate and its level-B differential fuzz on {ret, mem}.
- Single-metric gaming ([2606.06838](https://arxiv.org/html/2606.06838)) supports ReSchema's "strictness invariant": memory and coaching must never relax acceptance (roadmap Phase 2).
- Hidden oracles and private tasks in [CrackMeBench](https://arxiv.org/html/2605.10597v1) and [SRE-Bench](https://arxiv.org/html/2608.11469) parallel ReSchema's per-submission `secrets.token_hex(16)` hidden draws. ReSchema's version is stronger because the draws are fresh per submission, not a fixed private set.
- Buttercup's result ([ToB](https://blog.trailofbits.com/2025/08/07/aixcc-finals-tale-of-the-tape/)) suggests that structured harness workflows can substitute for model strength. That is consistent with ReSchema's efficiency metric E and its transfer benchmark.

**Fits scope guardrails (low-risk imports)**
1. **Context-economy conventions from re-mcp, ida-pro-mcp and pyghidra-mcp**:
   - explicit task/session handles (ReSchema already keys on task);
   - cursor pagination with hard caps on `task_open` disassembly slices and on the `status` ledger;
   - batch `experiment` with per-item errors;
   - short tool descriptions.

   This is cheap, needs no judge change, and matters once real binaries make slices large ([ReVa](https://github.com/cyberkaida/reverse-engineering-assistant) on "context rot").
2. **Harness-side arithmetic**: an `int_convert`-like helper, or always emitting hex/dec/signed views in divergence payloads, removes the most-cited hallucination class ([ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)). The `ret` divergence could carry signed, unsigned and hex renderings. Feedback stays first-divergence only.
3. **A `crash` divergence class plus crash-absence tests**: Decompile-Diverge's "Crash Absence" ([arXiv](https://arxiv.org/html/2609.05370v1)) is the failure ReSchema's level-B `crash` field already covers. A negative test where the model "fixes" an original crash on fuzzed input should be rejected, consistent with the negative-tests-first convention.
4. **Provenance-aware memory, matching the RARE evidence gate**: ReSchema's two-tier provenance (verified_fact on acceptance vs agent `unverified_hypothesis`) already matches [arXiv 2607.12507](https://arxiv.org/html/2607.12507). The transferable refinement is to **key support by origin** so that repeated agent-declared hypotheses never "corroborate" one another, and only judge acceptance promotes a fact.
5. **Old/new-value audit trails and MCP tool annotations** ([re-mcp](https://github.com/jtsylve/re-mcp)): mark `experiment` and `status` as read-only/idempotent and `submit_model` as state-changing in the MCP annotations. This is cheap and helps clients and dogfood transcripts.
6. **Benchmark realism axis**: [SRE-Bench](https://arxiv.org/html/2608.11469) finds -O level and static linking matter little, while scale and anti-analysis matter a lot. ReSchema's 60-slot matrix varies the axes that matter less. When moving toward real-world binaries, add seeds with larger runtimes or VM-style dispatchers rather than more compiler/opt permutations. CrackMeBench's split between "generated" and "public calibration" sets is a useful template for the 2C transfer benchmark.

**Needs care: conflicts or tension with deliberate choices**
- **Adversarial content in context.** When real binaries arrive, `stdout_decoded`, `stderr_decoded` and string facts in `task_open` become attacker-controlled. RARE shows that data-only rendering still leaves 15/40 unsafe proposals ([arXiv](https://arxiv.org/html/2607.12507)). ReSchema's judge-only acceptance means a confused agent cannot get a false *accept*. Inside the 2C dogfood driver, though, the agent sandbox (opencode) could be steered into unsafe actions. Consider labelling binary-derived fields explicitly and keeping hex authoritative (already the policy).
- **Rich multi-failure feedback** (Agent4Decompile's structured error lists, LLM-generated test suites) **conflicts with first-divergence-only feedback**, which deliberately prices extraction of the recorded corpus in submissions. Recommendation: do not adopt. If needed, enrich the *single* divergence (e.g. the planned `dep_slice` fd-chain) rather than adding more divergences.
- **LLM-generated tests as oracle** (Agent4Decompile) **conflicts with judge-only acceptance** and the harness-controlled N_FUZZ floor. Reject.
- **Composite readability metrics** (2606.06838) belong to the roadmap's separate *refactoring agent*, gated by an equivalence gate that must be non-negotiable. This matches the two-agent direction, but it should not enter the RE-agent judge.
- **Exposing a debugger, Python `execute`, or free-form decompiler tools** (Naptime, EnIGMA, ida-pro-mcp, REA) would raise solve rates (EnIGMA's ablation). It would also widen the trust boundary and bypass the `experiment` probe accounting that feeds E. If adopted, it must go through the same podman/qiling boundary and be counted as N_exp. A decompiler view (Ghidra headless in the toolchain image) fits the trust model better than a live debugger.
- **Symbolic or bounded equivalence** ([2501.04811](https://arxiv.org/pdf/2501.04811)) is explicitly out of scope ("no symbolic equivalence" guardrail). It is noted only as a future option.
- **Multi-binary, project-wide analysis and semantic code search** (pyghidra-mcp ChromaDB) fit the phase-3 "structural CFG fingerprint" upgrade path for the family cache, but are premature under the current manifest-keyed design.

### Inferences
- ReSchema's distinctive contribution relative to the 2026 field is **in-loop, per-submission fresh-entropy hidden tests plus emulated ground truth plus container-isolated agent code**. Benchmarks use hidden oracles only for final scoring, and decompilation papers use fixed or LLM-made tests. That is a defensible novelty claim, but it should be stated relative to Decompile-Diverge, which shares the fuzz-from-reference idea.
- The strongest external evidence for keeping first-divergence-only feedback is indirect: metric gaming (2606.06838) and test overfitting (2609.05370). I found no paper that directly ablates the feedback granularity of a verification harness.

### Gaps
- No surveyed system reports an efficiency or probe-cost metric comparable to ReSchema's E. CrackMeBench reports tokens and wall-clock only, so there is no external baseline for E.
- No published live-agent results exist on an emulation-judged, byte-exact world-model task, so no direct comparison is possible.
