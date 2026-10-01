# Phase 8 rubric — correctness audit of the existing connector answers

Status: candidate for independent plan review and operator acceptance.

This rubric grades the 50 answers already produced by the blind-batch
experiment. It does not rerun the connector, generate replacement answers, or
judge trading decisions. Kimi K3 applies this same rubric independently to one
existing case per fresh headless-CLI session. GPT-5.6-Sol specifies the
evidence, implements the deterministic tooling, and orchestrates approved work
but does not semantically grade its own connector outputs.

The examples in `connector/experiments/guidelines/` informed two principles:
substance must outweigh polish, and every score needs evidence. Their
two-answer comparison method and forced preference scale do not fit this task.
This is therefore a purpose-built, single-answer, three-axis rubric.

## What is being graded

For one case, Kimi receives the original commentary, the builder packet,
retrieval artifacts, evaluator output, public view, run provenance, and the
accepted/enriched material for all 31 Market Book states. Kimi judges whether
the retained connector answer correctly and faithfully analyzed that
commentary using the Market Book that existed for the experiment.

Aggregate batch statistics, topic expectations, match frequency, original
confidence labels, and Phase 6 repeatability results are not evidence that an
individual answer is correct.

## The three score axes

Each axis receives one integer score from 1 through 5. There are no subsidiary
letter grades and no separate `good` / `mixed` / `bad` judgment.

### Correctness — 45%

Question: Are the connector's affirmative conclusions and supporting reasons
correct?

Check:

- whether each selected Market Book state is supported by the commentary and
  by the requirements of the accepted state;
- whether the stated mechanism, direction, timing, and causal relationship are
  correct;
- whether factual and causal claims are supported by the supplied evidence;
- whether uncertainty is represented honestly; and
- for a `matched` result, whether at least one selected state is genuinely
  supported.

Do not score missed applicable states here. They belong under Fidelity.

| Score | Correctness anchor |
|---:|---|
| 5 | The central conclusion and material reasoning are correct. Every selected state is supported, and there is no material factual or causal error. |
| 4 | The central conclusion is correct. A limited imprecision, weak secondary claim, or partly supported detail exists, but it does not change the answer's meaning or outcome. |
| 3 | The answer contains meaningful correct analysis, but one material selection or reasoning problem requires correction. The answer remains partly usable. |
| 2 | A major conclusion, selected state, or core reasoning step is unsupported or wrong. Some valid analysis remains, but the answer is unreliable without major correction. |
| 1 | The central analysis fails: it is fabricated, contradicted by the evidence, has no valid selected state behind a `matched` result, or is fundamentally inconsistent with the commentary and Market Book. |

### Fidelity — 40%

Question: Did the connector preserve the commentary's meaning and cover the
Market Book states that the commentary actually supports?

Check:

- whether the builder preserved facts, direction, timing, magnitude, scope,
  qualifications, and uncertainty;
- whether the builder invented, reversed, or materially omitted information;
- whether any of the 31 accepted Market Book states clearly applies but is
  absent from the final selected states;
- whether a missed state was absent from retrieval candidates (a retrieval
  miss) or present in candidates but not selected (an evaluator miss); and
- whether the connector respected the boundary between a real mechanism match
  and a merely topical resemblance.

| Score | Fidelity anchor |
|---:|---|
| 5 | The commentary's material meaning is preserved and all clearly applicable Market Book states are covered. |
| 4 | Meaning is preserved. There is only a minor omission or missed secondary nuance that does not affect the central analysis or outcome. |
| 3 | The original meaning remains recognizable, but one material omission, distortion, or missed state weakens the analysis. |
| 2 | A major distortion or important missed state changes the practical meaning or makes the recorded outcome unreliable. |
| 1 | The answer no longer represents the commentary, or it misses the commentary's central applicable Market Book mechanism. |

Missed-state calibration:

- a minor missed secondary nuance with no effect on the main analysis is
  normally Fidelity 4;
- a material missed state where the final outcome remains defensible is no
  higher than Fidelity 3;
- a missed state that makes a `no_match` outcome wrong is no higher than
  Fidelity 2; and
- a central missed mechanism combined with material distortion may be
  Fidelity 1.

### Quality — 15%

Question: Is the answer clear, specific, properly calibrated, and useful to a
human reader?

Quality is deliberately the least important axis. It cannot rescue incorrect
or unfaithful analysis.

| Score | Quality anchor |
|---:|---|
| 5 | Clear, specific, well calibrated, and immediately understandable and usable. |
| 4 | Clear and useful, with one small presentation, precision, or organization issue. |
| 3 | Understandable but generic, uneven, repetitive, or insufficiently explicit about limits. |
| 2 | Confusing, vague, poorly organized, or difficult to use reliably. |
| 1 | Incoherent or unusable. |

