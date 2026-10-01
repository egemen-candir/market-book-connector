# Blind-batch commentary study

## Summary

The connector processed 50 model-written market commentaries. It returned 24 matched and 26 no-match outcomes. Kimi K3 then scored the existing answers with the frozen correctness-audit rubric: 24 cases scored 2.00–2.99 and 26 scored 4.50–5.00. The operator accepted the audit on 23 September 2026 with the qualifications recorded below.

This package contains the [commentaries](commentaries/), [rubric](rubric/rubric.md), [interpretation addendum](rubric/interpretation_addendum.md), and [per-case results](results.csv). It presents the retained scores and the reviewers' recorded conclusions.

## Design

GLM-5.3-flash wrote 50 commentaries across four reporting buckets: stress, calm, rotation, and boundary/off-book. The generation instructions asked for first-person, present-tense commentary from inside the supplied historical situation, without hindsight, headings, or references to the generation task. Plausible period-appropriate specifics were allowed.

The commentaries ran through the dashboard with gpt-5.6-sol at high reasoning as both observation-packet builder and Market Book evaluator, using fresh CLI processes against the frozen catalogue and index. The topic expectations were weak priors for reporting, not ground truth or inputs used to alter pipeline behavior. The generator's instructions did not expose those buckets or expectations.

The original final report records a date-serialization failure in the deterministic exporter, followed by a repair and successful reruns of the affected inputs. The final batch processed all 50 inputs; the original failed attempts remain part of the experiment's history.

## Roles

| Role | Model |
| --- | --- |
| Wrote the 50 commentaries | GLM-5.3-flash |
| Ran the pipeline (builder and evaluator) | gpt-5.6-sol |
| Scored all 50 answers with the frozen rubric | Kimi K3 |
| Checked Kimi's scoring | DeepSeek V4.1 Flash |
| Reviewed 27 cases against the primary evidence | GLM-5.3 |
| Planning and orchestration | gpt-5.6-sol and Luna |

These are the recorded role assignments. The GLM review also records use of GLM-5.3-flash for its continuation and GLM-5.3 for gap completion. Model and session changes remain disclosed procedural facts.

## Scoring method

The frozen rubric gives each axis an integer score from 1 through 5:

| Axis | Weight | What it checks |
| --- | ---: | --- |
| Correctness | 45% | Support for affirmative conclusions, selected states, and factual or causal reasoning |
| Fidelity | 40% | Preservation of commentary meaning, coverage of applicable states, and the boundary between mechanism and topical resemblance |
| Quality | 15% | Clarity, specificity, calibration, and usefulness to a human reader |

The weighted score is correctness × 0.45 + fidelity × 0.40 + quality × 0.15. If correctness or fidelity is 1, the final score has a ceiling of 1.99; if either is 2, the ceiling is 2.99. Quality does not create a cap. The final score is the lower of the weighted score and the applicable ceiling, recorded to two decimal places.

The addendum clarifies partial support, each accepted state's actual requirements, separate consequences for correctness and fidelity, and the distinction between major and fundamental failure. It also specifies exact quotations and source locations. See [the rubric](rubric/rubric.md) and [the addendum](rubric/interpretation_addendum.md). Their historical status and workflow wording are preserved unchanged; the audit's later acceptance is described under Review.

The retained backend profile specifies `kimi-code/k3`, high thinking, prompt-file delivery, stdout/text output, disabled tools/extensions/skills/rules, and fresh sessions. It specifies a 1,800-second command timeout, termination followed by forced kill after 10 seconds, and a 1,830-second adapter timeout. These are configuration settings; the acceptance record does not independently attest the served model or the original execution bindings.

## Results

### Connector outcomes

| Bucket | Matched | No match | Total |
| --- | ---: | ---: | ---: |
| Stress | 10 | 8 | 18 |
| Calm | 7 | 3 | 10 |
| Rotation | 2 | 8 | 10 |
| Boundary/off-book | 5 | 7 | 12 |
| **Total** | **24** | **26** | **50** |

The original batch report describes the matched/no-match split as a behavior distribution. The later correctness audit supplies the scores below. Neither report claims a universal accuracy rate.

