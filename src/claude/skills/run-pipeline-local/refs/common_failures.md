# Common Failures — Pipeline Failure Patterns
> Read this file when debugging gate failures or unexpected behavior.
> Each entry documents a real failure from a production run.

| What went wrong | Why it happened | How to prevent |
|----------------|-----------------|----------------|
| Lit review with <20 papers, no real searches | Subagent recalled papers from memory | Verify MCP tool calls in output; reject if missing |
| **Lit review phases 4-6 skipped** | All 7 phases delegated to a single subagent which collapsed them into 3-phase pass | Main context MUST orchestrate each phase as separate dispatch with inter-phase validation gates |
| **Fabricated DOIs in lit review** | No DOI validation step; subagent invented plausible-looking DOIs | Phase 4 now requires resolve_doi call for every paper; unverified DOIs excluded from final output |
| **No structured extractions from papers** | Phase 5 (extraction) skipped when single subagent compressed pipeline | Phase 5 is separate opus dispatch with ≥30 extraction gate; themes/gaps in Phase 6 depend on extraction output |
| No figures in final paper | Figure composition stage was skipped | Stage 7 is mandatory; validate composite figs exist |
| Single-pass analysis, no iteration | Entire loop delegated to one subagent | Main context must orchestrate Round→Reflection→Round |
| Reanalysis by narrative only, no code | Skill didn't require code execution | Check for .py files in cross_team/; reject narrative-only |
| Analysis references nonexistent columns | Planner was skipped | Stage 5 is mandatory and not toggleable |
| Teams selected duplicate datasets | No cross-team awareness | Pass other_team_focuses to each team's data acquisition |
| Missing supplementary tables | Post-processing skipped | Verify table_s*.csv files exist |
| Post-synthesis chain not executed | Skill didn't list all 9 nodes with execution details | All 9 post-synthesis steps are mandatory with model/prompt/gate specs (includes Paper Gen + Paper Descent) |
| **Supplementary figures document missing** | Post-synthesis subagent skipped the step or paper embedded all figures inline | Paper may only embed ≤6 main figures; all remaining figures go to supplementary_figures.pdf via post-synthesis |
| **Paper embeds all figures inline, no supplementary refs** | Paper gen subagent included every figure in `![...]()` syntax | Paper gen prompt now explicitly bans embedding non-main figures; supplementary figures referenced by S-number |
| Paper PDF is text-only (no images) | Figures not embedded during conversion | Check PDF file size; >500KB indicates embedded figs |
| **Missing panel_agreement in PI roundtable** | Dispatch prompt didn't require it | Validate PI JSON has `panel_agreement: float` before proceeding |
| **Blocked tests silently dropped** | Variance-based gene selection excluded hypothesis-driven genes; verdict didn't escalate | Verdict must include `blocked_tests` array; blocked tests with `escalation: CRITICAL_NEXT_ROUND` must be addressed in next round dispatch |
| **Falsified hypotheses buried in findings** | No dedicated array; falsifications mixed with validated findings | Verdict must include `falsified_hypotheses` array with `{hypothesis, evidence_against, what_was_learned}` |
| **Sequential dependencies labeled as consensus threads** | Thread types not distinguished | Threads must use `type: CRITICAL_PATH` (sequential gates) or `type: DIVERGENT` (parallel) — never generic "consensus" |
| **No roundtable during iterative deepening** | Skill said "Reflection subagent" but didn't specify full persona roundtable | Stage 6 now requires roundtable with persona injection; must produce next_hypotheses + next_approach |
| **Iterative deepening skipped after Round 1** | Reflection subagent voted CONVERGED after Round 1 citing data limitations | Convergence after Round 1 is NEVER acceptable; gate now blocks early convergence |
| **Multi-team reanalysis entirely skipped** | Main context jumped from meta-comparison to final synthesis | Phase 3 now has 4-step sequential protocol with anti-skip file existence checks |
| **Divergences resolved narratively** | No orchestration loop for per-divergence reanalysis dispatch | Reanalysis loop now mirrors iterative deepening: main context dispatches per-divergence |
| **Silent dataset fallback — approach specified 6 datasets, only 1 small fallback downloaded** | No manifest tracking attempted vs. succeeded downloads; validation gate only required "≥1 dataset"; no check that downloaded data matched approach | data_manifest.json now required with per-dataset attempt/status/error; gate checks PRIMARY dataset from approach; fallback must be flagged; user warned on failures |
| ArrayExpress datasets inaccessible | GEO-only tools couldn't access E-MTAB-* | Use `download_biostudies_file` for E-MTAB-* accessions |
| Data locked in large RAW.tar | GSE-level download hit size limit | Use `get_geo_sample_ids` + `download_geo_sample_file` for individual sample files |
| **Only 25 papers in lit review** | Phase 4 hardcoded "top 25" for full-text retrieval | Phase 4 now attempts top 50; gate requires ≥35 full texts |
| **Only 2 rounds of iterative deepening** | Min iterations was 2; no corroboration gate; no validator evidence | Min raised to 3; corroboration required; non-LLM validators constrain roundtable |
| **Subagent timeout loses entire analysis round** | No checkpoint before roundtable dispatch; 529 error destroys context | Step 2b checkpoint saves to disk; timeout recovery re-dispatches from checkpoint |
| **Audit trail empty** | Audit dispatch instructions were vague; no per-stage dispatch | Explicit haiku dispatch after EACH stage; validation gate checks audit/ non-empty |
| **Non-LLM validators never ran** | Validators existed in code but skill didn't mention them | Step 2c now mandatory; 4 validators run after every round; results constrain roundtable |
| **Duplicate figures in paper** | No figure inventory; paper gen picked same file twice | Figure inventory built pre-paper; post-paper dedup check rejects duplicates |
| **Config interview different from prior runs** | Interview steps were verbose and split across 6 AskUserQuestion calls | Streamlined: present defaults table, single confirmation unless user overrides |
| Full text retrieval returns null for all papers | s2_pdf_url hardcoded None, no DOI-to-PMID for PMC | Fixed 2026-03-16: S2 metadata enrichment now forwards openAccessPdf URL |
| **Per-team papers written before cross-team comparison — lossy compression of findings** | Paper prose loses statistical detail and figure path provenance; meta-comparison must re-extract claims from narrative | Use Findings Dossier (structured JSON) for per-team output in multi-team mode; write single comprehensive paper post-synthesis after all reanalysis is complete |
| **Composite figures double-rasterized — blurry text, color banding, bloated PDFs** | Figure composition used `imread()`+`imshow()` to paste PNGs into new figure, double-rasterizing content | BANNED: `imread`/`imshow` for compositing. Composite scripts MUST reload source Parquets and re-plot directly into `plt.subplot()` grids. Validation: composite PDFs should be ≤300 KB (true vector); >500 KB = banned antipattern. Paper PDF dropped from 7.7MB to 3.66MB after fix. |
| **Paper references supplementary figures but no supplementary_figures.pdf generated** | Paper gen mentioned "Supplementary Figure S1" but no post-synthesis step compiled the actual document | Post-synthesis chain MUST include Supplementary Figures Document step; paper gen subagent MUST be told supplementary figures exist and will be compiled separately |
| **RNA-only findings promoted to mechanism/therapy** | No evidence-type classification; roundtable endorsed compelling expression patterns | Evidence-typing validator classifies evidence types, assigns conclusion ceilings; violations flagged as CRITICAL |
| **Multi-team convergence treated as independent replication** | Teams analyze same data with same priors → "convergence" is pseudo-independence | Convergence validity classifier labels Type A/B/C; only Type A strongly upgrades confidence |
| **No competing explanations generated** | Pipeline produces one narrative, stress-tests pieces but not the whole | Model competition generates 2-6 alternatives per claim; synthesis uses winning qualifier language |
| **Synthesis escalates beyond component evidence** | Individual findings are "candidate_pathway" but synthesis concludes "mechanism" | Synthesis claim ceiling computes effective ceiling from components + convergence type; violations blocked |
| **Same dataset drives convergence across teams** | Dataset reuse not tracked; 3 teams on same GEO series = pseudo-replication | Dataset reuse scorer computes independence score; reuse_penalty >0.2 downgrades confidence tier |
| **Finding conclusion flips under different null model** | Null model choice sensitivity not propagated as uncertainty | Null sensitivity check classifies stable/sensitive/fragile; fragile findings auto-flagged |
| **Cross-species comparison confounds regeneration claim** | No comparator audit; zebrafish vs human mixed species and cell identity | Comparator auditor detects cross-species+cell-type and flags as CRITICAL; suggests better-matched public datasets |
| **Preprints cited as established evidence** | No preprint detection in reference validation | Citation provenance validator detects preprint DOI patterns and qualifying language |
| **Expression-only finding lacks orthogonal support** | No post-convergence modality search | Modality corroboration stage searches 7 public databases per gene for protein/genetic/perturbation evidence |
| **Paper claims novelty for established results** | No novelty check against literature | Novelty assessor grades each finding against lit review via TF-IDF; flags overclaimed novelty language |
| **No search for counter-evidence** | Pipeline only searches FOR supporting evidence | Contradiction retriever constructs negation queries and searches PubMed/S2 for published refutations |
| **Disease association treated as mechanistic validation** | Single corroboration score pooled all sources | Corroboration split into relevance (context) vs mechanistic (causal) — only mechanistic upgrades ceilings |
| **Paper sentences exceed finding-level ceilings** | Ceilings enforced at finding level but paper writer uses stronger language per-sentence | Sentence claim governor parses every sentence and flags/rewrites those exceeding their finding's ceiling |
| **Claims never tested outside original data** | Pipeline produces internally robust findings but never checks if predictions hold in external data | Prediction validation loop generates structured predictions, searches public DBs, executes tests, updates confidence |
| **Implicit predictions never formalized** | Mechanism claims imply temporal, perturbation, binding predictions that go unstated | Prediction generator extracts 8 categories of testable predictions from every finding |

