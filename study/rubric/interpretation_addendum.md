# Restart v2 candidate interpretation addendum

Status: candidate implementation for operator review. This addendum is not an
operator acceptance of the scoring/citation proposal and supplies no live
authorization.

## Precedence and scope

Apply the frozen adjudication prompt and the frozen rubric exactly. For this
candidate only, the rules in this addendum take precedence over a conflicting
interpretation in those frozen instructions for S1–S4 and C1–C3. The addendum
does not add a score, finding category, batch verdict, pass threshold, weight,
cap, or outside evidence requirement. Kimi owns semantic judgments. The
deterministic validator only checks the response contract, arithmetic, citation
identity/exactness, and the explicit S1 contradiction; it never changes a
score, finding, quotation, or locator.

## Proposed semantic interpretations

**S1 — partial support prevents Correctness 5.** A nonempty
`partly_supported_selected_states` or `unsupported_selected_states` list rules
out `correctness_score: 5`. The relevant limitation must be named in
`correctness_evidence.strongest_defect`; it cannot be `none`. This does not
assign a replacement score. A limited secondary defect may still support 4,
while a material selection or reasoning defect may support 3 or lower under
the frozen anchors.

**S2 — use the accepted entry's actual requirements.** For every selected or
potentially missed state, distinguish evidence of its mechanism and applicable
scope from topical resemblance. Use qualitative evidence only when the accepted
entry supports that inference. Require a named statistical measurement when
the state requires it for establishment; a measurement mentioned in a research
signature does not by itself create that requirement. A qualitative activation
cue does not by itself establish a full state. Explain the decisive present or
absent evidence. When the accepted material leaves a boundary unresolved,
disclose that ambiguity instead of inventing a universal threshold or exception.

**S3 — keep Correctness and Fidelity distinct.** Assess an unsupported
affirmative match under Correctness. Assess preservation of meaning, missed
states, and movement from an evidenced mechanism to topical resemblance under
Fidelity. A defect may affect both axes only when its separate consequence for
each axis is explained. An incorrect match does not automatically force
Fidelity 2, and accurate transcription does not excuse a material distortion of
meaning in the final answer. Apply the frozen Fidelity anchors, ceilings, and
all-31-state coverage check.

**S4 — resolve the Correctness 1/2 wording.** Treat the frozen rule that a
matched result with no supported selected state is no higher than 2 as a
ceiling. Use 1 for fundamental failure of the central analysis and 2 when
meaningful valid analysis remains despite an unsupported matched outcome. The
rationale must explain the distinction. A fabricated central claim remains a
1-level failure under the frozen anchor.

## Candidate citation contract

**C1 — one quotation is one contiguous excerpt.** After the returned JSON is
decoded, `quote` must be an exact contiguous substring of the named source
range. Preserve punctuation, case, whitespace, escape characters, and line
breaks. Do not normalize whitespace, insert ellipses, omit intervening fields,
or stitch separate excerpts into one quotation. A literal ellipsis already in
the source is allowed. Put paraphrases in explanations. The comparison source
is the exact UTF-8 text in the corresponding evidence block, not a parsed and
reserialized object.

**C2 — source-relative locations.** Use inclusive, one-based source-relative
line ranges in the form `L12-L16`; the range is within the named source block
and excludes the outer prompt heading and fence. For an enriched Market Book
citation use
`state_id=STATE_ID; entry_id=ENTRY_ID; L12-L16`, adding
`; linked_record_id=RECORD_ID` when the quotation is inside a linked record.
The entry and linked-record identity and the range must describe the same
object scope. A location identifies evidence and does not establish that the
evidence supports a semantic conclusion.

The following block is a deterministic navigation aid. It is not source
content and is never eligible for quotation. The source blocks in the frozen
prompt are presented with a readable one-based gutter: every displayed source line starts
with exactly `[L<n>] `, followed by the original line content. The gutter is a
presentation marker only. Remove exactly that first marker from each displayed
line and concatenate the remaining strings to reconstruct the exact source
text, including indentation, blank lines, source newline conventions, Unicode,
and a missing final newline. The raw UTF-8 bytes named by the frozen evidence
manifest and their hashes remain the citation authority.

===== BEGIN RESTART V2 SOURCE LOCATION AIDS (NOT QUOTABLE) =====
{{SOURCE_LOCATION_AIDS_JSON}}
===== END RESTART V2 SOURCE LOCATION AIDS (NOT QUOTABLE) =====

The location aid gives source hashes, displayed-source hashes, line counts, and
Market Book object spans. It deliberately omits UTF-8 byte-offset arrays from
the model-facing prompt. Use the visibly numbered source content block for
navigation, then quote the exact raw source text in the indicated range. The
displayed line labels must not appear in `quote`.

**C3 — deterministic exactness.** A valid response must cite a delivered
source, an existing inclusive line range, an exact quotation occurring inside
that range, and the required Market Book state/entry/linked-record identity in
the cited object scope. A failed check is a response defect. Never repair a
quotation, locator, or identity and never turn a failed response into a valid
one.

## Synthetic contract examples

These examples are synthetic illustrations only; they are not evidence from a
case and must not be cited.

Valid multiline quotation after JSON decoding:

```json
{"source":"original_commentary","locator":"L2-L3","quote":"alpha\nbeta"}
```

Invalid stitched quotation:

```json
{"source":"original_commentary","locator":"L2-L4","quote":"alpha ... beta"}
```

Invalid Market Book scope because the entry identity is absent from the cited
object:

```json
{"source":"enriched_market_book","locator":"state_id=state_demo; entry_id=entry_other; L8-L9","quote":"exact state text"}
```

Invalid S1 response because a selected-state limitation is listed while the
Correctness score and strongest defect claim no defect:

```json
{"correctness_score":5,"correctness_evidence":{"strongest_defect":"none"},"findings":{"partly_supported_selected_states":[{"subject":"state_demo"}]}}
```