### Final-score distribution

| Final-score range | Cases | Share |
| --- | ---: | ---: |
| 4.50–5.00 | 26 | 52.00% |
| 3.75–4.49 | 0 | 0.00% |
| 3.00–3.74 | 0 | 0.00% |
| 2.00–2.99 | 24 | 48.00% |
| 1.00–1.99 | 0 | 0.00% |

Mean final score: **3.74**; median: **4.93**; observed range: **2.30–5.00**. All 50 retained responses were valid and scored; missing, malformed, and evidence-invalid response counts were zero for this retained batch.

### Axis distributions

| Score | Correctness: cases | Fidelity: cases | Quality: cases |
| ---: | ---: | ---: | ---: |
| 1 | 0 | 0 | 0 |
| 2 | 24 | 19 | 0 |
| 3 | 0 | 5 | 0 |
| 4 | 0 | 0 | 25 |
| 5 | 26 | 26 | 25 |
| **Mean** | **3.56** | **3.66** | **4.50** |

Twenty-four cases triggered a 2.99 ceiling: five through correctness alone and nineteen through both correctness and fidelity. No ceiling reduced a weighted score; all were already below the applicable ceiling. In results.csv, applied_cap names the triggering axis or axes, and cap_ceiling records the numeric ceiling or none.

### Kimi's retained findings

| Finding | Items | Cases with a finding |
| --- | ---: | ---: |
| Unsupported selected states | 30 | 19 |
| Partly supported selected states | 11 | 11 |
| Missed applicable states | 0 | 0 |
| Retrieval misses | 0 | 0 |
| Evaluator misses | 0 | 0 |
| Material builder distortions | 0 | 0 |
| Unsupported factual or causal claims | 0 | 0 |

Case counts can overlap. Empty finding categories report what the retained audit found within its evidence boundaries.

### State concentration

The 24 matched commentaries produced 41 matched-state occurrences. The two most frequently selected states account for 24 of those 41 occurrences:

| State ID | Occurrences |
| --- | ---: |
| `state_nguyen_three_state_hmm_001` | 14 |
| `state_hurst_tsmom_trend_following_001` | 10 |
| `state_hamilton_arch_volatility_clustering_004` | 7 |
| `state_ang_bekaert_regime_params_001` | 6 |
| `state_ang_bekaert_bear_correlation_002` | 2 |
| `state_billio_financial_sector_comovement_001` | 1 |
| `state_cho_engle_market_conditions_004` | 1 |

The original report treats concentration as a diagnostic for correctness review, without concluding from frequency alone that a state is overused.

### Per-case table

The [results.csv](results.csv) follows the frozen case order. It contains the outcome, selected state IDs, Kimi's axis and final scores, cap information, finding counts, and GLM review coverage. State IDs are separated by semicolons; a no-match row has an empty state-ID field. Coverage is mandatory, control, both, or mechanical only. Both means the case belongs to both the mandatory and control sets. Mechanical only does not imply a primary-evidence judgment by GLM.

## Review

The GLM-5.3 review covered 27 distinct cases against primary evidence: 24 mandatory cases plus a fixed ten-case control sample with seven overlaps. Its corrected completion conclusions affirm all 27 retained conclusions, 26 without reservation and Case 11 with the limitation below. The other 23 cases received mechanical checks of identities, validation status, and arithmetic, without case-by-case semantic affirmation.

The GLM review describes faithful observation packets and generally requirement-grounded rejections. It identifies a recurring weakness in affirmative weak matches based on topical resemblance when a state's defining measurements are absent. This is the review's interpretation, already reflected in Kimi's retained scores and findings.

**Case 11 reservation.** For `blob_11_evergrande_fear`, GLM supports rejecting the Forbes state but considers `direction_discordant` debatable: the commentary lacks the necessary correlation measurements, yet its narrative is not necessarily directionally contrary to the Forbes finding. GLM says no scoring anchor or score depends on that label. The operator retained the overall matched outcome and final score **2.30**, with this explanation reservation.

