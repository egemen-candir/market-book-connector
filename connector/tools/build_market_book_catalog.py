"""Build the Market Book catalog from entry YAML files.

Reads research_loop/market_book/entries/*.yaml READ-ONLY and produces
a deterministic catalog JSON, its SHA-256, and an accepted-entry manifest.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from connector.tools.io_utils import dump_json, load_json, load_yaml, now_utc_iso, write_json, write_text
from connector.tools.paths import ensure_dir

MARKET_BOOK_VERSION = "stage6_accepted"
ACCEPTANCE_BASIS = "stage6_accepted_corpus (file_status=draft; synthesized from accepted Stage 5 records)"

# Feature-family keywords used for tag derivation from state_summary text.
TAG_KEYWORDS: dict[str, list[str]] = {
    "trend": ["trend", "momentum", "trending", "horizon"],
    "equity": ["equity", "stock", "return", "sharpe"],
    "breadth": ["breadth", "participation", "advance", "decline"],
    "volatility": ["volatility", "garch", "variance", "vol", "shock"],
    "correlation": ["correlation", "comovement", "co-movement", "covariance"],
    "systemic": ["systemic", "network", "interconnection"],
    "liquidity": ["liquidity", "illiq", "impact", "spread", "amihud", "taq"],
    "credit": ["credit", "spread", "default"],
    "microstructure": ["microstructure", "bid-ask", "tick"],
    "mean_reversion": ["mean-reversion", "mean reversion", "reversion"],
    "regime": ["regime", "state-switch", "hmm", "markov"],
    "macro": ["macro", "policy", "fiscal", "monetary"],
}


def _derive_tags(state_summary: str) -> list[str]:
    """Derive tags from state_summary text using keyword matching."""
    text_lower = state_summary.lower()
    tags = []
    for tag, keywords in TAG_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                tags.append(tag)
                break
    return sorted(tags)


def _entry_to_catalog_record(raw: dict) -> dict:
    """Convert a raw entry dict into a catalog record."""
    state_summary = str(raw.get("state_summary", ""))
    obs_sigs = raw.get("observable_signatures") or []
    fail_patterns = raw.get("failure_patterns") or []
    policy_impls = raw.get("policy_implications") or []
    supporting_claims = raw.get("supporting_claims") or []

    # Build text blob for text matching (lowercase concatenation).
    text_parts = [state_summary]
    for s in obs_sigs:
        text_parts.append(str(s))
    for s in fail_patterns:
        text_parts.append(str(s))
    for s in policy_impls:
        text_parts.append(str(s))
    text = " ".join(text_parts).lower()

    return {
        "entry_id": raw.get("entry_id", ""),
        "state_id": raw.get("state_id", ""),
        "file_status": raw.get("status", "draft"),
        "acceptance_basis": ACCEPTANCE_BASIS,
        "state_summary": state_summary,
        "observable_signatures": list(obs_sigs),
        "failure_patterns": list(fail_patterns),
        "policy_implications": list(policy_impls),
        "supporting_claims": list(supporting_claims),
        "tags": _derive_tags(state_summary),
        "text": text,
    }


def build_catalog(entries_dir_path: Path, *, allow_empty: bool = False) -> tuple[dict, dict]:
    """Read entry YAMLs and build catalog + manifest.

    Returns (catalog, manifest). Raises if the entries directory is missing or
    empty unless ``allow_empty`` is True.
    """
    entries_dir_path = Path(entries_dir_path)
    if not entries_dir_path.exists():
        if allow_empty:
            yaml_files = []
        else:
            raise FileNotFoundError(f"entries directory not found: {entries_dir_path}")
    elif not entries_dir_path.is_dir():
        raise NotADirectoryError(f"entries path is not a directory: {entries_dir_path}")
    else:
        yaml_files = sorted(entries_dir_path.glob("*.yaml"))

    if not yaml_files and not allow_empty:
        raise ValueError(f"no Market Book entry YAML files found in: {entries_dir_path}")

    catalog_entries: list[dict] = []
    all_accepted_claims: set[str] = set()
    all_state_ids: list[str] = []
    all_entry_ids: list[str] = []
    manifest_entries: list[dict] = []

    for fpath in yaml_files:
        raw = load_yaml(fpath)
        rec = _entry_to_catalog_record(raw)
        catalog_entries.append(rec)

        for c in rec["supporting_claims"]:
            all_accepted_claims.add(c)
        all_state_ids.append(rec["state_id"])
        all_entry_ids.append(rec["entry_id"])

        manifest_entries.append({
            "entry_id": rec["entry_id"],
            "state_id": rec["state_id"],
            "file_status": rec["file_status"],
            "acceptance_basis": rec["acceptance_basis"],
            "source_file": fpath.name,
        })

    generated_at = now_utc_iso()

    catalog = {
        "market_book_version": MARKET_BOOK_VERSION,
        "generated_at_utc": generated_at,
        "entry_count": len(catalog_entries),
        "entries": catalog_entries,
        "accepted_claims": sorted(all_accepted_claims),
        "state_ids": all_state_ids,
        "entry_ids": all_entry_ids,
    }

    catalog_hash = catalog_sha256(catalog)

    manifest = {
        "market_book_version": MARKET_BOOK_VERSION,
        "generated_at_utc": generated_at,
        "entry_count": len(catalog_entries),
        "catalog_sha256": catalog_hash,
        "entries": manifest_entries,
    }

    return catalog, manifest


def catalog_sha256(catalog: dict) -> str:
    """Compute SHA-256 hex digest of deterministic JSON bytes."""
    blob = dump_json(catalog).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Linked-record enrichment (opt-in via --enrich)
# ---------------------------------------------------------------------------

ENRICHED_VERSION = "stage6_enriched"

# Fixed emit order for embed_text / linked_records (signature, claim, policy, failure).
_ENRICH_KIND_ORDER: list[str] = ["signature", "claim", "policy", "failure"]

# kind -> (records subdir relative to records-root, id field, ordered text fields, entry link key).
# Subdirs are the F2 canonical record dirs; id field is read from file contents (F4/F5);
# no status filter is applied (F6).
_ENRICH_KIND_SPECS: dict[str, tuple[str, str, list[str], str]] = {
    "signature": (
        "02_observable_signatures/entries",
        "signature_id",
        ["description", "observable_fields", "notes"],
        "observable_signatures",
    ),
    "claim": (
        "working/extracted_claims",
        "claim_id",
        ["claim_text", "context"],
        "supporting_claims",
    ),
    "policy": (
        "04_policy_implications/entries",
        "policy_id",
        ["policy_area", "feature_implications", "model_implications",
         "confidence_implications", "suppression_conditions", "notes"],
        "policy_implications",
    ),
    "failure": (
        "03_failure_patterns/entries",
        "failure_id",
        ["failure_description", "failure_conditions", "notes"],
        "failure_patterns",
    ),
}


def _normalize_ws(text: str) -> str:
    """Collapse runs of whitespace (incl. newlines) to single spaces and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _render_field(value: Any) -> str:
    """Render one record field value for inlining.

    Lists are joined with '; ' (e.g. observable_fields); scalars are whitespace-
    normalised. Empty / missing values render to ''. Case is preserved (no lowercasing).
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_normalize_ws(str(item)) for item in value if str(item).strip())
    return _normalize_ws(str(value))


def _render_record_text(record: dict, text_fields: list[str]) -> str:
    """Join a record's non-empty text fields (in F5 order) with single spaces."""
    parts = [_render_field(record.get(fld)) for fld in text_fields]
    return " ".join(part for part in parts if part)


