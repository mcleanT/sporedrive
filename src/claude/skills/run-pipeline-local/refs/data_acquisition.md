# Data Acquisition — Complete Protocol
> Include this file's content in the Stage 4 subagent dispatch prompt.

## Stage 4: Data Acquisition

**Prompt**: `prompts/data_acquisition.md`  |  **Model**: sonnet

**CRITICAL: Data Acquisition must attempt to download ALL datasets specified in the
approach output — not just one.** The #3 most common pipeline failure is silent fallback
to a small convenience dataset that wasn't in any team's approach. This produces valid
but scientifically weak analyses that can't cross-validate across technologies/timepoints.

---

## MCP Tools (Full List)

**Primary data sources**:
- GEO: `download_geo_matrix`, `download_geo_supplementary_file`, `download_geo_spatial`,
  `list_geo_supplementary_files`, `detect_geo_dataset_type`, `get_geo_sample_ids`,
  `list_geo_sample_files`, `download_geo_sample_file`
- CELLxGENE: `get_cellxgene_dataset`, `list_cellxgene_datasets`
- cBioPortal: `get_cbioportal_clinical_data`, `get_cbioportal_mutations`,
  `get_cbioportal_molecular_data`, `get_cbioportal_survival`, `search_cbioportal_studies`
- BioStudies/ArrayExpress: `get_biostudies_study`, `list_biostudies_files`, `download_biostudies_file`

**Additional data tools** (for multi-category sourcing):
- GDC/TCGA: `search_gdc_projects`, `get_gdc_project`, `get_gdc_clinical_data`, `search_gdc_files`, `download_gdc_file`
- Gene annotation: `lookup_ensembl_gene`, `get_ensembl_xrefs`, `get_genes_in_region`, `get_gene_sequence`
- Protein: `search_uniprot`, `get_uniprot_protein`, `get_protein_by_gene`
- Gene sets: `get_msigdb_gene_set`, `search_msigdb_gene_sets`, `get_msigdb_genes`
- Tissue expression: `get_gtex_expression`, `get_gtex_eqtl`, `search_eqtl_by_variant`, `get_gtex_tissues`
- Drug targets: `search_chembl_target`, `get_chembl_drug_info`, `get_chembl_bioactivities`, `get_chembl_target_by_gene`
- Genetic evidence: `search_open_targets`, `get_target_associations`, `get_target_drugs`, `get_genetic_evidence`
- Clinical: `search_clinical_trials`, `get_clinical_trial`, `get_trial_outcomes`, `search_adverse_events`, `get_drug_label`, `search_drug_interactions`
- Oncology: `get_oncokb_gene`, `annotate_oncokb_mutation`, `get_oncokb_curated_genes`, `get_oncokb_actionable_variants`
- Variants: `search_clinvar_variants`, `get_clinvar_variant`, `search_clinvar_by_variant`, `get_cosmic_signatures`, `get_cosmic_signature`, `match_cosmic_spectrum`
- LD/eQTL: `get_ld_proxy`, `get_ld_matrix`, `get_ld_pair`, `ld_clump_snps`
- Pathway/Protein: `get_protein_interactions`, `get_functional_enrichment`, `get_hpa_gene_info`
- GWAS: `search_gwas_by_trait`, `search_gwas_associations`, `get_gwas_study`

---

## Orchestration Protocol

1. Read approach output → extract the FULL dataset list (all accessions, all teams)
2. Build a **download manifest** listing every dataset with: accession, source (GEO/
   CELLxGENE/cBioPortal/etc.), expected size, role (primary/validation/orthogonal)
3. Dispatch sonnet subagent with the download manifest + MCP tools
4. Subagent attempts EACH dataset in order:
   - For GEO: `detect_geo_dataset_type` → `list_geo_supplementary_files` → download
   - For GEO with large RAW.tar: `get_geo_sample_ids` → `list_geo_sample_files` → `download_geo_sample_file` (bypasses archive)
   - For ArrayExpress (E-MTAB-*): `get_biostudies_study` → `list_biostudies_files` → `download_biostudies_file`
   - For CELLxGENE: `get_cellxgene_dataset`
   - For cBioPortal: `get_cbioportal_clinical_data` + `get_cbioportal_mutations` + `get_cbioportal_survival`
   - For GDC/TCGA: `search_gdc_projects` → `search_gdc_files` → `download_gdc_file`
   - For Open Targets: `search_open_targets` → `get_target_associations` / `get_genetic_evidence`
   - For ChEMBL: `search_chembl_target` → `get_chembl_bioactivities`
   - For GWAS: `search_gwas_by_trait` / `search_gwas_associations` → `get_gwas_study`
   - For Pathway/Protein: `get_protein_interactions` / `get_functional_enrichment` / `get_hpa_gene_info`
   - For Gene Sets: `get_msigdb_gene_set` / `search_msigdb_gene_sets`
   - For Variants: `search_clinvar_variants` / `get_oncokb_gene` / `annotate_oncokb_mutation`
   - For eQTL: `get_gtex_eqtl` / `get_ld_proxy`
   - Log EVERY attempt: accession, status (success/failed), error message if failed,
     file paths + sizes if succeeded
