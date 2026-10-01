"""Validator for Market Book Evaluations.

Deterministic validation -- no LLM calls.  Checks matched entries against
candidate sets, validates enum values, rejects trading language outside
exempt fields, and cross-references against catalog and packet data.
"""

import re
import sys
from pathlib import Path

from connector.tools.io_utils import load_yaml, now_utc_iso
from connector.tools.paths import vocab_dir
from connector.tools.validate_observation_packet import (
    find_prohibited,
    load_vocab,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MATCH_STRENGTH_ALLOWED = {"strong", "moderate", "weak", "none"}

_VERDICT_ALLOWED = {"matched", "no_match_not_in_book", "no_match_out_of_scope"}

_CITATION_KIND_ALLOWED = {
    "observable_signature",
    "failure_pattern",
    "policy_implication",
    "extracted_claim",
}

_REASON_CODE_ALLOWED = {
    "direction_discordant",
    "out_of_scope",
    "coincidental_neighbor",
    "family_sibling_rejected",
    "insufficient_evidence",
}

_CONFIDENCE_ALLOWED = {
    "well_supported",
    "partially_supported",
    "weakly_supported",
    "unsupported",
}

_REQUIRED_TOP_LEVEL = [
    "evaluation_id",
    "observation_id",
    "candidate_set_id",
    "verdict",
    "matched_states",
    "non_matches",
    "prohibited_actions",
    "overall_interpretation",
    "evaluation_version",
    "market_book_version",
]

_MATCHED_STATE_REQUIRED = [
    "entry_id",
    "state_id",
    "match_strength",
    "matched_observations",
    "citations",
    "evidence_caveats",
    "policy_posture",
]

_INCOHERENCE_MARKER_RE = re.compile(r"placeholder|TBD|TODO", re.IGNORECASE)


def _iter_coherence_text_fields(value, path: str = "evaluation"):
    """Yield text under reason fields and fields whose names contain caveat."""
    if isinstance(value, dict):
        for key, field_value in value.items():
            field_path = f"{path}.{key}"
            normalized_key = str(key).lower()
            if normalized_key == "reason" or "caveat" in normalized_key:
                yield from _iter_text_values(field_value, field_path)
            else:
                yield from _iter_coherence_text_fields(field_value, field_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_coherence_text_fields(item, f"{path}[{index}]")


def _iter_text_values(value, path: str):
    """Yield every string leaf in a selected coherence field."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_text_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_text_values(item, f"{path}[{index}]")


def _catalog_entry_map(catalog: dict) -> dict:
    """Return {entry_id: entry_dict}. Supports catalog 'entries' as a list or a dict."""
    entries = catalog.get("entries", [])
    if isinstance(entries, dict):
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    if isinstance(entries, list):
        return {
            e["entry_id"]: e
            for e in entries
            if isinstance(e, dict) and e.get("entry_id")
        }
    return {}


def _expected_kind_for_record(record_id: str) -> tuple[bool, str | None]:
    """Return (prefix_known, expected_kind) for *record_id* per F3.

    expected_kind is None when the prefix is known to be a gloss (state_*) --
    always invalid as a citation. prefix_known is False for an unrecognized
    prefix, meaning the kind-consistency check should be skipped.
    """
    if record_id.startswith("signature_"):
        return True, "observable_signature"
    if record_id.startswith("failure_"):
        return True, "failure_pattern"
    if record_id.startswith("policy_"):
        return True, "policy_implication"
    if record_id.startswith("claim_"):
        return True, "extracted_claim"
    if record_id.startswith("state_"):
        return True, None
    return False, None


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------

def validate_evaluation(
    evaluation: dict,
    *,
    packet: dict | None = None,
    candidate_set: dict | None = None,
    catalog: dict | None = None,
    vocab: dict | None = None,
) -> dict:
    """Validate an evaluation dict.

    Returns a report dict:
        {"valid": bool, "errors": [str], "warnings": [str],
         "checked_at_utc": str, "validator_version": "0.3"}
    """
    errors: list[str] = []
    warnings: list[str] = []

    if vocab is None:
        vocab = load_vocab()

    prohibited_phrases: list = vocab["prohibited_phrases"]
    prohibited_patterns: list = vocab["prohibited_patterns"]
    allowed_phrases: list = vocab["allowed_phrases"]

    # -- required top-level fields --
    for field in _REQUIRED_TOP_LEVEL:
        if field not in evaluation:
            errors.append(f"Missing required field: {field}")

    # -- market_book_version --
    if "market_book_version" in evaluation and evaluation["market_book_version"] != "stage6_accepted":
        errors.append(f"market_book_version must be 'stage6_accepted', got {evaluation['market_book_version']!r}")

    # -- observation_id / candidate_set_id consistency --
    if packet and candidate_set:
        if "observation_id" in evaluation and "observation_id" in packet:
            if evaluation["observation_id"] != packet["observation_id"]:
                errors.append(
                    f"evaluation.observation_id ({evaluation['observation_id']}) "
                    f"!= packet.observation_id ({packet['observation_id']})"
                )
        if "candidate_set_id" in evaluation and "candidate_set_id" in candidate_set:
            if evaluation["candidate_set_id"] != candidate_set["candidate_set_id"]:
                errors.append(
                    f"evaluation.candidate_set_id ({evaluation['candidate_set_id']}) "
                    f"!= candidate_set.candidate_set_id ({candidate_set['candidate_set_id']})"
                )

    # -- verdict enum + verdict<->matched_states<->no_match_rationale consistency --
    matched_states = evaluation.get("matched_states") or []
    if not isinstance(matched_states, list):
        errors.append("matched_states must be a list")

    if "verdict" in evaluation:
        verdict = evaluation["verdict"]
        if verdict not in _VERDICT_ALLOWED:
            errors.append(f"verdict unknown value: {verdict}")
        else:
            if verdict == "matched":
                if not matched_states:
                    errors.append("verdict is 'matched' but matched_states is empty")
            else:
                if matched_states:
                    errors.append(
                        f"verdict is '{verdict}' but matched_states is non-empty"
                    )
                rationale = evaluation.get("no_match_rationale")
                if not rationale:
                    errors.append(
                        f"verdict is '{verdict}' but no_match_rationale is missing"
                    )
                elif not isinstance(rationale, str) or not rationale.strip():
                    errors.append(
                        f"verdict is '{verdict}' but no_match_rationale is empty"
                    )

    # -- candidate linked records: entry_id -> set of source_record_id across
    #    its retrieval_reasons (F2). Used for citation membership checks.
    candidate_linked_records: dict[str, set] = {}
    candidate_state_by_entry: dict[str, str] = {}
    if candidate_set:
        for item in candidate_set.get("candidates", []):
            eid = item.get("entry_id", "")
            candidate_state_by_entry[eid] = item.get("state_id", "")
            linked = set()
            for rr in item.get("retrieval_reasons", []) or []:
                if isinstance(rr, dict):
                    sid = rr.get("source_record_id")
                    if sid:
                        linked.add(sid)
            candidate_linked_records[eid] = linked

    # -- packet observation count, for matched_observations/missing_observations range checks --
    packet_observation_count = None
    if packet is not None:
        packet_observation_count = len(packet.get("observations", []) or [])

    # -- matched_states validation --
    matched_entry_ids: set = set()
    for i, ms in enumerate(matched_states):
        if not isinstance(ms, dict):
            errors.append(f"matched_states[{i}] must be a dict")
            continue
        for f in _MATCHED_STATE_REQUIRED:
            if f not in ms:
                errors.append(f"matched_states[{i}] missing required field: {f}")

        # legacy fields (R45-C2): retired by construction, must not be present
        for legacy_f in ("matched_packet_features", "missing_features"):
            if legacy_f in ms:
                errors.append(
                    f"matched_states[{i}] contains retired legacy field {legacy_f!r}"
                )

        eid = ms.get("entry_id", "")
        sid = ms.get("state_id", "")
        matched_entry_ids.add(eid)

        # match_strength enum
        mstr = ms.get("match_strength", "")
        if mstr and mstr not in _MATCH_STRENGTH_ALLOWED:
            errors.append(f"matched_states[{i}].match_strength unknown value: {mstr}")

        # policy_posture enum (loaded from vocab)
        posture_values = set()
        try:
            from connector.tools.paths import vocab_dir as _vd
            posture_data = load_yaml(_vd() / "policy_posture_enum.yaml")
            posture_values = set(posture_data.get("values", []))
        except Exception:
            posture_values = {
                "defensive_monitoring", "risk_off_monitoring", "neutral_observation",
                "regime_dependent", "caution_flagged", "no_actionable_posture", "unsupported",
            }
        pp = ms.get("policy_posture", "")
        if pp and pp not in posture_values:
            errors.append(f"matched_states[{i}].policy_posture unknown value: {pp}")

        # evidence_caveats: check for prohibited phrases
        caveats = ms.get("evidence_caveats", "")
        if caveats:
            match = find_prohibited(
                str(caveats),
                phrases=prohibited_phrases,
                patterns=prohibited_patterns,
                allowed_phrases=allowed_phrases,
            )
            if match:
                errors.append(f"matched_states[{i}].evidence_caveats contains prohibited phrase: {match}")

        # matched_observations: indices into packet.observations[] (A3)
        if "matched_observations" in ms:
            mo = ms["matched_observations"]
            if not isinstance(mo, list):
                errors.append(f"matched_states[{i}].matched_observations must be a list")
            elif len(mo) < 1:
                errors.append(f"matched_states[{i}].matched_observations must have at least 1 entry")
            else:
                seen_idx = set()
                for idx in mo:
                    if not isinstance(idx, int) or isinstance(idx, bool):
                        errors.append(
                            f"matched_states[{i}].matched_observations contains "
                            f"non-integer value: {idx!r}"
                        )
                        continue
                    if packet_observation_count is not None and not (0 <= idx < packet_observation_count):
                        errors.append(
                            f"matched_states[{i}].matched_observations index out of "
                            f"range: {idx} (packet has {packet_observation_count} observations)"
                        )
                    if idx in seen_idx:
                        errors.append(
                            f"matched_states[{i}].matched_observations contains "
                            f"duplicate index: {idx}"
                        )
                    seen_idx.add(idx)

        # missing_observations: optional, same checks (A4)
        if "missing_observations" in ms:
            mo = ms["missing_observations"]
            if not isinstance(mo, list):
                errors.append(f"matched_states[{i}].missing_observations must be a list")
            else:
                seen_idx = set()
                for idx in mo:
                    if not isinstance(idx, int) or isinstance(idx, bool):
                        errors.append(
                            f"matched_states[{i}].missing_observations contains "
                            f"non-integer value: {idx!r}"
                        )
                        continue
                    if packet_observation_count is not None and not (0 <= idx < packet_observation_count):
                        errors.append(
                            f"matched_states[{i}].missing_observations index out of "
                            f"range: {idx} (packet has {packet_observation_count} observations)"
                        )
                    if idx in seen_idx:
                        errors.append(
                            f"matched_states[{i}].missing_observations contains "
                            f"duplicate index: {idx}"
                        )
                    seen_idx.add(idx)

        # citations: required, >=1 item, kind enum, candidate-linked-record
        # membership, and gloss/kind prefix consistency (A5).
        if "citations" in ms:
            citations = ms["citations"]
            if not isinstance(citations, list) or len(citations) < 1:
                errors.append(f"matched_states[{i}].citations must be a non-empty list")
            else:
                linked_records = candidate_linked_records.get(eid) if candidate_set else None
                for j, cit in enumerate(citations):
                    if not isinstance(cit, dict):
                        errors.append(f"matched_states[{i}].citations[{j}] must be a dict")
                        continue
                    for f in ("record_id", "kind"):
                        if f not in cit:
                            errors.append(
                                f"matched_states[{i}].citations[{j}] missing required field: {f}"
                            )

                    rid = cit.get("record_id", "")
                    kind = cit.get("kind", "")

                    if kind and kind not in _CITATION_KIND_ALLOWED:
                        errors.append(
                            f"matched_states[{i}].citations[{j}].kind unknown value: {kind}"
                        )

                    if rid and candidate_set is not None and eid in candidate_linked_records:
                        if rid not in linked_records:
                            errors.append(
                                f"matched_states[{i}].citations[{j}].record_id {rid} "
                                f"is not in candidate {eid}'s linked records"
                            )

                    if rid:
                        known, expected_kind = _expected_kind_for_record(rid)
                        if known:
                            if expected_kind is None:
                                errors.append(
                                    f"matched_states[{i}].citations[{j}].record_id {rid} "
                                    f"is a gloss (state_*) and cannot be cited as evidence"
                                )
                            elif kind and kind != expected_kind:
                                errors.append(
                                    f"matched_states[{i}].citations[{j}].kind {kind!r} does "
                                    f"not match expected kind {expected_kind!r} for record_id {rid}"
                                )

        # entry_id is itself a Market Book identifier -- expected, not text.

    # -- candidate_set membership --
    if candidate_set:
        cs_entry_ids = set(candidate_linked_records.keys())
        for eid in matched_entry_ids:
            if eid not in cs_entry_ids:
                errors.append(
                    f"Matched entry_id {eid} is not in the candidate set"
                )

    # -- catalog cross-reference: matched entry_id must exist and the
    #    (entry_id, state_id) pair must match the catalog (catches invented
    #    state IDs). Supports catalog 'entries' as a list or a dict.
    if catalog:
        entry_map = _catalog_entry_map(catalog)
        for i, ms in enumerate(matched_states):
            if not isinstance(ms, dict):
                continue
            eid = ms.get("entry_id", "")
            sid = ms.get("state_id", "")
            if not eid or not sid:
                continue
            if eid not in entry_map:
                errors.append(
                    f"matched_states[{i}].entry_id {eid} not found in catalog"
                )
                continue
            catalog_state_id = entry_map[eid].get("state_id", "")
            if catalog_state_id and catalog_state_id != sid:
                errors.append(
                    f"matched_states[{i}]: state_id {sid} does not match "
                    f"catalog state_id {catalog_state_id} for entry {eid}"
                )

    # -- non_matches validation --
    catalog_entry_map = _catalog_entry_map(catalog) if catalog else {}

    non_matches = evaluation.get("non_matches") or []
    for i, nm in enumerate(non_matches):
        if not isinstance(nm, dict):
            errors.append(f"non_matches[{i}] must be a dict")
            continue
        for f in ["entry_id", "state_id", "reason_code", "reason"]:
            if f not in nm:
                errors.append(f"non_matches[{i}] missing required field: {f}")

        eid = nm.get("entry_id", "")
        sid = nm.get("state_id", "")

        reason_code = nm.get("reason_code", "")
        if reason_code and reason_code not in _REASON_CODE_ALLOWED:
            errors.append(f"non_matches[{i}].reason_code unknown value: {reason_code}")

        # candidate-set membership + state consistency
        if candidate_set:
            if eid not in candidate_state_by_entry:
                errors.append(f"non_matches[{i}].entry_id {eid} is not in the candidate set")
            elif sid != candidate_state_by_entry[eid]:
                errors.append(
                    f"non_matches[{i}].state_id {sid} does not match candidate "
                    f"state_id {candidate_state_by_entry[eid]} for entry {eid}"
                )

        # catalog cross-reference: entry must exist and state_id must match
        if catalog:
            if eid not in catalog_entry_map:
                errors.append(f"non_matches[{i}].entry_id {eid} not found in catalog")
            else:
                catalog_state_id = catalog_entry_map[eid].get("state_id", "")
                if catalog_state_id and catalog_state_id != sid:
                    errors.append(
                        f"non_matches[{i}]: state_id {sid} does not match "
                        f"catalog state_id {catalog_state_id} for entry {eid}"
                    )

    # -- evaluation coherence (U4b R8) --
    matched_items = matched_states if isinstance(matched_states, list) else []
    non_match_items = non_matches if isinstance(non_matches, list) else []
    matched_ids = {
        item.get("entry_id")
        for item in matched_items
        if isinstance(item, dict) and item.get("entry_id")
    }
    non_match_ids = {
        item.get("entry_id")
        for item in non_match_items
        if isinstance(item, dict) and item.get("entry_id")
    }
    for entry_id in sorted(matched_ids & non_match_ids):
        errors.append(
            f"entry_id {entry_id} appears in both matched_states and non_matches"
        )

    for list_name, items in (
        ("matched_states", matched_items),
        ("non_matches", non_match_items),
    ):
        seen_ids = set()
        duplicate_ids = set()
        for item in items:
            if not isinstance(item, dict) or not item.get("entry_id"):
                continue
            entry_id = item["entry_id"]
            if entry_id in seen_ids:
                duplicate_ids.add(entry_id)
            seen_ids.add(entry_id)
        for entry_id in sorted(duplicate_ids):
            errors.append(
                f"entry_id {entry_id} appears more than once within {list_name}"
            )

    if candidate_set is not None:
        candidates = candidate_set.get("candidates", []) or []
        candidate_ids = {
            item.get("entry_id")
            for item in candidates
            if isinstance(item, dict) and item.get("entry_id")
        }
        classified_ids = matched_ids | non_match_ids
        if classified_ids != candidate_ids:
            missing_ids = sorted(candidate_ids - classified_ids)
            unexpected_ids = sorted(classified_ids - candidate_ids)
            errors.append(
                "classified entry_id set does not equal candidate entry_id set; "
                f"missing from evaluation: {missing_ids}; "
                f"unexpected in evaluation: {unexpected_ids}"
            )

    for field_path, text in _iter_coherence_text_fields(evaluation):
        marker_match = _INCOHERENCE_MARKER_RE.search(text)
        if marker_match:
            errors.append(
                f"{field_path} contains incoherence marker: {marker_match.group(0)}"
            )

    # -- prohibited_actions is exempt from phrase checks (by contract) --

    # -- overall_interpretation: check for prohibited phrases in summary + confidence enum --
    oi = evaluation.get("overall_interpretation")
    if isinstance(oi, dict):
        summary = oi.get("summary", "")
        if summary:
            match = find_prohibited(
                str(summary),
                phrases=prohibited_phrases,
                patterns=prohibited_patterns,
                allowed_phrases=allowed_phrases,
            )
            if match:
                errors.append(f"overall_interpretation.summary contains prohibited phrase: {match}")

        confidence = oi.get("confidence", "")
        if confidence and confidence not in _CONFIDENCE_ALLOWED:
            errors.append(f"overall_interpretation.confidence unknown value: {confidence}")

    valid = len(errors) == 0
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "checked_at_utc": now_utc_iso(),
        "validator_version": "0.3",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    from connector.tools.io_utils import load_json

    ap = argparse.ArgumentParser(description="Validate a Market Book Evaluation")
    ap.add_argument("--evaluation", required=True, help="Path to evaluation YAML or JSON")
    ap.add_argument("--packet", default=None, help="Path to observation packet YAML or JSON")
    ap.add_argument("--candidate-set", default=None, help="Path to candidate set JSON")
    ap.add_argument("--catalog", default=None, help="Path to catalog JSON")
    ap.add_argument(
        "--vocab-dir",
        default=None,
        help="Directory containing vocabulary YAML files (defaults to connector/vocab).",
    )
    ap.add_argument("--out", default=None, help="Write report JSON to this path")
    args = ap.parse_args()

    eval_path = Path(args.evaluation)
    if eval_path.suffix in (".yaml", ".yml"):
        evaluation = load_yaml(eval_path)
    else:
        evaluation = load_json(eval_path)

    packet = None
    if args.packet:
        pp = Path(args.packet)
        if pp.suffix in (".yaml", ".yml"):
            packet = load_yaml(pp)
        else:
            packet = load_json(pp)

    candidate_set = None
    if args.candidate_set:
        candidate_set = load_json(Path(args.candidate_set))

    catalog = None
    if args.catalog:
        catalog = load_json(Path(args.catalog))

    vocab = None
    if args.vocab_dir:
        vocab = load_vocab(Path(args.vocab_dir))

    report = validate_evaluation(
        evaluation,
        packet=packet,
        candidate_set=candidate_set,
        catalog=catalog,
        vocab=vocab,
    )

    report_text = json.dumps(report, indent=2, sort_keys=True)
    print(report_text)

    if args.out:
        Path(args.out).write_text(report_text, encoding="utf-8")

    sys.exit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
