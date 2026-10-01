"""Dashboard report renderer for the Market Book connector module.

Reads validated run artifacts and produces deterministic markdown + JSON
reports.  Handles partial runs (e.g. validation failure) gracefully.
"""

import argparse
import os
from pathlib import Path
from typing import Any

from connector.tools.io_utils import dump_json, load_json, load_yaml, now_utc_iso
from connector.tools.paths import ensure_dir

NO_TRADING_WARNING = (
    "Market Book connector output is research context only. "
    "It is NOT a trade recommendation."
)

REPORT_VERSION = "0.1"
MARKET_BOOK_VERSION = "stage6_accepted"

# Artifact keys to look for in run_dir (relative paths).
_PACKET_PATH = "builder/market_observation_packet.yaml"
_PACKET_VALIDATION_PATH = "validation/observation_packet_validation.json"
_EVAL_PATH = "evaluator/market_book_evaluation.yaml"
_EVAL_VALIDATION_PATH = "validation/market_book_evaluation_validation.json"
_CANDIDATES_PATH = "retrieval/candidate_entries.json"
_MANIFEST_PATH = "run_manifest.json"
_RAW_HASH_PATH = "raw_input_hash.txt"


def _safe_load(path: Path) -> dict | None:
    """Load a JSON or YAML file, returning None if missing/unreadable."""
    try:
        if path.suffix == ".json":
            return load_json(path)
        if path.suffix in (".yaml", ".yml"):
            return load_yaml(path)
    except Exception:
        return None
    return None