def _build_record_index(records_root: Path) -> dict[str, dict[str, str]]:
    """Build {kind: {record_id: rendered_text}} from the F2 canonical dirs only.

    Scans only the canonical record dirs under records_root, skipping any *README*
    file and anything under 99_control/. Each record is keyed by its id field
    (read from file contents — never by filename). No status filter (F3/F4/F5/F6).
    """
    records_root = Path(records_root)
    index: dict[str, dict[str, str]] = {}
    for kind, (rel_dir, id_field, text_fields, _link_key) in _ENRICH_KIND_SPECS.items():
        kind_index: dict[str, str] = {}
        scan_dir = records_root / rel_dir
        if scan_dir.is_dir():
            for fpath in sorted(scan_dir.glob("*.yaml")):
                if "README" in fpath.name.upper():
                    continue
                if any(part == "99_control" for part in fpath.parts):
                    continue
                record = load_yaml(fpath)
                if not isinstance(record, dict):
                    continue
                record_id = record.get(id_field)
                if not record_id:
                    continue
                kind_index[str(record_id)] = _render_record_text(record, text_fields)
        index[kind] = kind_index
    return index


def _enrich_entry(record: dict, index: dict[str, dict[str, str]]) -> tuple[str, list[dict]]:
    """Build (embed_text, linked_records) for one catalog entry.

    embed_text line 1 is '[state] <state_summary>'. Then, for each kind in the fixed
    order signature, claim, policy, failure, one line per linked id (sorted ascending):
    '[<id>] <joined record text>'. linked_records mirrors the same resolution.
    Raises ValueError naming the entry + id on any unresolved link (R5).
    """
    state_summary = _normalize_ws(str(record.get("state_summary", "")))
    lines: list[str] = [f"[state] {state_summary}"]
    linked_records: list[dict] = []
    for kind in _ENRICH_KIND_ORDER:
        kind_index = index.get(kind, {})
        link_key = _ENRICH_KIND_SPECS[kind][3]
        for record_id in sorted(str(x) for x in (record.get(link_key) or [])):
            if record_id not in kind_index:
                raise ValueError(
                    f"unresolved linked record: entry={record.get('entry_id')} "
                    f"kind={kind} id={record_id}"
                )
            text = kind_index[record_id]
            lines.append(f"[{record_id}] {text}")
            linked_records.append({"id": record_id, "kind": kind, "text": text})
    return "\n".join(lines), linked_records