## Weighted score and core-failure caps

The uncapped weighted score is calculated deterministically:

```text
weighted_score = (Correctness x 0.45) + (Fidelity x 0.40) + (Quality x 0.15)
```

The following caps prevent polished writing from hiding a core analytical
failure:

- if Correctness or Fidelity is 1, the final score cannot exceed 1.99;
- if Correctness or Fidelity is 2, the final score cannot exceed 2.99;
- Quality never creates a cap;
- a fabricated central claim requires Correctness 1;
- an outcome-changing missed state requires Fidelity no higher than 2;
- a material builder distortion that changes the analysis requires Fidelity no
  higher than 2; and
- a `matched` result with no supported selected state requires Correctness no
  higher than 2.

The final score is the lower of the weighted score and the applicable cap,
recorded to two decimal places. If both core axes trigger caps, use the lower
cap and record that both axes triggered it.

These ranges explain the meaning of the numeric result; they are not extra
verdict labels:

| Final score | Meaning |
|---:|---|
| 4.50–5.00 | Highly reliable; no material correction is needed. |
| 3.75–4.49 | Substantively strong; only limited correction is needed. |
| 3.00–3.74 | Materially mixed; human review is needed before relying on it. |
| 2.00–2.99 | Unreliable; major correction is needed. |
| 1.00–1.99 | Fundamental analytical failure. |

There is no invented experiment pass threshold. Phase 8 reports the score
distribution and the underlying defects; it does not retroactively declare the
original batch passed or failed.

## Required evidence and factual findings

For each of the three axes, Kimi must provide:

1. the integer score;
2. the strongest positive feature;
3. the strongest defect or `none`;
4. exact commentary evidence;
5. exact Market Book evidence where the judgment concerns a state or
   mechanism; and
6. why the score is not one point higher and not one point lower.

Kimi also records the following factual findings. These are traceable findings,
not additional grades:

- unsupported selected states;
- partly supported selected states;
- missed applicable states;
- retrieval misses;
- evaluator misses;
- material builder distortions; and
- unsupported factual or causal claims.

Every listed finding must identify the affected state or claim and cite the
source material that supports the finding. An empty finding list means none
were found; it must not mean the check was skipped.

## Required result fields

Every valid adjudication must contain:

- `correctness_score`: integer 1–5;
- `fidelity_score`: integer 1–5;
- `quality_score`: integer 1–5;
- the evidence and score justification required above for each axis;
- the seven factual-finding lists;
- `weighted_score`: the formula result to two decimals;
- `applied_cap`: `none`, `correctness`, `fidelity`, or `both`;
- `final_score`: the capped result to two decimals; and
- a concise overall explanation of the answer's principal strength and
  principal defect.

The deterministic validator recalculates `weighted_score`, checks any cap, and
rejects arithmetic or field-shape mismatches. Kimi supplies the semantic axis
scores and findings; it does not choose weights, alter the formula, or decide
aggregate totals.

## Consistency rules for Kimi

- Use this exact rubric and output shape for all 50 cases.
- Judge the case from its primary evidence, not from the expected topic bucket
  or the batch's outcome distribution.
- Do not reward agreement with the connector merely because it is agreement.
- Do not infer correctness from the connector's strength or confidence label.
- A boundary/off-book topic is not automatically `no_match`; judge the actual
  mechanism in the commentary.
- Within each axis, anchor the score to the most consequential evidenced defect
  rather than averaging several informal subgrades.
- Reserve 5 for an answer with no material problem on that axis. Use 4 for a
  limited non-outcome-changing problem, 3 for a genuine substantive weakness,
  and 1–2 for major or fundamental failure.
- If a required primary artifact is missing, do not invent a score. Return an
  invalid-evidence record so the governed run stops for an operator decision.
- If the evidence is genuinely ambiguous, explain the ambiguity and score the
  answer's handling of it; do not create an unplanned categorical verdict.
- A score without checkable citations, or a rationale that conflicts with its
  score anchor, is invalid.

## Role boundary

- GPT-5.6-Sol owns the semantic plan, rubric, prompt and evidence specification,
  deterministic evidence packaging, file validation, runner implementation,
  arithmetic summarization, narrowly necessary tests, approved orchestration,
  interpretation, and report wording.
- Kimi K3 owns the three semantic scores, cited findings, and case rationale.
- GLM-5.3 independently reviews the plan/rubric, checks pre-run conformance,
  and, after the audit, checks the records, calculations, material cases, and
  fixed control sample.
- Egemen Candir is the sole operator/arbiter: he accepts the plan, authorizes
  paid model execution, resolves disputed judgments, and accepts or rejects
  the final result.

No Claude model is assigned any new work. Existing Claude-compatible support
is not removed or changed.