def _safe_read(path: Path) -> str:
    """Read a text file, returning empty string if missing."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _build_artifact_paths(run_dir: Path) -> dict[str, str]:
    """Scan for existing artifacts and return a dict of relative paths."""
    found: dict[str, str] = {}
    candidates = [
        ("raw_input", "raw_input.md"),
        ("raw_input_hash", "raw_input_hash.txt"),
        ("raw_input_metadata", "raw_input_metadata.json"),
        ("observation_packet", _PACKET_PATH),
        ("observation_packet_validation", _PACKET_VALIDATION_PATH),
        ("candidate_entries", _CANDIDATES_PATH),
        ("retrieval_trace", "retrieval/retrieval_trace.json"),
        ("market_book_evaluation", _EVAL_PATH),
        ("market_book_evaluation_validation", _EVAL_VALIDATION_PATH),
        ("run_manifest", _MANIFEST_PATH),
    ]
    for key, rel in candidates:
        if (run_dir / rel).exists():
            found[key] = rel
    return found


def render_report(run_dir) -> tuple[str, dict]:
    """Read run artifacts and return (markdown_text, json_obj).

    *run_dir* may be a Path or str.  Handles partial runs where evaluation
    artifacts are missing (validation-failure path).
    """
    run_dir = Path(run_dir)

    # --- read artifacts (graceful) ------------------------------------------
    manifest = _safe_load(run_dir / _MANIFEST_PATH) or {}
    raw_hash = _safe_read(run_dir / _RAW_HASH_PATH)
    packet_val = _safe_load(run_dir / _PACKET_VALIDATION_PATH) or {}
    eval_val = _safe_load(run_dir / _EVAL_VALIDATION_PATH) or {}
    packet = _safe_load(run_dir / _PACKET_PATH) or {}
    evaluation = _safe_load(run_dir / _EVAL_PATH) or {}
    candidate_data = _safe_load(run_dir / _CANDIDATES_PATH) or {}
    artifact_paths = _build_artifact_paths(run_dir)

    run_id = manifest.get("run_id", run_dir.name)
    backend = manifest.get("backend", "unknown")
    generated_at = now_utc_iso()

    # validation_status
    validation_status = {
        "observation_packet": {
            "valid": packet_val.get("valid", False),
        },
        "market_book_evaluation": {
            "valid": eval_val.get("valid", False),
        },
    }

    # packet-derived fields
    normalized_features = [
        fa["feature"]
        for fa in (packet.get("feature_assertions") or [])
        if "feature" in fa
    ]
    unsupported_statements = [
        s.get("statement", "")
        for s in (packet.get("unsupported_or_uncertain_statements") or [])
    ]
    unavailable_features = list(packet.get("unavailable_features") or [])

    # candidate_entries (simplified for report)
    candidates_list = candidate_data.get("candidates") or []
    candidate_entries_report = [
        {
            "entry_id": c["entry_id"],
            "state_id": c.get("state_id", ""),
            "retrieval_score": c.get("retrieval_score", 0.0),
        }
        for c in candidates_list
    ]

    # matched_states from evaluation
    matched_raw = evaluation.get("matched_states") or []
    matched_states = [
        {
            "entry_id": m["entry_id"],
            "state_id": m.get("state_id", ""),
            "match_strength": m.get("match_strength", ""),
            "policy_posture": m.get("policy_posture", ""),
            "matched_packet_features": list(m.get("matched_packet_features") or []),
            "missing_features": list(m.get("missing_features") or []),
            "evidence_caveats": m.get("evidence_caveats", ""),
        }
        for m in matched_raw
    ]

    # Derive caveats and missing_evidence from matched_states
    caveats = [
        m["evidence_caveats"]
        for m in matched_states
        if m.get("evidence_caveats")
    ]
    missing_evidence = []
    for m in matched_states:
        for f in m.get("missing_features") or []:
            if f not in missing_evidence:
                missing_evidence.append(f)

    # policy_posture union
    policy_postures = sorted({
        m["policy_posture"]
        for m in matched_states
        if m.get("policy_posture")
    })

    # prohibited_actions from evaluation
    prohibited_actions = list(evaluation.get("prohibited_actions") or [])

    # If evaluation validation failed, include errors
    # (handled by validation_status already; errors rendered in markdown)

    # --- JSON obj (must conform to dashboard_report.schema.yaml) ------------
    report_obj: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": generated_at,
        "run_id": run_id,
        "backend": backend,
        "market_book_version": MARKET_BOOK_VERSION,
        "raw_input_hash": raw_hash,
        "no_trading_warning": NO_TRADING_WARNING,
        "validation_status": validation_status,
        "normalized_features": sorted(normalized_features),
        "unsupported_statements": unsupported_statements,
        "unavailable_features": unavailable_features,
        "candidate_entries": candidate_entries_report,
        "matched_states": matched_states,
        "missing_evidence": missing_evidence,
        "caveats": caveats,
        "policy_posture": policy_postures,
        "prohibited_actions": prohibited_actions,
        "artifact_paths": artifact_paths,
    }

    # --- Markdown ------------------------------------------------------------
    md = _render_markdown(report_obj, packet_val, eval_val)

    return md, report_obj


def _render_markdown(
    report: dict[str, Any],
    packet_val: dict,
    eval_val: dict,
) -> str:
    """Build human-readable markdown from the report dict."""
    lines: list[str] = []

    lines.append("# Market Book Connector -- Dashboard Report")
    lines.append("")
    lines.append(f"**NO-TRADING WARNING**: {report['no_trading_warning']}")
    lines.append("")

    # Header metadata
    lines.append("## Run Metadata")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| report_version | {report['report_version']} |")
    lines.append(f"| generated_at_utc | {report['generated_at_utc']} |")
    lines.append(f"| run_id | {report['run_id']} |")
    lines.append(f"| backend | {report['backend']} |")
    lines.append(f"| market_book_version | {report['market_book_version']} |")
    lines.append(f"| raw_input_hash | {report['raw_input_hash']} |")
    lines.append("")

    # Validation status
    lines.append("## Validation Status")
    lines.append("")
    pkt_valid = report["validation_status"]["observation_packet"]["valid"]
    eval_valid = report["validation_status"]["market_book_evaluation"]["valid"]
    pkt_errors = packet_val.get("errors", [])
    eval_errors = eval_val.get("errors", [])
    lines.append(f"- Observation packet: **{'VALID' if pkt_valid else 'INVALID'}**")
    lines.append(f"- Market Book evaluation: **{'VALID' if eval_valid else 'INVALID'}**")
    if pkt_errors:
        for e in pkt_errors:
            lines.append(f"  - Packet error: {e}")
    if eval_errors:
        for e in eval_errors:
            lines.append(f"  - Evaluation error: {e}")
    lines.append("")

    # Normalized features
    lines.append("## Normalized Features")
    lines.append("")
    if report["normalized_features"]:
        for f in report["normalized_features"]:
            lines.append(f"- {f}")
    else:
        lines.append("(none)")
    lines.append("")

    # Unsupported statements
    lines.append("## Unsupported Statements")
    lines.append("")
    if report["unsupported_statements"]:
        for s in report["unsupported_statements"]:
            lines.append(f"- {s}")
    else:
        lines.append("(none)")
    lines.append("")

    # Unavailable features
    lines.append("## Unavailable Features")
    lines.append("")
    if report["unavailable_features"]:
        for f in report["unavailable_features"]:
            lines.append(f"- {f}")
    else:
        lines.append("(none)")
    lines.append("")

    # Candidate entries
    lines.append("## Candidate Entries")
    lines.append("")
    if report["candidate_entries"]:
        lines.append("| entry_id | state_id | retrieval_score |")
        lines.append("|---|---|---|")
        for c in report["candidate_entries"]:
            lines.append(f"| {c['entry_id']} | {c['state_id']} | {c['retrieval_score']} |")
    else:
        lines.append("(none)")
    lines.append("")

    # Matched states
    lines.append("## Matched States")
    lines.append("")
    if report["matched_states"]:
        for m in report["matched_states"]:
            lines.append(f"### {m['entry_id']}")
            lines.append("")
            lines.append(f"- **state_id**: {m['state_id']}")
            lines.append(f"- **match_strength**: {m['match_strength']}")
            lines.append(f"- **policy_posture**: {m['policy_posture']}")
            matched = m.get("matched_packet_features") or []
            lines.append(f"- **matched features**: {', '.join(matched) if matched else 'none'}")
            missing = m.get("missing_features") or []
            lines.append(f"- **missing features**: {', '.join(missing) if missing else 'none'}")
            lines.append(f"- **evidence caveats**: {m.get('evidence_caveats', 'none')}")
            lines.append("")
    else:
        lines.append("(none -- possibly due to validation failure)")
        lines.append("")

    # Missing evidence (union across matched states)
    lines.append("## Missing Evidence")
    lines.append("")
    if report["missing_evidence"]:
        for f in report["missing_evidence"]:
            lines.append(f"- {f}")
    else:
        lines.append("(none)")
    lines.append("")

    # Caveats (union across matched states)
    lines.append("## Caveats")
    lines.append("")
    if report["caveats"]:
        for c in report["caveats"]:
            lines.append(f"- {c}")
    else:
        lines.append("(none)")
    lines.append("")

    # Policy posture (union)
    lines.append("## Policy Posture")
    lines.append("")
    if report["policy_posture"]:
        for p in report["policy_posture"]:
            lines.append(f"- {p}")
    else:
        lines.append("(none)")
    lines.append("")

    # Prohibited actions
    lines.append("## Prohibited Actions")
    lines.append("")
    if report["prohibited_actions"]:
        for a in report["prohibited_actions"]:
            lines.append(f"- {a}")
    else:
        lines.append("(none)")
    lines.append("")

    # Artifact paths
    lines.append("## Artifact Paths")
    lines.append("")
    if report["artifact_paths"]:
        lines.append("| Key | Relative Path |")
        lines.append("|---|---|")
        for k in sorted(report["artifact_paths"]):
            lines.append(f"| {k} | {report['artifact_paths'][k]} |")
    else:
        lines.append("(none)")
    lines.append("")

    return "\n".join(lines)


def write_report(run_dir) -> tuple[Path, Path]:
    """Render the report for *run_dir* and write files under dashboard/.

    Returns (md_path, json_path).
    """
    run_dir = Path(run_dir)
    md_text, json_obj = render_report(run_dir)
    dashboard_dir = ensure_dir(run_dir / "dashboard")
    md_path = dashboard_dir / "dashboard_report.md"
    json_path = dashboard_dir / "dashboard_report.json"

    md_path.write_text(md_text, encoding="utf-8")
    # Use io_utils.write_json for deterministic JSON output
    from connector.tools.io_utils import write_json
    write_json(json_path, json_obj)

    return md_path, json_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a dashboard report from a connector run directory."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to the connector run directory.",
    )
    parser.add_argument(
        "--out-md",
        default=None,
        help="Optional override: path for the markdown output.",
    )
    parser.add_argument(
        "--out-json",
        default=None,
        help="Optional override: path for the JSON output.",
    )
    args = parser.parse_args()

    md_text, json_obj = render_report(args.run_dir)

    if args.out_md:
        Path(args.out_md).write_text(md_text, encoding="utf-8")
        print(f"Wrote markdown to {args.out_md}")
    else:
        print(md_text)

    if args.out_json:
        from connector.tools.io_utils import write_json
        write_json(Path(args.out_json), json_obj)
        print(f"Wrote JSON to {args.out_json}")


if __name__ == "__main__":
    main()
