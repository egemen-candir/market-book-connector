"""Validator for Market Observation Packets.

Deterministic validation -- no LLM calls. Checks STRUCTURE and SAFETY only:
required fields, packet shape, Market Book identifiers in text fields, and
direct trading language (prohibited phrases). It does NOT validate any field
against a closed feature vocabulary -- `summary` and the free-text
`observations` use model-chosen labels that are never checked for membership
in any fixed set. Field structure follows
docs/connector_module_task_documentation.md (input_reference, as_of,
prohibited_content_flags).
"""

import re
import sys
from pathlib import Path

from connector.tools.io_utils import load_yaml, now_utc_iso
from connector.tools.paths import vocab_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = [
    "packet_version",
    "observation_id",
    "created_at_utc",
    "market_book_version",
    "input_reference",
    "as_of",
    "asset_scope",
    "summary",
    "observations",
    "prohibited_content_flags",
]

_INPUT_REFERENCE_REQUIRED = ["raw_input_hash", "raw_input_path", "source_type"]
_AS_OF_REQUIRED = ["date", "timezone", "observation_horizon"]
_OBSERVATION_REQUIRED = ["statement", "dimension"]

# Matches Market Book identifier tokens (entry_/state_/claim_...) anywhere in
# free text. Anchored on a word boundary so it fires mid-sentence, not only at
# the start of a string.
_ID_PATTERN = re.compile(r"\b(?:entry|state|claim)_[A-Za-z0-9_]+")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]+$")

_REQUIRED_FLAGS = [
    "contains_trade_recommendation",
    "contains_position_sizing",
    "contains_policy_conclusion",
    "contains_market_book_state_id",
]


# ---------------------------------------------------------------------------
# Vocabulary loader
# ---------------------------------------------------------------------------

def _load_enum(path: Path) -> set:
    return set(load_yaml(path).get("values", []))


def load_vocab(vocab_dir_path: Path | None = None) -> dict:
    """Load the trading-directive safety vocabulary from *vocab_dir_path*.

    Only the prohibited/allowed phrase + pattern data (used by
    ``find_prohibited``) is loaded here. The closed feature vocabulary has been
    retired, so this function NO LONGER loads ``feature_vocabulary.yaml`` and
    does NOT return a ``feature_names`` key. It succeeds when
    ``feature_vocabulary.yaml`` is absent.

    *vocab_dir_path* is the directory that directly contains
    ``prohibited_phrases.yaml`` (and the legacy enum files). It defaults to the
    connector vocab directory.

    Returns: {direction:set, strength:set, confidence:set,
              prohibited_phrases:list, prohibited_patterns:list,
              allowed_phrases:list}

    The ``prohibited_phrases``/``prohibited_patterns``/``allowed_phrases`` keys
    are the safety contract relied on by ``validate_signal_context.py`` and
    ``export_signal_context.py``; they are always populated from
    ``prohibited_phrases.yaml``.
    """
    if vocab_dir_path is None:
        vocab_dir_path = vocab_dir()
    prohibited = load_yaml(vocab_dir_path / "prohibited_phrases.yaml")
    return {
        "direction": _load_enum(vocab_dir_path / "direction_enum.yaml"),
        "strength": _load_enum(vocab_dir_path / "strength_enum.yaml"),
        "confidence": _load_enum(vocab_dir_path / "confidence_enum.yaml"),
        "prohibited_phrases": list(prohibited.get("phrases", [])),
        "prohibited_patterns": list(prohibited.get("patterns", [])),
        "allowed_phrases": list(prohibited.get("allowed_phrases", [])),
    }


# ---------------------------------------------------------------------------
# Text-scan helpers
# ---------------------------------------------------------------------------

def _allowed_spans(text: str, allowed_phrases: list[str]) -> list[tuple[int, int]]:
    """Return the `\\b`-anchored, case-insensitive spans in *text* covered by
    *allowed_phrases* (e.g. ``short horizon``, ``leverage ratio``)."""
    spans: list[tuple[int, int]] = []
    for ap in allowed_phrases or []:
        ap = str(ap)
        if not ap:
            continue
        for m in re.finditer(r"\b" + re.escape(ap) + r"\b", text, flags=re.IGNORECASE):
            spans.append((m.start(), m.end()))
    return spans


