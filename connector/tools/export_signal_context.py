"""Deterministic signal-context exporter for the Market Book connector module.

Reads validated pipeline artifacts (packet, candidate set, evaluation,
eval validation) and produces a market_state_context package.  No LLM calls.
Only stdlib + PyYAML are used.
"""

import argparse
import sys
from pathlib import Path

from connector.tools.io_utils import (
    dump_json,
    load_json,
    load_yaml,
    now_utc_iso,
    sha256_bytes,
    write_json,
    write_yaml,
)
from connector.tools.paths import catalog_dir, ensure_dir
from connector.tools.validate_observation_packet import find_prohibited, load_vocab

EXPORTER_VERSION = "0.1"
MARKET_BOOK_VERSION = "stage6_accepted"


def _dedup_ordered(items):
    """Return de-duplicated list preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _check_not_trading(context: dict, vocab: dict) -> bool:
    """Scan all free-text fields in the context for prohibited phrases.

    Returns True when NO prohibited phrase is found.
    """
    phrases = vocab["prohibited_phrases"]
    patterns = vocab["prohibited_patterns"]
    allowed_phrases = vocab["allowed_phrases"]

    text_parts = []

    # observation_summary.narrative_summary
    obs_sum = context.get("observation_summary")
    if isinstance(obs_sum, dict):
        ns = obs_sum.get("narrative_summary", "")
        if ns:
            text_parts.append(str(ns))

    # caveats (list of strings)
    for c in context.get("caveats") or []:
        if c:
            text_parts.append(str(c))

    # matched_states[].evidence_caveats
    for ms in context.get("matched_states") or []:
        ec = ms.get("evidence_caveats", "")
        if ec:
            text_parts.append(str(ec))

    # risk_posture fields that are strings
    rp = context.get("risk_posture")
    if isinstance(rp, dict):
        for v in rp.values():
            if isinstance(v, str) and v:
                text_parts.append(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item:
                        text_parts.append(item)

    combined = "\n".join(text_parts)
    hit = find_prohibited(
        combined,
        phrases=phrases,
        patterns=patterns,
        allowed_phrases=allowed_phrases,
    )
    return hit is None


def export_signal_context(
    *,
    run_dir,
    run_id,
    raw_input_hash,
    packet,
    candidate_set,
    evaluation,
    eval_validation,
    catalog,
    vocab,
) -> dict:
    """Build and write the signal-context package.

    Returns {valid: bool, context_path: str|None, artifacts: [reldirs...]}.
    """
    run_dir = Path(run_dir)

    # -- Fail-closed gate: eval validation must be valid --
    if not eval_validation.get("valid"):
        return _write_invalid(run_dir, ["eval_validation is not valid"])

    if not evaluation:
        return _write_invalid(run_dir, ["evaluation is None or empty"])

    if raw_input_hash != MARKET_BOOK_VERSION and False:  # placeholder; real check below
        pass

    # -- Build observation_summary --
    as_of = packet.get("as_of") or {}
    observations = packet.get("observations") or []
    unsupported_stmts = packet.get("unsupported_or_uncertain_statements") or []

    observation_summary = {
        "observation_id": packet.get("observation_id", ""),
        "narrative_summary": packet.get("narrative_summary", ""),
        "asset_scope": list(packet.get("asset_scope") or []),
        "as_of": {
            "date": as_of.get("date", ""),
            "timezone": as_of.get("timezone", ""),
            "observation_horizon": as_of.get("observation_horizon", ""),
        },
        "normalized_features": sorted(
            {obs.get("dimension", "") for obs in observations if obs.get("dimension")}
        ),
        "unavailable_features": list(packet.get("unavailable_features") or []),
        "unsupported_statements": [
            s.get("statement", "") for s in unsupported_stmts if s.get("statement")
        ],
    }

    # -- matched_states projection --
    matched_states_raw = evaluation.get("matched_states") or []
    matched_states = []
    for ms in matched_states_raw:
        if not isinstance(ms, dict):
            continue
        matched_states.append({
            "entry_id": ms.get("entry_id", ""),
            "state_id": ms.get("state_id", ""),
            "match_strength": ms.get("match_strength", ""),
            "matched_observations": list(ms.get("matched_observations") or []),
            "citations": list(ms.get("citations") or []),
            "evidence_caveats": ms.get("evidence_caveats", ""),
            "policy_posture": ms.get("policy_posture", ""),
            "failure_modes": list(ms.get("failure_modes") or []),
        })

    # -- non_matches projection --
    non_matches_raw = evaluation.get("non_matches") or []
    non_matches = []
    for nm in non_matches_raw:
        if not isinstance(nm, dict):
            continue
        non_matches.append({
            "entry_id": nm.get("entry_id", ""),
            "state_id": nm.get("state_id", ""),
            "reason_code": nm.get("reason_code", ""),
            "reason": nm.get("reason", ""),
        })

    # -- missing_evidence: matched_state-declared missing_observations (optional,
    #    validator A4) only. Indices resolve to their observation's dimension label.
    declared_missing = set()
    for ms in matched_states_raw:
        if not isinstance(ms, dict):
            continue
        declared_missing.update(ms.get("missing_observations") or [])

    def _observation_label(idx: int) -> str:
        if 0 <= idx < len(observations):
            return observations[idx].get("dimension") or f"observation_{idx}"
        return f"observation_{idx}"

    missing_evidence = _dedup_ordered(_observation_label(idx) for idx in sorted(declared_missing))

    # -- caveats: de-duped union of evidence_caveats + unsupported statements --
    caveats_parts = []
    for ms in matched_states:
        ec = ms.get("evidence_caveats", "")
        if ec:
            caveats_parts.append(ec)
    for s in observation_summary.get("unsupported_statements") or []:
        if s:
            caveats_parts.append(s)
    caveats = _dedup_ordered(caveats_parts)

    # -- failure_modes: de-duped union + weak_match entries --
    fm_parts = []
    for ms in matched_states:
        for fm in ms.get("failure_modes") or []:
            if fm:
                fm_parts.append(fm)
        if ms.get("match_strength") == "weak":
            fm_parts.append(f"weak_match:{ms.get('entry_id', '')}")
    failure_modes = _dedup_ordered(fm_parts)

    # -- policy_posture: sorted distinct --
    policy_posture_set = {ms.get("policy_posture", "") for ms in matched_states if ms.get("policy_posture")}
    policy_posture = sorted(policy_posture_set) if policy_posture_set else ["no_actionable_posture"]

    # -- risk_posture --
    oi = evaluation.get("overall_interpretation") or {}
    confidence = oi.get("confidence", "unknown")
    dominant_themes = list(oi.get("dominant_themes") or [])
    risk_posture = {
        "confidence": confidence,
        "dominant_themes": dominant_themes,
        "postures": list(policy_posture),
        "posture_count": len(policy_posture),
    }

    # -- constraints: deterministic, no prohibited phrases --
    constraints = []
    unavailable = observation_summary.get("unavailable_features") or []
    for f in unavailable:
        constraints.append(f"measurement_unavailable: {f}")
    for m in missing_evidence:
        constraints.append(f"requires_confirmation: {m}")
    for ms in matched_states:
        if ms.get("match_strength") == "weak":
            eid = ms.get("entry_id", "")
            constraints.append(f"low_confidence_match: {eid}")
    constraints = _dedup_ordered(constraints)

    # -- required_validations --
    required_validations = []
    for m in missing_evidence:
        required_validations.append(f"measure_feature: {m}")
    for u in unavailable:
        if u not in missing_evidence:
            required_validations.append(f"acquire_measurement: {u}")
    required_validations = _dedup_ordered(required_validations)

    # -- source_artifacts --
    source_artifacts = {
        "observation_packet": "builder/market_observation_packet.yaml",
        "observation_packet_validation": "validation/observation_packet_validation.json",
        "candidate_entries": "retrieval/candidate_entries.json",
        "market_book_evaluation": "evaluator/market_book_evaluation.yaml",
        "market_book_evaluation_validation": "validation/market_book_evaluation_validation.json",
        "run_manifest": "run_manifest.json",
    }

    # -- Assemble context --
    market_state_context = {
        "run_id": run_id,
        "raw_input_hash": raw_input_hash,
        "market_book_version": MARKET_BOOK_VERSION,
        "observation_summary": observation_summary,
        "matched_states": matched_states,
        "non_matches": non_matches,
        "missing_evidence": missing_evidence,
        "caveats": caveats,
        "failure_modes": failure_modes,
        "policy_posture": policy_posture,
        "risk_posture": risk_posture,
        "constraints": constraints,
        "required_validations": required_validations,
        "not_trading_instruction": True,  # provisional; checked below
        "source_artifacts": source_artifacts,
    }

    # -- not_trading_instruction: scan free-text fields --
    nti = _check_not_trading(market_state_context, vocab)
    market_state_context["not_trading_instruction"] = nti

    # -- Final fail-closed checks --
    reasons = []
    if not nti:
        reasons.append("prohibited trading phrase detected in context free-text fields")
    if market_state_context["market_book_version"] != MARKET_BOOK_VERSION:
        reasons.append(f"market_book_version must be '{MARKET_BOOK_VERSION}'")

    if reasons:
        return _write_invalid(run_dir, reasons)

    # -- Compute context hash BEFORE writing metadata --
    context_json_bytes = dump_json(market_state_context).encode("utf-8")
    context_sha256 = sha256_bytes(context_json_bytes)

    # -- Write signal_context dir --
    sc_dir = ensure_dir(run_dir / "signal_context")
    ctx_json_path = sc_dir / "market_state_context.json"
    ctx_yaml_path = sc_dir / "market_state_context.yaml"
    meta_path = sc_dir / "export_metadata.json"
    val_path = sc_dir / "validation.json"

    write_json(ctx_json_path, market_state_context)
    write_yaml(ctx_yaml_path, market_state_context)

    export_metadata = {
        "exported_at_utc": now_utc_iso(),
        "exporter_version": EXPORTER_VERSION,
        "run_id": run_id,
        "raw_input_hash": raw_input_hash,
        "market_book_version": MARKET_BOOK_VERSION,
        "source": "deterministic_export",
        "not_trading_instruction": True,
        "context_sha256": f"sha256:{context_sha256}",
    }
    write_json(meta_path, export_metadata)

    validation = {
        "valid": True,
        "status": "ok",
        "checked_at_utc": now_utc_iso(),
        "validator_version": EXPORTER_VERSION,
        "errors": [],
        "warnings": [],
    }
    write_json(val_path, validation)

    return {
        "valid": True,
        "context_path": str(ctx_json_path),
        "artifacts": [
            "signal_context/market_state_context.json",
            "signal_context/market_state_context.yaml",
            "signal_context/export_metadata.json",
            "signal_context/validation.json",
        ],
    }


def _write_invalid(run_dir: Path, reasons: list) -> dict:
    """Write validation.json with valid:false and return failure result."""
    sc_dir = ensure_dir(run_dir / "signal_context")
    val_path = sc_dir / "validation.json"
    validation = {
        "valid": False,
        "status": "invalid",
        "checked_at_utc": now_utc_iso(),
        "validator_version": EXPORTER_VERSION,
        "errors": list(reasons),
        "warnings": [],
    }
    write_json(val_path, validation)
    return {"valid": False, "context_path": None, "artifacts": ["signal_context/validation.json"]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export signal context from a connector run directory."
    )
    parser.add_argument("--run-dir", required=True, help="Path to the connector run directory.")
    parser.add_argument("--catalog", default=None, help="Path to catalog JSON (overrides default).")
    parser.add_argument("--vocab-dir", default=None, help="Directory containing vocabulary YAML files.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)

    # Load artifacts from disk
    manifest = load_json(run_dir / "run_manifest.json")
    run_id = manifest.get("run_id", run_dir.name)
    raw_input_hash = manifest.get("raw_input_hash", "")
    if not raw_input_hash:
        hash_file = run_dir / "raw_input_hash.txt"
        if hash_file.exists():
            raw_input_hash = hash_file.read_text(encoding="utf-8").strip()

    packet = load_yaml(run_dir / "builder" / "market_observation_packet.yaml")
    evaluation = load_yaml(run_dir / "evaluator" / "market_book_evaluation.yaml")
    eval_validation = load_json(run_dir / "validation" / "market_book_evaluation_validation.json")
    candidate_set = load_json(run_dir / "retrieval" / "candidate_entries.json")

    if args.catalog:
        catalog = load_json(Path(args.catalog))
    else:
        catalog = load_json(catalog_dir() / f"market_book_catalog.{MARKET_BOOK_VERSION}.json")

    vocab = load_vocab(Path(args.vocab_dir)) if args.vocab_dir else load_vocab()

    result = export_signal_context(
        run_dir=run_dir,
        run_id=run_id,
        raw_input_hash=raw_input_hash,
        packet=packet,
        candidate_set=candidate_set,
        evaluation=evaluation,
        eval_validation=eval_validation,
        catalog=catalog,
        vocab=vocab,
    )

    if result["valid"]:
        print(f"Signal context exported to {result['context_path']}")
    else:
        print(f"Signal context export FAILED: validation.json written with errors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
