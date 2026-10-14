# Sanitized evaluation appendix

This appendix describes the offline QA and PR development evaluation summarized in the [case study](../CASE_STUDY.md#offline-evaluation). It publishes the rubric and result counts without private source files, original conversations, repository identities or raw model transcripts. Case IDs and source-family labels are anonymous. The included data supports checking the arithmetic, not reproducing model judgments without the private inputs.

## Tasks and inputs

- **QA:** 20 reconstructed tasks: 10 code questions and 10 document questions, including cross-document facts, numerical boundaries, missing information and contradictions. These were not exported user conversations.
- **PR review:** 20 tasks from nine source families: 10 historical changes and 10 regression cases reconstructed by reversing historical fixes. They were not 20 independent production PRs. Original PR descriptions were unavailable.
- **Inputs:** selected source and document files; PR tasks included pinned base/head files and diffs. This was a bounded offline corpus, not access to the full organization or deployment environment. Frozen reference answers and defect labels were withheld from the answering model.
- **Model:** gpt-5.6-terra, high reasoning. The original agent searched the selected corpus with tools. The same-model full-text baseline received all selected material without search tools. The revised PR workflow used a fresh exploration session followed by a fresh verification session. QA was evaluated separately in the original comparison; the PR workflow result is not a new QA measurement.

## Grading

Codex reviewed answers against frozen references and source evidence. Grading was unblinded, and there was no independent human review.

A QA task passed strictly only when it covered all required facts without a consequential unsupported claim. Reading a file alone was not sufficient evidence of a correct answer.

A valid PR finding had to identify an introduced or worsened problem, a viable trigger, meaningful impact, the correct location and a source-supported causal chain. Findings about the same root cause were deduplicated. Pre-existing behavior, style preferences and unsupported assumptions were invalid. Insufficient context was **unresolved**, and unresolved findings stayed in the precision denominator. A case with no findings does not have 100% precision; its finding-level precision is undefined.

Known-defect recall used a frozen set of 13 defects. Additional valid findings counted toward precision but did not enlarge the recall denominator.

## Results

| Measure | Original agent | Full-text baseline | Two-stage PR workflow |
| --- | ---: | ---: | ---: |
| QA strict pass | 19/20 (95%) | 20/20 (100%) | Not evaluated |
| Valid PR findings | 17 | 19 | 20 |
| Invalid PR findings | 5 | 3 | 2 |
| Unresolved PR findings | 4 | 3 | 2 |
| Valid / all PR findings | 17/26 (65%) | 19/25 (76%) | 20/24 (83%) |
| Frozen known-defect recall | 11/13 (85%) | 11/13 (85%) | 11/13 (85%) |
| Cumulative PR tokens, including cached input | 3,364,858 | 1,584,729 | 10,131,651 |
| Median PR wall time, seconds | 174.86 | 60.53 | 605.07 |

The revised workflow completed all 20 jobs and still missed the same two frozen defects. It used approximately 3.01 times the original agent's cumulative tokens and 3.46 times its median wall time. Token totals include cached input and are not a monetary bill. Runtime repairs, retries and scheduling also differed, so this is not an equal-compute comparison.

[results.json](results.json) contains the anonymous per-case finding counts for all three PR arms, frozen-defect hit counts, and the revised workflow's per-case tokens and time. To recompute the totals:

```sh
python3 evaluation/check_results.py
```

## Examples of grading decisions

These are paraphrases of the reviewed cases, with business details removed:

| Case | Judgment | Reason |
| --- | --- | --- |
| pr-02 | Valid | Removing a guard after input normalization let an empty filter become an unrestricted directory query. The changed path and resulting query supported the finding. |
| pr-09 | Invalid | A reported terminal-state transition was already disallowed in the base version. The claimed regression was not introduced by the change. |
| pr-18 | Unresolved | A helper's handling of a missing-value sentinel looked risky, but the provided source did not establish a caller that could trigger the claimed production failure. |

## What the comparison supports

It showed fewer unsupported findings on an exposed development set while preserving frozen-defect recall. That supported spending more compute on background review to reduce the developer's burden of checking noise. It did not quantify human time saved.

All cases were visible during development. The full-text baseline was retained from the original comparison and was not rerun alongside the revised full run. Cases share source families; they are not independent samples. Repeated-run stability, held-out generalization and a statistically established advantage were not demonstrated. Production finding acceptance and human time savings were not systematically measured. Engineering tests cover linked-PR context preparation and delivery, but this evaluation does not measure the cross-repository feature.
