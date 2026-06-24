# Part 1 Analysis Checkpoint

Generated: 2026-06-24T00:02:14.898715+00:00

## Status

Part 1 is complete and ready for visualization. No dashboard UI files were created or modified in this checkpoint.

## Inputs and contributor selection

- Source repository analyzed: `PostHog/posthog`.
- Current window used for the reconstructed impact cohort: merged PRs from `2026-05-24` through `2026-06-23`.
- Cached GitHub input: `data/github_cache/recent_merged_prs_search_30d.json` plus per-PR bundles in `data/github_cache/raw_prs/`.
- Important limitation: the attached assessment repository contained only `README.md`, not the prior dashboard or existing pipeline output. To satisfy the checkpoint, I reconstructed the current top contributors with a deterministic impact proxy using merged PR recency, title/body/label signal, review discussion count, high-leverage keywords, and then recalculated selected PR impact points after fetching file/review evidence.

| Impact rank | Contributor | Reconstructed impact total | Reviewed PRs | Quality score | Confidence | Reviewed PR numbers |
|---:|---|---:|---:|---:|---|---|
| 1 | `Gilbert09` | 3129.07 | 8 | 82.8 | high | #65534 (reliability, 83.0), #65503 (migration, 86.5), #65281 (reliability, 84.5), #62913 (performance, 88.0), #65583 (reliability, 80.0), #65394 (migration, 81.5), #65505 (reliability, 71.0), #61146 (product, 88.0) |
| 2 | `pauldambra` | 1171.0 | 8 | 79.2 | high | #63882 (performance, 78.5), #64999 (reliability, 88.0), #63443 (migration, 88.0), #65059 (migration, 77.0), #63883 (performance, 74.5), #65502 (reliability, 79.0), #64557 (infra, 72.5), #65588 (bugfix, 75.5) |
| 3 | `sampennington` | 919.53 | 8 | 82.7 | high | #64827 (reliability, 86.5), #65295 (migration, 84.5), #64681 (migration, 78.5), #64901 (migration, 83.5), #63907 (migration, 86.5), #64311 (infra, 83.0), #65144 (bugfix, 76.0), #61840 (migration, 82.0) |
| 4 | `andrewm4894` | 835.58 | 8 | 79.5 | high | #65075 (reliability, 78.0), #63493 (reliability, 90.0), #62394 (performance, 87.5), #62058 (reliability, 88.0), #64710 (reliability, 72.0), #57760 (product, 88.0), #65550 (bugfix, 75.0), #65527 (maintenance, 63.0) |
| 5 | `rnegron` | 535.66 | 8 | 80.5 | high | #64027 (reliability, 88.0), #64123 (reliability, 80.0), #64351 (reliability, 83.5), #64117 (product, 80.0), #64715 (reliability, 78.0), #62699 (performance, 80.5), #59440 (migration, 87.5), #65485 (dx, 66.0) |
| 6 | `webjunkie` | 521.36 | 5 | 77.7 | high | #64207 (migration, 89.0), #65432 (migration, 86.5), #65222 (reliability, 80.5), #64479 (product, 86.5), #65369 (dx, 69.0) |
| 7 | `skoob13` | 520.31 | 5 | 73.1 | high | #65463 (reliability, 82.0), #65442 (reliability, 78.0), #64792 (performance, 68.0), #65435 (migration, 81.5), #65552 (bugfix, 71.0) |
| 8 | `danielcarletti` | 431.47 | 5 | 83.8 | high | #64933 (migration, 86.5), #64660 (migration, 86.0), #65329 (migration, 87.5), #64967 (migration, 78.5), #65458 (bugfix, 83.5) |
| 9 | `jonmcwest` | 407.41 | 5 | 76.9 | high | #64118 (migration, 84.5), #64000 (migration, 80.0), #64885 (reliability, 79.5), #65119 (product, 77.5), #65118 (product, 70.0) |
| 10 | `MattPua` | 379.28 | 5 | 83.1 | high | #65507 (reliability, 85.0), #63614 (performance, 86.5), #63066 (reliability, 83.0), #65539 (bugfix, 71.0), #65538 (product, 71.0) |

## Rubric used

Each reviewed PR was scored from 0 to 5 on the exact requested dimensions, then converted to a 0-100 weighted PR score:

- Problem Importance: 20%
- Technical Soundness: 20%
- Risk Handling and Reliability: 15%
- Review Quality and Collaboration: 15%
- Test and Validation Strength: 10%
- Area Leverage and Ownership: 10%
- Communication Quality: 10%

Contributor quality scores use the requested weighting:

- 45% weighted PR quality average, with single-PR contribution capped at 25% of reviewed-PR weight
- 20% quality consistency
- 15% review leverage quality
- 10% ownership signal
- 10% communication quality average

## Scoring distribution

- Reviewed PRs: 65
- Min / median / mean / max PR quality: 63.0 / 81.5 / 80.7 / 90.0
- PRs >= 80: 40
- PRs 70-79.9: 21
- PRs 60-69.9: 4
- PRs < 60: 0

## Major uncertainties

- The original mechanical impact model was unavailable in this repository, so `existing_impact_points` and impact ranks are reconstructed rather than imported.
- GitHub public API data shows public PR discussions, file patches, labels, and linked issues; it does not capture private Slack/design discussion, internal incident context, production outcomes, or reviewer intent not written on GitHub.
- Some high-volume data-warehouse scaffold PRs are real merged work but repetitive; their mechanical impact can overstate novelty even when the implementation follows a useful pattern.
- Approval-only reviews are scored conservatively because they provide weaker evidence than inline design/risk discussion.
- Large PRs are assessed from GitHub file patches and metadata, not a full local checkout of PostHog/posthog at each merge SHA.

## API/data limitations

- GitHub search is capped, so the 30-day cohort was fetched by day (`merged:YYYY-MM-DD`) to avoid the 1,000-result search ceiling.
- Review examples use `reviewed-by:` search plus visible review/review-comment bodies; private or deleted comments are not visible.
- Linked issue fetching is limited to explicit `fixes/closes/resolves` references and direct `github.com/PostHog/posthog/issues/...` links found in PR text/comments.
- Per-PR file evidence uses the GitHub Pull Request Files API, capped by pagination into cached bundles.

## Readiness for visualization

Ready for Part 2 visualization: yes.

The required files are present:

- `data/qualitative_pr_reviews.json`
- `data/contributor_quality_scores.json`
- `data/quality_assessment_sources.json`
- `PART_1_ANALYSIS_CHECKPOINT.md`

Suggested second pass before treating this as anything stronger than directional dashboard data:

- Reconcile reconstructed impact ranks against the original pipeline output if it becomes available.
- Manually spot-check the highest-volume connector/scaffold PRs for `Gilbert09` and `danielcarletti`.
- Add production/incident outcome context where available for reliability/security/performance PRs.
