# Literature Review — 7-Phase Orchestration Protocol
> This file is read by the main context before Stage 1 and included in phase subagent prompts.
> It is the COMPLETE protocol — do not abbreviate or summarize when dispatching subagents.

## Stage 1: Literature Review (Local AutoReview — Main Context Orchestrated)

**Model**: sonnet (phases 1-4), opus (phases 5-7)
**MCP Tools**: `search_pubmed`, `search_semantic_scholar`, `search_openalex`,
`search_perplexity`, `resolve_doi`, `retrieve_full_text`
**Figure format**: All figures produced by the pipeline MUST be saved as **PDF (vector)**
for publication-quality outputs. PNG copies may be generated alongside for previewing,
but the canonical output is always `.pdf`. This applies to analysis figures, composites,
findings network, and graphical abstract.

**CRITICAL: DO NOT run `autoreview` CLI or AutoReview as a subprocess** — that requires
an Anthropic API key. For local runs, the lit review is executed as a multi-phase
subagent pipeline that replicates AutoReview's architecture using MCP tools + Claude
Code subagents as the LLM at each phase.

**CRITICAL: The main context MUST orchestrate each phase as a SEPARATE subagent dispatch,
validating intermediate outputs between phases.** Delegating all 7 phases to a single
subagent is a KNOWN FAILURE MODE — it produces a shallow 3-phase pass where phases 4-6
(full-text retrieval, structured extraction, theme clustering) are silently skipped.
This happened in the 2026-03-14 run: 38 papers screened but no full texts retrieved,
no structured extractions, and 4 papers had fabricated DOIs (`10.64898/` prefix).

The reason for multi-phase orchestration (not a single subagent):
1. A single subagent hits context limits trying to hold 100+ papers
2. Without inter-phase validation, expensive phases (full-text, extraction) get skipped
3. DOI fabrication is only detectable when Phase 4 actually calls `resolve_doi`

**Orchestration protocol** (main context controls the pipeline):