def enrich_catalog(catalog: dict, records_root: Path) -> dict:
    """Return a copy of catalog with embed_text + linked_records added to each entry.

    All pre-existing entry and top-level fields are preserved unchanged.
    """
    index = _build_record_index(records_root)
    enriched = dict(catalog)
    enriched_entries: list[dict] = []
    for record in catalog["entries"]:
        embed_text, linked_records = _enrich_entry(record, index)
        new_rec = dict(record)
        new_rec["embed_text"] = embed_text
        new_rec["linked_records"] = linked_records
        enriched_entries.append(new_rec)
    enriched["entries"] = enriched_entries
    return enriched


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build Market Book catalog")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root directory (default: current directory). "
             "Entries are read from <project-root>/research_loop/market_book/entries/ "
             "and the catalog is written to <project-root>/connector/catalog/.",
    )
    parser.add_argument(
        "--entries-dir",
        default=None,
        help="Override entries directory (default: "
             "<project-root>/research_loop/market_book/entries/).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Override catalog output directory (default: "
             "<project-root>/connector/catalog/).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow writing an empty catalog. Intended only for explicit tests or development fixtures.",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Resolve each entry's linked records and add embed_text + linked_records, "
             "writing market_book_catalog.stage6_enriched.json (+.sha256). "
             "Leaves the *stage6_accepted* outputs untouched. Default (no --enrich) is unchanged.",
    )
    parser.add_argument(
        "--records-root",
        default=None,
        help="Root of the research_loop record dirs used by --enrich (default: "
             "<project-root>/research_loop).",
    )
    args = parser.parse_args(argv)

    root = Path(args.project_root).resolve()
    edir = Path(args.entries_dir) if args.entries_dir else root / "research_loop" / "market_book" / "entries"
    cat_dir = Path(args.out_dir) if args.out_dir else root / "connector" / "catalog"
    records_root = Path(args.records_root) if args.records_root else root / "research_loop"
    ensure_dir(cat_dir)

    if args.enrich:
        # The enriched catalog is the accepted catalog (each accepted-entry record
        # verbatim) plus embed_text + linked_records per entry. Loading the accepted
        # catalog — instead of rebuilding with a fresh timestamp — keeps the output
        # byte-reproducible across runs (R7) and the pre-existing entry fields
        # byte-identical to the accepted catalog (R6).
        accepted_path = (root / "connector" / "catalog"
                         / f"market_book_catalog.{MARKET_BOOK_VERSION}.json")
        if not accepted_path.is_file():
            print(f"Error: --enrich requires the accepted catalog at {accepted_path}",
                  file=sys.stderr)
            sys.exit(1)
        catalog = load_json(accepted_path)
        try:
            enriched = enrich_catalog(catalog, records_root)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        cat_file = cat_dir / f"market_book_catalog.{ENRICHED_VERSION}.json"
        sha_file = cat_dir / f"market_book_catalog.{ENRICHED_VERSION}.sha256"
        write_json(cat_file, enriched)
        sha256hex = catalog_sha256(enriched)
        write_text(sha_file, f"{sha256hex}  market_book_catalog.{ENRICHED_VERSION}.json")
        print(f"Wrote enriched catalog: {cat_file}")
        print(f"Wrote enriched sha256:  {sha_file}")
        print(f"Entries: {len(enriched['entries'])}")
        print(f"SHA-256: {sha256hex}")
        return

    try:
        catalog, manifest = build_catalog(edir, allow_empty=args.allow_empty)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    cat_file = cat_dir / f"market_book_catalog.{MARKET_BOOK_VERSION}.json"
    sha_file = cat_dir / f"market_book_catalog.{MARKET_BOOK_VERSION}.sha256"
    manifest_file = cat_dir / f"accepted_entry_manifest.{MARKET_BOOK_VERSION}.json"

    write_json(cat_file, catalog)
    sha256hex = catalog_sha256(catalog)
    write_text(sha_file, f"{sha256hex}  market_book_catalog.{MARKET_BOOK_VERSION}.json")
    write_json(manifest_file, manifest)

    print(f"Wrote catalog:      {cat_file}")
    print(f"Wrote sha256:       {sha_file}")
    print(f"Wrote manifest:     {manifest_file}")
    print(f"Entries: {len(catalog['entries'])}")
    print(f"SHA-256: {sha256hex}")


if __name__ == "__main__":
    main()
