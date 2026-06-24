# workweave-posthog-assessment

## Qualitative PostHog contributor quality data

Part 1 now produces dashboard-ready qualitative datasets for every non-bot contributor in the reconstructed PostHog merged-PR cohort from 2026-01-01 through 2026-06-23.

Generated files:

- `data/qualitative_pr_reviews.json` — scored representative PR reviews with rubric justifications and source URLs.
- `data/contributor_quality_scores.json` — contributor-level quality scorecards, coverage tiers, confidence, strengths, caveats, and review leverage examples.
- `data/quality_assessment_sources.json` — auditable source index for PRs, diffs, review comments, issues, and file evidence.
- `PART_1_ANALYSIS_CHECKPOINT.md` — checkpoint summary, rubric, coverage, scoring distribution, uncertainties, and visualization readiness.

Regenerate with:

```bash
python3 scripts/part1_quality_analysis.py
```

Raw GitHub cache lives under `data/github_cache/` and is intentionally gitignored.