```
output_dir = {experiment_dir}/shared/  (or {team_dir}/ for per-team)

# Phase 1: Query Expansion
1. Dispatch sonnet subagent with:
   - Research question
   - Task: Generate 8-12 diverse search queries covering different facets
   - Each query must specify target_api (pubmed/semantic_scholar/openalex)
   - Output: write queries.json to output_dir

2. GATE: Read queries.json → verify ≥8 queries with target_api assignments
   If <8 queries → RE-DISPATCH with feedback

# Phase 2: Multi-Source Search
3. Dispatch sonnet subagent (or parallel subagents per source) with:
   - queries.json from Phase 1
   - MCP tools: search_pubmed, search_semantic_scholar, search_openalex, search_perplexity
   - Task: For EACH query, call the assigned search API
   - ALSO call search_perplexity for 2-3 high-level queries (catches recent/grey lit)
   - Deduplicate by DOI/title (keep highest relevance per duplicate)
   - Output: write raw_candidates.json with source attribution per paper

4. GATE: Read raw_candidates.json → count unique papers
   If <100 raw candidates → RE-DISPATCH Phase 2 with expanded queries
   Verify source diversity: results from ≥3 different APIs

# Phase 3: Screening
5. Dispatch sonnet subagent with:
   - raw_candidates.json from Phase 2
   - Research question (for relevance scoring)
   - Task: Score each paper 0-10 for relevance
   - Apply inclusion criteria: directly relevant to research question
   - Apply exclusion criteria: reviews-only, retracted, non-English, pre-2015 (unless seminal)
   - Keep papers scoring ≥7.0
   - Output: write screened_papers.json (scored + ranked) AND screening_summary.md

6. GATE: Read screened_papers.json → count papers
   If <40 screened papers → LOWER threshold to 6.0 and re-screen
   If still <25 → RE-DISPATCH Phase 2 with broader queries
   Verify: each paper has title, authors, year, DOI/PMID, relevance_score

# Phase 4: Full Text Retrieval + DOI Validation
7. Dispatch sonnet subagent(s) with:
   - screened_papers.json (top 50 by relevance_score)
   - MCP tools: retrieve_full_text, resolve_doi
   - Task: For EACH of top 50 papers:
     a. Call resolve_doi to validate the DOI is real (NOT fabricated)
     b. Call retrieve_full_text to get full text or extended abstract
     c. Truncate full texts to 15K chars (context management)
     d. Flag papers where DOI resolution fails as UNVERIFIED
   - If a single subagent cannot handle 50 papers (context limits), split into
     2 parallel subagents of 25 each, then merge results
   - Output: write full_texts.json (paper_id → {text, doi_verified, source})
   - Output: write doi_validation.json (paper_id → {doi, resolved: bool, error})

8. GATE: Read full_texts.json + doi_validation.json
   - ≥35 papers with retrieved text (full text or extended abstract)
   - ≥40 papers with doi_verified: true
   - If any papers have UNVERIFIED DOIs → flag them for exclusion in Phase 5
   - If <35 papers with text → RE-DISPATCH with broader retrieval settings
   - If <25 papers with text after 2 attempts → proceed with warning (paywall-heavy field)
   Report: "Retrieved full text for X of Y papers. Z DOIs unverified."

# Phase 5: Structured Extraction
9. Dispatch opus subagent with:
   - full_texts.json from Phase 4
   - screened_papers.json (for abstract-only papers not in full_texts)
   - doi_validation.json (to exclude unverified papers)
   - Task: For EACH paper extract structured fields:
     * key_findings: list of 2-5 main results
     * methods_used: experimental and computational methods
     * limitations: stated or inferred limitations
     * relevance_justification: why this paper matters to the research question
     * datasets_mentioned: any public dataset accessions (GEO, SRA, etc.)
     * genes_mentioned: key genes/proteins discussed
     * statistical_methods: tests used, sample sizes, effect sizes
   - For abstract-only papers: extract what's available from abstract
   - EXCLUDE papers flagged as UNVERIFIED in doi_validation.json
   - Output: write extractions.json (paper_id → extraction dict)

10. GATE: Read extractions.json → verify structure
    - ≥30 papers with extractions (screened minus unverified)
    - Each extraction has ≥2 key_findings
    - datasets_mentioned populated where applicable
    If <30 extractions → check why (likely too many DOI exclusions — may need
    to re-run Phase 2 searches)

# Phase 6: Theme Clustering + Gap Detection
11. Dispatch opus subagent with:
    - extractions.json from Phase 5
    - Research question
    - Task: Cluster papers into 4-7 thematic groups based on extracted content
    - For each theme: name, description, paper_ids, key_methods, key_findings
    - Identify 3-6 research gaps:
      * What questions does the literature NOT answer?
      * What methods are missing?
      * What datasets would fill gaps?
      * Classify each gap as MAJOR (blocks progress) or MINOR (nice-to-have)
    - Compute coverage_score (0.0-1.0): how well does the literature address
      all facets of the research question?
    - Output: write themes.json + gaps.json

12. GATE: Read themes.json + gaps.json
    - ≥4 themes, each with ≥3 papers
    - ≥3 gaps, ≥1 classified as MAJOR
    - coverage_score is a float between 0.0 and 1.0
    If <4 themes → RE-DISPATCH with instruction to use broader clustering

# Phase 6.5: Novelty Baseline Computation (INTEGRATED — No Additional Searches)

This phase computes the NoveltyBaseline from existing Phase 5 extraction data.
It requires NO additional literature searches — all data comes from extractions.json.

**Dispatch**: Can be computed inline during Phase 6 or as a quick haiku subagent.

**Input**: extractions.json from Phase 5, themes.json from Phase 6

**Computation**:
1. **Gene frequency**: For each gene mentioned in any extraction's `genes_mentioned` field
   (or extracted via regex from `key_findings` text), compute the fraction of total
   screened papers that mention it.
   `gene_frequency[gene] = papers_mentioning_gene / total_screened_papers`

2. **Theme saturation**: For each theme from Phase 6, compute a saturation score:
   `theme_saturation[theme] = len(theme.paper_ids) / total_screened_papers`

3. **Canonical pathway genes**: Use the top-20 most-frequent genes as the
   `canonical_pathway_genes` list (MCP pathway calls are optional and not required).
   If MCP tools are available, 1-2 calls to `get_pathways_for_gene` or
   `search_reactome_pathways` for the top 5 genes can enrich this list.

4. **Expected convergence Jaccard**: Sample 100 random gene sets of size K (where K =
   median gene panel size from the top themes, or len(universe)//3) from the
   topic_gene_universe. Compute pairwise Jaccard for each random pair. The mean is the
   expected_convergence_jaccard. Convergence above this baseline is meaningful.

5. **Topic gene universe**: Union of all genes mentioned across all screened paper extractions.

**Output**: Write `novelty_baseline.json` to output_dir (shared/ for multi-team).
The field is also embedded in `lit_review.json` as `novelty_baseline`.

**Gate**: `novelty_baseline.json` exists; `gene_frequency` has >= 10 entries;
`topic_gene_universe` has >= 20 genes.

**On failure**: Proceed without novelty baseline (downstream stages handle None gracefully).
This is a soft gate — missing novelty baseline does NOT block the pipeline.

# Phase 7: Synthesis
13. Dispatch opus subagent with:
    - screened_papers.json, extractions.json, themes.json, gaps.json
    - novelty_baseline.json from Phase 6.5 (if present)
    - coverage_score from Phase 6
    - Research question
    - Task: Produce final LitReviewOutput:
      * summary: 500-800 word narrative synthesis of the field
      * screened_papers: full paper list with scores and extractions
      * themes: themed clusters with cross-references
      * structured_gaps: research gaps with severity and suggested approaches
      * coverage_score: from Phase 6
      * novelty_baseline: embed the NoveltyBaseline object if computed in Phase 6.5
      * key_datasets: consolidated list of datasets mentioned across papers
      * methodological_landscape: summary of methods used in the field
    - Write lit_review.json (structured) + lit_review.md (human-readable)

14. GATE (FINAL): Read lit_review.json
    - ≥30 papers in screened_papers
    - ≥4 themes with paper cross-references
    - ≥3 gaps with severity classifications
    - coverage_score present
    - summary is ≥400 words
    - No papers with unverified DOIs in final output
    - [ ] Phase 6.5: novelty_baseline.json exists with ≥10 gene frequency entries (SOFT — proceed without if absent)
```