DeepSeek V4.1 Flash checked Kimi's scoring against the frozen rubric and retained evidence, including findings, state coverage, citations, and arithmetic. Its reports and correction supplements record checks of Kimi's judgments. Kimi remains the study's scorer. Their disclosed delivery defects, early comparisons, and exposure to prior results remain part of the review history.

On **23 September 2026**, the operator accepted the review step with qualifications, retained Case 11's outcome and score with the reason-code reservation, and accepted the audit. The completion report says this measures the connector with inputs that are not tightly controlled, without expecting exceptional scores on every case.

The acceptance carries these qualifications:

- Historical GLM delivery ranges and tool-output locators remain unverified independently. Repository checks establish source identities and metadata, not what the reviewer actually saw.
- The GLM flash continuation used a new OMP session while reusing an earlier backup; its handoff assumed the existing session remained available.
- The Kimi OMP workflow has no independent remote served-model attestation or original runner execution bindings. Local metadata and self-reported identity have narrower evidentiary value.
- Earlier delivery failures, early comparisons, exposure to prior results, model/session changes, and recorded role departures remain disclosed. Corrective completion does not restore historical blindness or independence; exposure alone establishes neither influence nor its absence.

The frozen numerical summary still carries its earlier pending-acceptance status. The later operator completion report records acceptance without rewriting that summary.

## Repeatability

The original Phase 6 probe selected eight cases: one original match and one original no-match from each bucket. Each received one fresh dashboard submission.

| Measurement | Agreement/comparability |
| --- | ---: |
| Healthy, comparable reruns | 7/8 |
| Public outcome stable | 6/7 comparable |
| Matched-state set stable | 5/7 comparable |
| Confidence stable | 6/7 comparable |
| Match strengths stable | 5/7 comparable |

The report records an outcome change for `blob_33_energy_outperformance`, from no match to a weak Ang-Bekaert regime-parameter match. `blob_27_record_high_low_vol` stayed matched but added Hamilton volatility-clustering and Hurst trend-following states. The `blob_44_nickel_squeeze` rerun was not comparable: wording the event as “short squeeze” triggered the prohibited-phrase validator, whereas “position squeeze” had passed originally.

The Phase 6 GLM-5.3 review accepted the probe with recorded limitations. The final report states that fresh sessions still had filesystem access to prior results, producing a possible bias toward agreement of unknown size. The sample was diagnostic and stratified, without a recorded replayable random seed or witness. The first pass and rerun used different code states because of the serialization repair. Generator and reviewer belonged to the same model family, and wording-dependent validation removed a case from comparison. The report calls the observed agreement upper-biased; it does not provide a population stability estimate.

## Limitations

The completion report limits the audit to the existing answers and their evidence. It establishes neither a universal accuracy rate nor a release or trading decision. The batch's topic expectations were weak priors for reporting. The repeatability and review qualifications above remain part of the result.

The state IDs containing `nguyen` derive from Nicklas Werge (2021), not Nguyen. [CATALOGUE_ERRATA.md](../connector/catalog/CATALOGUE_ERRATA.md) records the correct attribution and other factual corrections. The audit judged the catalogue that existed for the experiment; publication preserves the identifiers.

An undated commentary's run date could be read as the market date. This was found before publication and fixed in the builder and evaluator prompts; the study's runs predate the fix.

## Reproducing

Use a file from [commentaries/](commentaries/) as input after following the main README's [Setup](../README.md#setup) and [Configure the model CLI](../README.md#configure-the-model-cli) sections. In the [Dashboard](../README.md#dashboard), choose Evaluate my commentary, paste the file's text, and select the builder and evaluator profiles.

For the CLI, follow the live-submission example under [Run the supplied mock example](../README.md#run-the-supplied-mock-example): replace my_commentary.md with your selected study commentary, configure the desired role profiles, and use a fresh output directory. The saved packet can be used to [re-derive its shortlist](../README.md#re-derive-a-runs-shortlist).

A new run is a new observation; fresh model outputs, changed prompts, and prior-result visibility can change it. The published table records the original answers and Kimi's retained audit, and is not rewritten by a new run.