5. Write `data_manifest.json` with structured acquisition report (schema below)
6. Download validation datasets alongside primary ones:
   - Tag validation data in the manifest with `cohort_type: "validation"`
   - Save validation data to `{team_dir}/data/validation/`
   - Log validation dataset attempts in `data_manifest.json` alongside primary datasets
7. Save primary data to `{experiment_dir}/{team_dir}/data/` (per-team) or `shared/data/` (shared)

---

## data_manifest.json Schema

```json
{
  "attempted": [
    {"accession": "GSE...", "status": "success", "files": [...], "size_bytes": ...},
    {"accession": "E-MTAB-...", "status": "failed", "error": "404 not found", "files": []},
    ...
  ],
  "successful": ["GSE...", ...],
  "failed": ["E-MTAB-...", ...],
  "fallback_used": false,
  "total_files": 42,
  "total_size_bytes": 180000000
}
```

For validation datasets, each entry in `attempted` also carries `"cohort_type": "validation"`.

---

## Fallback Policy

If a specified dataset fails (404, too large, auth required):
- Log the failure reason in the manifest
- Try the next dataset in the list
- If ALL primary datasets fail, the subagent MAY search for an alternative using MCP
  search tools — but MUST document the substitution clearly in the manifest with
  `"fallback_used": true` and `"fallback_reason": "..."`
- **NEVER silently substitute a different dataset without logging it**

If a file exceeds 200MB (MCP tool limit), try supplementary files or subsets first.
Only fall back to a different dataset as a last resort, with explicit logging.

---

## Post-Acquisition Validation (Main Context, BEFORE Proceeding to Stage 5)

The main context MUST verify after data acquisition completes:
1. `data_manifest.json` exists and is valid JSON
2. Check `successful` vs. the approach's dataset list:
   - If ≥1 PRIMARY dataset succeeded → proceed (warn about missing validation sets)
   - If ALL primary datasets failed but fallbacks exist → WARN user before proceeding
   - If NO data downloaded at all → HALT and report
3. For each successful dataset: verify the file exists on disk and is non-empty
4. Report a clear summary to the user: "Downloaded X of Y datasets. Missing: [list]"

---

## Validation Dataset Handling

Approach Generation specifies 1-2 **validation datasets** — independent cohorts for
replicating key findings. These are marked with `is_validation: true` in the approach output.
Validation datasets should use similar data type/methodology but come from independent
samples (no overlap with primary cohort).

During data acquisition:
- Download validation datasets alongside primary ones
- Tag with `cohort_type: "validation"` in the manifest
- Save to `{team_dir}/data/validation/`
- Report any failures to download validation data (warn, but don't halt)

After analysis convergence (Stage 6, step 5b), validation analysis:
- Selects top-3 novel findings (p < 0.01, meaningful effect size)
- Applies same statistical tests to validation cohort
- Reports: replicated / partial / not_replicated / insufficient_power

---

## Multi-Team Data Acquisition

In multi-team mode, pass `other_team_focuses` to each team's Data Acquisition subagent:
- This enables teams to select DIFFERENT datasets for complementary coverage
- Team A may take GEO expression data; Team B takes cBioPortal clinical data
- Prevents all teams from downloading identical data (wasteful + reduces diversity)

---

## Validation Gate

- [ ] `data_manifest.json` written with per-dataset attempt/status/error logs
- [ ] ≥1 PRIMARY dataset from the approach downloaded (not just any random dataset)
- [ ] Downloaded files verified on disk (exist + non-empty)
- [ ] If fallback used: explicitly flagged with `fallback_used: true` + reason
- [ ] If primary datasets failed: user warned before proceeding