For multi-team shared review: run the 7-phase pipeline once, save all outputs to `shared/`.
Each team's subsequent stages receive the same lit review.

## Validation Gate (summary — detailed gates are per-phase above)

- [ ] All 7 phases executed as separate subagent dispatches (not collapsed)
- [ ] Phase 2: MCP search tools called, ≥100 raw candidates, ≥3 APIs used
- [ ] Phase 3: ≥40 papers screened (or ≥25 with lowered threshold)
- [ ] Phase 4: `retrieve_full_text` called for ≥50 papers, `resolve_doi` called for DOI validation
- [ ] Phase 4: ≥35 papers with full text, ≥40 with verified DOIs
- [ ] Phase 5: ≥30 structured extractions with key_findings, methods, datasets
- [ ] Phase 6: ≥4 themes (≥3 papers each), ≥3 gaps (≥1 MAJOR), coverage_score computed
- [ ] Phase 6.5: novelty_baseline.json with ≥10 gene_frequency entries (SOFT — absent is OK)
- [ ] Phase 7: Final lit_review.json + lit_review.md with all fields populated (novelty_baseline embedded if Phase 6.5 ran)
- [ ] No fabricated DOIs in final output (Phase 4 DOI validation catches these)

## Anti-Collapse Enforcement

**The main context MUST verify after EACH phase that the output file exists and meets the
phase gate before dispatching the next phase.** If a phase output is missing or insufficient,
re-dispatch that phase — do NOT proceed.

## Multi-Team Note

For multi-team shared review: the 7-phase pipeline runs ONCE and outputs are saved to
`{experiment_dir}/shared/`. Each team's Hypothesis Generation, Approach Generation, and
subsequent stages all receive the SAME lit review. Do not re-run lit review per team.

## API Keys Affecting Lit Review Performance

- `NCBI_API_KEY` / `NCBI_EMAIL` — increases PubMed rate from 3 to 10 req/sec
- `SEMANTIC_SCHOLAR_API_KEY` — increases S2 rate limit
- `PERPLEXITY_API_KEY` — required for Perplexity Sonar search (Phase 2 grey lit)
- `ELSEVIER_API_KEY` / `SPRINGER_API_KEY` — enhances full-text retrieval (Phase 4)

If `PERPLEXITY_API_KEY` is missing, skip `search_perplexity` calls in Phase 2.
