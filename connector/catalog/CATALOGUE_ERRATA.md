# Catalogue errata

The research catalogue (`connector/catalog/market_book_catalog.stage6_enriched.json`)
and its search index (`connector/catalog/glossed/e5/catalog_embedding_index.npz`) are
frozen and hash-pinned: the pipeline checks their SHA-256 at run time. Known errors are
therefore corrected here instead of in the files. Each correction applies equally to
the catalogue text and to the same text in the index.

Record IDs are identifiers and are not renamed. Where an ID contains an author's name,
the correct credit is given below.

## 1. Werge (2021): records whose IDs contain `nguyen`

**Correct source:** Nicklas Werge (2021), *Predicting Risk-adjusted Returns using an
Asset Independent Regime-switching Model*, arXiv preprint 2107.05535v1.

Every record whose ID contains `nguyen` is derived from this paper. It is **not** by
Nguyen, and it is **not** Nguyen (2018), *Hidden Markov Model for Stock Trading*, which
Werge cites.

The records are:

- `entry_nguyen_three_state_hmm_001`
- `state_nguyen_three_state_hmm_001`
- the signatures `signature_nguyen_ewmm_span_calibration_001`,
  `signature_nguyen_hmm_regime_states_001` and `signature_nguyen_pesr_oos_sharpe_002`
- the claims `claim_nguyen_three_state_hmm_001`, `claim_nguyen_pesr_metric_002`,
  `claim_nguyen_oos_sharpe_003` and `claim_nguyen_span_tradeoff_004`
- the policies `policy_nguyen_hmm_regime_switching_001` and
  `policy_nguyen_pesr_position_sizing_001`
- the failures `failure_nguyen_hmm_state_count_001`,
  `failure_nguyen_oos_window_length_001`, `failure_nguyen_pesr_nonstationarity_001`
  and `failure_nguyen_span_turnover_001`

Besides the author credit, these records contain factual errors against the paper.
Page, section and table numbers below refer to the paper.