---

## Adversarial Review Failures

### Reviewer batch dispatch fails (per-round)
**Symptom:** `reviewer_team/round{N}/review.json` missing after dispatch.
**Fix:** Re-dispatch once. If second failure: proceed without reviewer feedback for this round. Log warning. Teams may converge without reviewer pressure — post-synthesis review (Phase 3.5) serves as fallback.

### Checklist MCP checks skipped
**Symptom:** FM-03 or FM-06 produce `status: "skipped"` due to MCP unavailability.
**Fix:** Not blocking. Reviewer panel is informed and applies extra scrutiny to novelty and convergence claims. If both MCP-dependent checks are skipped, consider whether those claims need manual review.

### Tier escalation blocked by minor_revisions
**Symptom:** Tier 1 changes a conclusion but editorial decision is `minor_revisions` (Tier 2 blocked).
**Fix:** By design. The changed conclusion is included in revised synthesis with updated confidence but without full round investigation. Log the blocked escalation as a warning.

### Multiple `requires_further_analysis` dispositions
**Symptom:** Data cleanup is skipped, multiple concerns unresolved.
**Fix:** Correct safety behavior. Paper generator receives these as explicit limitations. Human review recommended before submission.

### Reviewer team has insufficient personas
**Symptom:** Persona library has fewer than N_analysis + 5 independent candidates.
**Fix:** Fallback cascade activates automatically: 5 → 3 → dynamically generated archetype reviewers. Check `team_assignment.json` to verify fallback was used.