def _contained(span: tuple[int, int], allowed_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(a_start <= start and end <= a_end for a_start, a_end in allowed_spans)


def _match_span(match: re.Match) -> tuple[int, int]:
    """Return *match*'s span with trailing non-word characters trimmed.

    Some prohibited-pattern regexes (e.g. the verb+instrument pattern's
    ``[\\w.\\-]*`` instrument tail) consume a trailing sentence period or
    hyphen that the `\\b`-anchored allowed-phrase span does not, e.g.
    ``short horizon.`` matches one character past ``short horizon``. Trimming
    to the last word character before the containment check compares what
    was actually matched, not incidental trailing punctuation.
    """
    start, end = match.start(), match.end()
    text = match.string
    while end > start and not (text[end - 1].isalnum() or text[end - 1] == "_"):
        end -= 1
    return start, end


def find_prohibited(
    text: str,
    *,
    phrases: list[str] | None = None,
    patterns: list | None = None,
    allowed_phrases: list[str] | None = None,
) -> str | None:
    """Return the first prohibited phrase/pattern label found, or None.

    Matchers run on the undamaged *text* (no blanking). A match is suppressed
    if and only if it lies entirely within a span claimed by
    *allowed_phrases* (containment; see ``_allowed_spans``) -- a match that
    only partly overlaps an allowed phrase, or extends past it, still fires.
    Multi-word or hyphenated *phrases* match as substrings; single-word
    phrases match on word boundaries. *patterns* is a list of regex strings
    or ``{name, regex}`` dicts matched case-insensitively.
    """
    if not text:
        return None
    text = str(text)
    allowed_spans = _allowed_spans(text, allowed_phrases or [])

    for phrase in phrases or []:
        pl = str(phrase)
        regex = re.escape(pl) if (" " in pl or "-" in pl) else r"\b" + re.escape(pl) + r"\b"
        for m in re.finditer(regex, text, flags=re.IGNORECASE):
            if not _contained(_match_span(m), allowed_spans):
                return phrase

    for entry in patterns or []:
        if isinstance(entry, dict):
            regex = entry.get("regex", "")
            label = entry.get("name", regex)
        else:
            regex = entry
            label = entry
        if not regex:
            continue
        for m in re.finditer(regex, text, flags=re.IGNORECASE):
            if not _contained(_match_span(m), allowed_spans):
                return label
    return None


def _contains_id_pattern(text: str) -> bool:
    return bool(_ID_PATTERN.search(text))


def _scan_freetext(
    text,
    field_label: str,
    *,
    phrases: list,
    patterns: list,
    allowed_phrases: list,
) -> list[str]:
    """Return error strings for a single free-text value.

    A free-text field may be a non-string (e.g. null for nullable
    direction/magnitude); such values are not scanned. Non-empty strings are
    checked for Market Book identifiers and prohibited trading-directive
    phrases.
    """
    if not isinstance(text, str) or not text:
        return []
    errors: list[str] = []
    if _contains_id_pattern(text):
        errors.append(f"{field_label} contains a Market Book identifier (entry_/state_/claim_)")
    hit = find_prohibited(text, phrases=phrases, patterns=patterns, allowed_phrases=allowed_phrases)
    if hit:
        errors.append(f"{field_label} contains prohibited phrase: {hit}")
    return errors


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------

def validate_packet(
    packet: dict,
    *,
    vocab: dict | None = None,
    catalog: dict | None = None,
) -> dict:
    """Validate an observation packet dict.

    Structure + safety only: never validates any field against a closed
    feature vocabulary.

    Returns {"valid":bool, "errors":[str], "warnings":[str],
             "checked_at_utc":str, "validator_version":"0.1"}.
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
        if field not in packet:
            errors.append(f"Missing required field: {field}")

    # -- input_reference --
    input_ref = packet.get("input_reference")
    if isinstance(input_ref, dict):
        for f in _INPUT_REFERENCE_REQUIRED:
            if f not in input_ref:
                errors.append(f"input_reference missing required field: {f}")
        raw_hash = input_ref.get("raw_input_hash", "")
        if raw_hash and not _HASH_PATTERN.match(str(raw_hash)):
            errors.append(
                f"input_reference.raw_input_hash must match sha256:<hex>, got {raw_hash!r}"
            )
    elif "input_reference" in packet:
        errors.append("input_reference must be a dict")

    # -- as_of --
    as_of = packet.get("as_of")
    if isinstance(as_of, dict):
        for f in _AS_OF_REQUIRED:
            if f not in as_of:
                errors.append(f"as_of missing required field: {f}")
    elif "as_of" in packet:
        errors.append("as_of must be a dict")

    # -- market_book_version --
    if packet.get("market_book_version") and packet["market_book_version"] != "stage6_accepted":
        errors.append(
            f"market_book_version must be 'stage6_accepted', got {packet['market_book_version']!r}"
        )

    # -- summary: required non-empty, no identifiers, no trading language --
    summary = packet.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    else:
        errors.extend(
            _scan_freetext(
                summary,
                "summary",
                phrases=prohibited_phrases,
                patterns=prohibited_patterns,
                allowed_phrases=allowed_phrases,
            )
        )

    # -- observations: open list of free-text observations --
    observations = packet.get("observations")
    if observations is None:
        observations = []
    if not isinstance(observations, list):
        errors.append("observations must be a list")
        observations = []
    elif len(observations) == 0:
        errors.append("observations must contain at least one item")

    for i, obs in enumerate(observations):
        if not isinstance(obs, dict):
            errors.append(f"observations[{i}] must be a dict")
            continue
        for f in _OBSERVATION_REQUIRED:
            val = obs.get(f, "")
            if not isinstance(val, str) or not val.strip():
                errors.append(f"observations[{i}] missing required non-empty field: {f}")
        # direction/magnitude are optional and nullable: when present they must
        # be strings (model-authored free text), but may be null or absent.
        for f in ("direction", "magnitude"):
            val = obs.get(f)
            if val is not None and not isinstance(val, str):
                errors.append(f"observations[{i}].{f} must be a string or null")
        # Free-text fields are scanned for safety; direction/magnitude may be null.
        for f in ("statement", "dimension", "direction", "magnitude"):
            errors.extend(
                _scan_freetext(
                    obs.get(f, ""),
                    f"observations[{i}].{f}",
                    phrases=prohibited_phrases,
                    patterns=prohibited_patterns,
                    allowed_phrases=allowed_phrases,
                )
            )

    # -- prohibited_content_flags: require the 4 contract flags, all false --
    flags = packet.get("prohibited_content_flags")
    if isinstance(flags, dict):
        for required in _REQUIRED_FLAGS:
            if required not in flags:
                errors.append(f"prohibited_content_flags missing required key: {required}")
        for k, v in flags.items():
            if v is not False:
                errors.append(f"prohibited_content_flags.{k} must be false, got {v!r}")
    elif "prohibited_content_flags" in packet:
        errors.append("prohibited_content_flags must be a dict")

    valid = len(errors) == 0
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "checked_at_utc": now_utc_iso(),
        "validator_version": "0.1",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    from connector.tools.io_utils import load_json

    ap = argparse.ArgumentParser(description="Validate a Market Observation Packet")
    ap.add_argument("--packet", required=True, help="Path to packet YAML or JSON")
    ap.add_argument(
        "--vocab-dir",
        default=None,
        help="Directory containing vocabulary YAML files (defaults to connector/vocab).",
    )
    ap.add_argument("--catalog", default=None, help="Path to catalog JSON")
    ap.add_argument("--out", default=None, help="Write report JSON to this path")
    args = ap.parse_args()

    packet_path = Path(args.packet)
    packet = load_yaml(packet_path) if packet_path.suffix in (".yaml", ".yml") else load_json(packet_path)

    vocab = load_vocab(Path(args.vocab_dir)) if args.vocab_dir else None
    catalog = load_json(Path(args.catalog)) if args.catalog else None

    report = validate_packet(packet, vocab=vocab, catalog=catalog)
    report_text = json.dumps(report, indent=2, sort_keys=True)
    print(report_text)
    if args.out:
        Path(args.out).write_text(report_text, encoding="utf-8")
    sys.exit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