| # | Catalogue text says | The paper says | Affected records (ID suffix after `nguyen_`) |
| --- | --- | --- | --- |
| W1 | "Nguyen 2021" | Werge (2021) | state `three_state_hmm_001`; signatures `ewmm_span_calibration_001`, `hmm_regime_states_001`, `pesr_oos_sharpe_002` |
| W2 | 15 futures: 4 commodity, 4 currency, 4 equity, 3 fixed income | 3 commodity, 4 currency, 4 equity, 4 fixed income (§3 and Table 1, p.3) | state; signature `hmm_regime_states_001` |
| W3 | Three states "selected via greedy model selection" | Three states were chosen for interpretability (§5, p.5). The greedy approach is discussed in §2.4 (p.3) as an option; the paper does not report using it or comparing other state counts. | state; signature `hmm_regime_states_001`; claim `three_state_hmm_001`; policy `hmm_regime_switching_001`; failure `hmm_state_count_001` |
| W4 | State labels: bull (moderate volatility), bear (negative or flat return, elevated volatility), high-volatility (extreme variance) | States are labelled by the sign of their expected Sharpe ratio (ESR): bull positive, bear negative, high-volatility close to zero because volatility dominates (§5, p.5). The paper gives no "moderate/elevated/extreme" volatility levels. | state; signature `hmm_regime_states_001` |
| W5 | Features normalized and processed independently per instrument or asset class; HMM "trained per instrument" | The paper proposes one asset-independent HMM (abstract, p.1; §6, p.6), not separate models per instrument. | state; signature `hmm_regime_states_001`; claim `three_state_hmm_001`; policy `hmm_regime_switching_001` |
| W6 | Span 30 out-of-sample Sharpe ratios: EQ4 3.0, FI1 1.91, CO1 2.4 | Span 30 (Table 4, p.6): **EQ4 2.01, FI1 2.58, CO1 2.44**. The values 3.0 and 2.4 are the span 15 Sharpe ratios for EQ4 and CO1; 1.91% is FI1's span 15 **volatility**, not a Sharpe ratio. Buy-and-hold comparisons 1.0, 0.27 and 0.67 are correct (Table 3, p.5). | claim `oos_sharpe_003`; signature `pesr_oos_sharpe_002`; policy `pesr_position_sizing_001` |
| W7 | "EQ4: SR=3.0 vs. 1.0" given as the headline example | That is EQ4 at **span 15**; at span 30, the recommended setting, it is 2.01 vs 1.0 (Tables 3 and 4). | state; failure `oos_window_length_001` |
| W8 | Daily turnover about 1.64% to 4.87% at span 15, and about 2.54% at span 30 | Table 4, p.6: span 15 ranges **1.63% to 5.40%**; span 30 ranges **1.64% to 4.08%** (the paper: an investment horizon of about 25 to over 60 days). 2.54% is CO3 at span 60. | claim `span_tradeoff_004`; signature `ewmm_span_calibration_001`; failure `span_turnover_001`; policy `pesr_position_sizing_001` |
| W9 | A favorable span range of about 25 to 45, with Sharpe ratios degrading at both extremes | Only spans 15, 30 and 60 were tested (§5, p.5); no 25 to 45 range was tested. The effect of span differs by instrument (Table 4), so no uniform degradation at both ends is shown. | signature `ewmm_span_calibration_001`; failure `span_turnover_001` |
| W10 | Training period 2000 to 2015 | Training up to 2012, validation 2012 to 2016, test January 2016 to October 2019 (§5, p.5) | failure `pesr_nonstationarity_001` |
| W11 | Real-time or filtered regime probabilities and real-time regime identification | The reported results used the **entire test dataset** to infer the hidden-state sequence; incremental prediction using only past data is left as future work (§6, p.6). The reported out-of-sample performance is therefore not a real-time result. | signature `hmm_regime_states_001`; policy `hmm_regime_switching_001`; applies to every performance figure in this group |
| W12 | "Long-only strategy design ... extension to long/short requires separate validation" | The paper also tests a long/short strategy: higher absolute returns, higher volatility and a lower Sharpe ratio than long-only (Table 5 and §5.1, pp.5 to 7). | policy `hmm_regime_switching_001` |
| W13 | Long-only positions = max(0, PESR) | Holdings are capped at [0, 1] for long-only and [-1, 1] for long/short (§5, p.5). The reported strategies use a one-step-ahead forecast, PESR(1) (Tables 4 and 5). | signature `pesr_oos_sharpe_002`; policy `pesr_position_sizing_001` |

**Not from the paper.** Several texts in these records are the project's own
interpretation or advice, not findings of the paper:

- reading PESR magnitude as confidence;
- posterior-probability thresholds and cross-asset stress scaling;
- recalibration intervals;
- comments on the 2016 to 2019 market environment and on later years.

Read them as design suggestions.

**Correct as published:**

- the PESR and ESR definitions (§4.3, Equation 4.1, p.4);
- the 4,972 daily observations from January 2000 to October 2019;
- the tested spans {15, 30, 60};
- the 5 basis point transaction cost;
- the test period;
- the statement that span 30 gives a Sharpe ratio above 1 for most instruments
  (14 of 15; FX1 is 0.86).

## 2. Other attribution corrections

| # | Catalogue text says | Correct | Affected records |
| --- | --- | --- | --- |
| A1 | "Ang (2011)" | Andrew Ang and Allan Timmermann (2011), *Regime Changes and Financial Markets*, NBER Working Paper 17182; later published in *Annual Review of Financial Economics* 4 (2012), 313 to 337 | signature `signature_ang_2011_exceedance_asymmetry_003` |
| A2 | "Lo and MacKinlay (1988), Journal of Finance" | Andrew W. Lo and A. Craig MacKinlay (1988), *Stock Market Prices Do Not Follow Random Walks: Evidence from a Simple Specification Test*, **Review of Financial Studies** 1(1), 41 to 66. The project worked from NBER Working Paper 2168 (1987). | state of `entry_lo_mackinlay_short_horizon_predictability_001`; signature `signature_lo_mackinlay_variance_ratio_001` |

## 3. Source of these corrections

The corrections come from checking each record against the paper's text. For
section 1, both Werge (2021) and Nguyen (2018) were compared line by line, and the
records' specific details (data, formula references, tables) match only Werge. The
figures above were checked against the paper's Tables 1, 3, 4 and 5.
