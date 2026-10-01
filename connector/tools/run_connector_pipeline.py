"""Orchestrator for the Market Book downstream connector pipeline.

Runs: capture raw input -> builder -> validate packet -> retrieve ->
evaluator -> validate evaluation -> render dashboard -> run manifest.
"""

import argparse
import hashlib
import sys
from pathlib import Path

from connector.tools.build_market_book_catalog import build_catalog
from connector.tools.io_utils import (
    dump_json,
    load_json,
    load_yaml,
    now_utc_iso,
    sha256_bytes,
    write_bytes,
    write_json,
    write_text,
    write_yaml,
)
from connector.tools.mock_backend import invoke_mock
from connector.tools.paths import (
    catalog_dir,
    ensure_dir,
    golden_inputs_dir,
    project_root,
    prompts_dir,
    runs_dir,
    schemas_dir,
)
from connector.tools.frozen_index_resolver import resolve_frozen_index_dir
from connector.tools.render_dashboard_report import write_report
from connector.tools.retrieve_candidate_entries import build_trace, retrieve
from connector.tools.run_agent_backend import invoke_cli, invoke_cli_with_retry
from connector.tools.export_signal_context import export_signal_context
from connector.tools.validate_market_book_evaluation import validate_evaluation
from connector.tools.validate_observation_packet import load_vocab, validate_packet

MARKET_BOOK_VERSION = "stage6_accepted"
# The retriever embeds the ENRICHED catalog (state_summary + linked_records).
# This is a derived artifact of the accepted corpus; MARKET_BOOK_VERSION (the
# accepted body of knowledge) is unchanged (F7).
CATALOG_ENRICHED_VERSION = "stage6_enriched"


def _validate_raw_input(raw_bytes: bytes) -> tuple[bool, list[str]]:
    """Deterministic sanity check on raw market input. Fail closed on malformed input.

    The connector accepts human-authored market observation text. Input that is
    empty, whitespace-only, binary/NUL-laden, or not decodable as UTF-8 is
    rejected before any model role runs, so malformed input can never produce a
    false-valid signal context.
    """
    errors: list[str] = []
    if len(raw_bytes) == 0:
        errors.append("raw input is empty")
        return False, errors
    if b"\x00" in raw_bytes:
        errors.append("raw input contains NUL bytes; text input required")
        return False, errors
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        errors.append("raw input is not valid UTF-8 text")
        return False, errors
    if text.strip() == "":
        errors.append("raw input is whitespace-only")
        return False, errors
    return True, errors


def _resolve_role_backend_name(
    *,
    role_key: str,
    role_cfg: dict,
    default_backend: str,
    override_backend: str | None,
) -> str:
    """Resolve the backend profile name for a role.

    Precedence: explicit CLI override > role-config ``backend_profile`` >
    ``--backend`` default.
    """
    if override_backend:
        return override_backend
    if role_cfg.get("backend_profile"):
        return str(role_cfg["backend_profile"])
    return default_backend


def _merged_profile(backend_profiles_cfg: dict, profile_name: str) -> dict:
    """Return the named profile with the top-level ``defaults`` merged in.

    Profile-specific keys override defaults.
    """
    defaults = backend_profiles_cfg.get("defaults", {}) or {}
    return {**defaults, **backend_profiles_cfg["profiles"][profile_name]}


def _backend_failure_reason(result: dict) -> str | None:
    """Return a human-readable failure reason for a backend result, or None."""
    if result.get("failure_reason"):
        return str(result["failure_reason"])
    if result.get("output_obj") is None:
        return "expected output object missing"
    return None


def _preflight_backend_selection(
    *,
    backend_profiles_cfg: dict,
    roles_cfg: dict,
    default_backend: str,
    builder_backend_override: str | None,
    evaluator_backend_override: str | None,
    backend_config_path: Path,
) -> dict:
    """Validate every backend/profile/role selection before any side effects.

    Runs entirely before the run directory is created or any input is read, so
    an invalid backend/profile/role configuration fails cleanly with no run
    artifacts left behind. This is a configuration preflight: it does NOT create
    directories, read raw input, write files, create manifests, or invoke any
    backend (mock or CLI).

    Returns the validated role configs plus the resolved profile names and their
    merged profiles. Exits nonzero (printing to stderr) on any invalid name.
    """
    profiles_map = backend_profiles_cfg.get("profiles", {})
    roles_map = roles_cfg.get("roles", {})

    # The required --backend argument must always identify a real profile, even
    # when roles.example.yaml overrides the runtime role backends. An invalid
    # default backend must fail loudly rather than be accepted silently.
    if default_backend not in profiles_map:
        print(
            f"Error: backend profile '{default_backend}' not found in {backend_config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    # The two required roles must exist in the role config.
    builder_role_key = "observation_packet_builder"
    evaluator_role_key = "market_book_evaluator"
    for role_key in (builder_role_key, evaluator_role_key):
        if role_key not in roles_map:
            print(
                f"Error: required role '{role_key}' missing from role config",
                file=sys.stderr,
            )
            sys.exit(1)

    builder_role = roles_map[builder_role_key]
    evaluator_role = roles_map[evaluator_role_key]

    # Resolve role-specific backend profiles. --backend is kept for
    # backward-compatible smoke runs; roles.example.yaml may select a profile
    # per role, and --builder-backend / --evaluator-backend override it.
    builder_profile_name = _resolve_role_backend_name(
        role_key=builder_role_key,
        role_cfg=builder_role,
        default_backend=default_backend,
        override_backend=builder_backend_override,
    )
    if builder_profile_name not in profiles_map:
        print(
            f"Error: backend profile '{builder_profile_name}' selected for role "
            f"'{builder_role_key}' not found in {backend_config_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    builder_profile = _merged_profile(backend_profiles_cfg, builder_profile_name)

    evaluator_profile_name = _resolve_role_backend_name(
        role_key=evaluator_role_key,
        role_cfg=evaluator_role,
        default_backend=default_backend,
        override_backend=evaluator_backend_override,
    )
    if evaluator_profile_name not in profiles_map:
        print(
            f"Error: backend profile '{evaluator_profile_name}' selected for role "
            f"'{evaluator_role_key}' not found in {backend_config_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    evaluator_profile = _merged_profile(backend_profiles_cfg, evaluator_profile_name)

    return {
        "builder_role": builder_role,
        "evaluator_role": evaluator_role,
        "builder_profile_name": builder_profile_name,
        "evaluator_profile_name": evaluator_profile_name,
        "builder_profile": builder_profile,
        "evaluator_profile": evaluator_profile,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the Market Book downstream connector pipeline."
    )
    parser.add_argument("--raw-input", required=True, help="Path to raw input file.")
    parser.add_argument("--backend-config", required=True, help="Path to backend profiles YAML.")
    parser.add_argument("--role-config", required=True, help="Path to roles config YAML.")
    parser.add_argument("--backend", required=True, help="Backend profile name to use.")
    parser.add_argument("--builder-backend", default=None, help="Override backend profile for observation_packet_builder.")
    parser.add_argument("--evaluator-backend", default=None, help="Override backend profile for market_book_evaluator.")
    parser.add_argument("--out", required=True, help="Output (run) directory.")
    parser.add_argument("--app-config", default=None, help="Path to connector_app config YAML.")
    parser.add_argument("--catalog", default=None, help="Path to catalog JSON (overrides default).")
    parser.add_argument(
        "--vocab-dir",
        default=None,
        help="Directory containing vocabulary YAML files (defaults to connector/vocab).",
    )
    parser.add_argument("--run-utc", default=None, help="UTC timestamp for run (ISO 8601).")
    args = parser.parse_args(argv)

    # -----------------------------------------------------------------------
    # a. Preflight all backend/profile/role selections BEFORE any side effects.
    #    This runs before the run directory is created or any input is read, so
    #    an invalid backend/profile/role configuration fails cleanly with no run
    #    artifacts left behind.
    # -----------------------------------------------------------------------
    backend_profiles_cfg = load_yaml(Path(args.backend_config))
    roles_cfg = load_yaml(Path(args.role_config))

    selection = _preflight_backend_selection(
        backend_profiles_cfg=backend_profiles_cfg,
        roles_cfg=roles_cfg,
        default_backend=args.backend,
        builder_backend_override=args.builder_backend,
        evaluator_backend_override=args.evaluator_backend,
        backend_config_path=Path(args.backend_config),
    )

    builder_role_key = "observation_packet_builder"
    evaluator_role_key = "market_book_evaluator"
    builder_role = selection["builder_role"]
    evaluator_role = selection["evaluator_role"]
    builder_profile_name = selection["builder_profile_name"]
    evaluator_profile_name = selection["evaluator_profile_name"]
    builder_profile = selection["builder_profile"]
    evaluator_profile = selection["evaluator_profile"]
    backend_profiles_manifest = {
        builder_role_key: builder_profile_name,
        evaluator_role_key: evaluator_profile_name,
    }

    # Frozen index of record preflight: verify BEFORE any stage runs (precedes all LLM spend).
    # Serving path READS; it never builds (ruling 2026-07-06). resolve_frozen_index_dir() checks
    # existence/completeness/three-way anchor and fails loud if glossed/e5 is absent or drifted.
    resolve_frozen_index_dir()

    # -----------------------------------------------------------------------
    # b. Create the run directory (only after preflight passes).
    # -----------------------------------------------------------------------
    run_dir = Path(args.out).resolve()
    ensure_dir(run_dir)
    run_id = run_dir.name
    created_at_utc = args.run_utc or now_utc_iso()

    # -----------------------------------------------------------------------
    # c. Capture raw input
    # -----------------------------------------------------------------------
    raw_input_path = Path(args.raw_input)
    raw_bytes = raw_input_path.read_bytes()
    raw_hash_hex = sha256_bytes(raw_bytes)
    raw_hash = f"sha256:{raw_hash_hex}"

    # Deterministic sanity check: reject malformed input before any model role.
    valid_input, errors = _validate_raw_input(raw_bytes)
    if not valid_input:
        write_bytes(run_dir / "raw_input.md", raw_bytes)
        write_text(run_dir / "raw_input_hash.txt", raw_hash)
        write_json(run_dir / "raw_input_metadata.json", {
            "source_path": str(raw_input_path),
            "byte_length": len(raw_bytes),
            "sha256": raw_hash,
            "captured_at_utc": created_at_utc,
            "source_type": "dashboard_text",
            "valid": False,
            "validation_errors": errors,
        })
        _write_run_manifest(
            run_dir=run_dir,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=args.backend,
            raw_input_hash=raw_hash,
            overall_status="failed_raw_input_validation",
            failed_step="capture_raw_input",
            reason="; ".join(errors),
            steps=[{"name": "capture_raw_input", "status": "failed", "reason": "; ".join(errors)}],
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )
        print(
            f"Connector pipeline FAILED at step 'capture_raw_input': {'; '.join(errors)}",
            file=sys.stderr,
        )
        sys.exit(1)

    write_text(run_dir / "raw_input.md", raw_bytes.decode("utf-8"))
    write_text(run_dir / "raw_input_hash.txt", raw_hash)
    write_json(run_dir / "raw_input_metadata.json", {
        "source_path": str(raw_input_path),
        "byte_length": len(raw_bytes),
        "sha256": raw_hash,
        "captured_at_utc": created_at_utc,
        "source_type": "dashboard_text",
    })

    observation_id = f"obs_{raw_hash_hex[:12]}"

    # Catalog (enriched: carries state_summary + linked_records for retrieval).
    if args.catalog:
        catalog_path = Path(args.catalog)
    else:
        catalog_path = catalog_dir() / f"market_book_catalog.{CATALOG_ENRICHED_VERSION}.json"
    catalog = load_json(catalog_path)

    # Vocab
    if args.vocab_dir:
        vocab = load_vocab(Path(args.vocab_dir))
    else:
        vocab = load_vocab()

    prohibitions = vocab["prohibited_phrases"]

    # -----------------------------------------------------------------------
    # c. Builder role
    # -----------------------------------------------------------------------
    builder_subdir = "builder"
    builder_output_filename = builder_role["output_filename"]
    builder_output_path = run_dir / builder_subdir / builder_output_filename

    # Materialise the builder context bundle (raw input + schema + vocab).
    builder_context_dir = _write_builder_context(
        run_dir,
        raw_text=raw_bytes.decode("utf-8"),
        output_path=builder_output_path,
    )

    # Build prompt text: raw input + prohibitions + schema/vocab references
    builder_prompt = _build_builder_prompt(
        raw_text=raw_bytes.decode("utf-8"),
        prohibitions=prohibitions,
        created_at_utc=created_at_utc,
        role_prompt_path=project_root() / builder_role["prompt_file"],
        schema_path=schemas_dir() / "market_observation_packet.schema.yaml",
        context_dir=builder_context_dir,
        output_path=builder_output_path,
    )

    patches = {
        "observation_id": observation_id,
        "input_reference.raw_input_hash": raw_hash,
        "input_reference.raw_input_path": f"{run_id}/raw_input.md",
    }

    context_manifest_builder = {
        "role": builder_role_key,
        "observation_id": observation_id,
        "raw_input_hash": raw_hash,
        "created_at_utc": created_at_utc,
        "backend": builder_profile_name,
        "context_dir": str(builder_context_dir),
    }

    if builder_profile.get("backend_type") == "mock":
        golden_output_dir = builder_role.get("golden_output_dir", "")
        golden_output_filename = builder_role.get("golden_output_filename", builder_output_filename)
        golden_source = project_root() / golden_output_dir / golden_output_filename
        builder_result = invoke_mock(
            role=builder_subdir,
            prompt_text=builder_prompt,
            run_dir=run_dir,
            profile=builder_profile,
            golden_source=golden_source,
            expected_filename=builder_output_filename,
            patches=patches,
            context_manifest=context_manifest_builder,
        )
    else:
        def _validate_builder(obj):
            if obj is None:
                return False, ["no packet produced"]
            v = validate_packet(obj, vocab=vocab)
            return bool(v.get("valid")), list(v.get("errors") or [])
        builder_result = invoke_cli_with_retry(
            profile=builder_profile,
            role=builder_subdir,
            prompt_text=builder_prompt,
            run_dir=run_dir,
            expected_filename=builder_output_filename,
            context_manifest=context_manifest_builder,
            validate=_validate_builder,
            max_attempts=3,
        )

    # A CLI backend may time out, exit nonzero, or fail to write/parse the
    # expected output. Fail cleanly instead of crashing later on a None packet.
    builder_failure = _backend_failure_reason(builder_result)
    if builder_failure:
        _fail_backend(
            run_dir,
            subdir=builder_subdir,
            role_key=builder_role_key,
            reason=builder_failure,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=builder_profile_name,
            raw_input_hash=raw_hash,
            backend_result=builder_result,
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )

    # -----------------------------------------------------------------------
    # d. Validate packet
    # -----------------------------------------------------------------------
    validation_dir = ensure_dir(run_dir / "validation")
    packet = builder_result["output_obj"]
    packet_validation = validate_packet(packet, vocab=vocab)
    write_json(validation_dir / "observation_packet_validation.json", packet_validation)

    if not packet_validation["valid"]:
        write_report(run_dir)
        _write_run_manifest(
            run_dir=run_dir,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=args.backend,
            raw_input_hash=raw_hash,
            overall_status="failed_observation_packet_validation",
            steps=[],
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )
        print(f"Packet validation failed: {packet_validation['errors']}")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # e. Retrieve
    # -----------------------------------------------------------------------
    retrieval_dir = ensure_dir(run_dir / "retrieval")
    # Retrieval resolves through the frozen pointer (index + params); serving path never builds
    # (ruling 2026-07-06).
    candidate_set = retrieve(packet)
    retrieval_trace = build_trace(packet)
    write_json(retrieval_dir / "candidate_entries.json", candidate_set)
    write_json(retrieval_dir / "retrieval_trace.json", retrieval_trace)
    candidate_set_id = candidate_set["candidate_set_id"]

    # -----------------------------------------------------------------------
    # f. Evaluator role
    # -----------------------------------------------------------------------
    evaluator_subdir = "evaluator"
    evaluator_output_filename = evaluator_role["output_filename"]
    evaluator_output_path = run_dir / evaluator_subdir / evaluator_output_filename

    # Materialise the evaluator context bundle (validated packet + candidates
    # + full candidate Market Book content + schema) so a real CLI evaluator
    # has the actual data to reason over.
    evaluator_context_dir = _write_evaluator_context(
        run_dir,
        packet=packet,
        candidate_set=candidate_set,
        catalog=catalog,
        output_path=evaluator_output_path,
    )

    evaluator_prompt = _build_evaluator_prompt(
        packet=packet,
        candidate_set=candidate_set,
        prohibitions=prohibitions,
        role_prompt_path=project_root() / evaluator_role["prompt_file"],
        packet_context_path=evaluator_context_dir / "market_observation_packet.yaml",
        candidate_context_path=evaluator_context_dir / "candidate_entries.json",
        candidate_full_context_path=evaluator_context_dir / "market_book_candidate_entries_full.json",
        schema_path=schemas_dir() / "market_book_evaluation.schema.yaml",
        output_path=evaluator_output_path,
    )

    eval_patches = {
        "observation_id": packet["observation_id"],
        "candidate_set_id": candidate_set_id,
    }

    context_manifest_evaluator = {
        "role": evaluator_role_key,
        "observation_id": packet["observation_id"],
        "candidate_set_id": candidate_set_id,
        "candidate_count": len(candidate_set["candidates"]),
        "created_at_utc": created_at_utc,
        "backend": evaluator_profile_name,
        "context_dir": str(evaluator_context_dir),
        "candidate_metadata_path": str(evaluator_context_dir / "candidate_entries.json"),
        "candidate_full_context_path": str(evaluator_context_dir / "market_book_candidate_entries_full.json"),
        "packet_context_path": str(evaluator_context_dir / "market_observation_packet.yaml"),
    }

    if evaluator_profile.get("backend_type") == "mock":
        golden_output_dir = evaluator_role.get("golden_output_dir", "")
        golden_output_filename = evaluator_role.get("golden_output_filename", evaluator_output_filename)
        golden_source = project_root() / golden_output_dir / golden_output_filename
        evaluator_result = invoke_mock(
            role=evaluator_subdir,
            prompt_text=evaluator_prompt,
            run_dir=run_dir,
            profile=evaluator_profile,
            golden_source=golden_source,
            expected_filename=evaluator_output_filename,
            patches=eval_patches,
            context_manifest=context_manifest_evaluator,
        )
    else:
        def _validate_evaluator(obj):
            if obj is None:
                return False, ["no evaluation produced"]
            v = validate_evaluation(obj, packet=packet, candidate_set=candidate_set, catalog=catalog, vocab=vocab)
            return bool(v.get("valid")), list(v.get("errors") or [])
        evaluator_result = invoke_cli_with_retry(
            profile=evaluator_profile,
            role=evaluator_subdir,
            prompt_text=evaluator_prompt,
            run_dir=run_dir,
            expected_filename=evaluator_output_filename,
            context_manifest=context_manifest_evaluator,
            validate=_validate_evaluator,
            max_attempts=3,
        )

    # Controlled failure if the CLI backend did not produce parseable output.
    evaluator_failure = _backend_failure_reason(evaluator_result)
    if evaluator_failure:
        _fail_backend(
            run_dir,
            subdir=evaluator_subdir,
            role_key=evaluator_role_key,
            reason=evaluator_failure,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=evaluator_profile_name,
            raw_input_hash=raw_hash,
            backend_result=evaluator_result,
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )

    # -----------------------------------------------------------------------
    # g. Validate evaluation
    # -----------------------------------------------------------------------
    evaluation = evaluator_result["output_obj"]
    eval_validation = validate_evaluation(
        evaluation,
        packet=packet,
        candidate_set=candidate_set,
        catalog=catalog,
        vocab=vocab,
    )
    write_json(validation_dir / "market_book_evaluation_validation.json", eval_validation)

    if not eval_validation["valid"]:
        write_report(run_dir)
        _write_run_manifest(
            run_dir=run_dir,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=args.backend,
            raw_input_hash=raw_hash,
            overall_status="failed_market_book_evaluation_validation",
            steps=[],
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )
        print(f"Evaluation validation failed: {eval_validation['errors']}")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # h. Export signal context
    # -----------------------------------------------------------------------
    signal_result = export_signal_context(
        run_dir=run_dir, run_id=run_id, raw_input_hash=raw_hash,
        packet=packet, candidate_set=candidate_set, evaluation=evaluation,
        eval_validation=eval_validation, catalog=catalog, vocab=vocab,
    )
    if not signal_result["valid"] or signal_result.get("context_path") is None:
        write_report(run_dir)
        signal_reasons = signal_result.get("artifacts", [])
        _write_run_manifest(
            run_dir=run_dir,
            run_id=run_id,
            created_at_utc=created_at_utc,
            backend=args.backend,
            raw_input_hash=raw_hash,
            overall_status="failed_signal_context_export",
            steps=[],
            failed_step="export_signal_context",
            reason="signal context export failed (fail-closed)",
            default_backend=args.backend,
            backend_profiles=backend_profiles_manifest,
        )
        print("Signal context export failed (fail-closed).", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # i. Write run manifest (before render so the report can read run metadata)
    # -----------------------------------------------------------------------
    steps = [
        {
            "name": "capture_raw_input",
            "status": "ok",
            "artifacts": ["raw_input.md", "raw_input_hash.txt", "raw_input_metadata.json"],
        },
        {
            "name": "observation_packet_builder",
            "status": "ok",
            "backend": builder_profile_name,
            "valid": True,
            "artifacts": builder_result["artifacts"],
        },
        {
            "name": "validate_observation_packet",
            "status": "ok",
            "valid": True,
            "artifacts": ["validation/observation_packet_validation.json"],
        },
        {
            "name": "retrieve_candidate_entries",
            "status": "ok",
            "candidates": len(candidate_set["candidates"]),
            "artifacts": [
                "retrieval/candidate_entries.json",
                "retrieval/retrieval_trace.json",
            ],
        },
        {
            "name": "market_book_evaluator",
            "status": "ok",
            "backend": evaluator_profile_name,
            "valid": True,
            "artifacts": evaluator_result["artifacts"],
        },
        {
            "name": "validate_market_book_evaluation",
            "status": "ok",
            "valid": True,
            "artifacts": ["validation/market_book_evaluation_validation.json"],
        },
        {
            "name": "export_signal_context",
            "status": "ok",
            "valid": True,
            "artifacts": [
                "signal_context/market_state_context.json",
                "signal_context/market_state_context.yaml",
                "signal_context/export_metadata.json",
                "signal_context/validation.json",
            ],
        },
        {
            "name": "render_dashboard_report",
            "status": "ok",
            "artifacts": [
                "dashboard/dashboard_report.md",
                "dashboard/dashboard_report.json",
            ],
        },
    ]

    _write_run_manifest(
        run_dir=run_dir,
        run_id=run_id,
        created_at_utc=created_at_utc,
        backend=args.backend,
        raw_input_hash=raw_hash,
        overall_status="ok",
        steps=steps,
        default_backend=args.backend,
        backend_profiles=backend_profiles_manifest,
    )

    # -----------------------------------------------------------------------
    # h. Render dashboard (after manifest so it can read run metadata)
    # -----------------------------------------------------------------------
    write_report(run_dir)

    print(f"Pipeline completed successfully. Run ID: {run_id}")
    print(f"Output directory: {run_dir}")


def _build_builder_prompt(
    *,
    raw_text: str,
    prohibitions: list[str],
    created_at_utc: str,
    role_prompt_path: Path,
    schema_path: Path,
    context_dir: Path,
    output_path: Path,
) -> str:
    """Build the prompt text for the observation_packet_builder role.

    The role's prompt_file (from the role config) is embedded verbatim as the
    instruction head, followed by the run-specific input and output paths.
    """
    role_text = Path(role_prompt_path).read_text(encoding="utf-8")

    lines = [
        "# Run Inputs: Build a Market Observation Packet",
        "",
        f"Timestamp: {created_at_utc}",
        "",
        "## Context Inputs",
        "",
        "The raw input is inlined below and also available, alongside the packet",
        "schema, in the builder context directory:",
        "",
        f"- context dir:    {context_dir}",
        f"- raw input:      {context_dir / 'raw_input.md'}",
        f"- packet schema:  {schema_path}  (copy: {context_dir / 'market_observation_packet.schema.yaml'})",
        "",
        "## Raw Input",
        "",
        raw_text,
        "",
        "## Prohibited Phrases",
        "",
        "The following direct trading-instruction phrases must NOT appear in any",
        "free-text output field (narrative_summary, evidence_note, statements):",
    ]
    for phrase in prohibitions:
        lines.append(f"- {phrase}")
    lines.extend([
        "",
        "## Output",
        "",
        f"Output ONLY the YAML document conforming to the observation packet schema.",
        f"Print the YAML to standard output. Do not write files. Do not include",
        f"Markdown code fences, commentary, or any text before or after the YAML.",
        f"Every string value that contains a colon (\":\") MUST be wrapped in double quotes.",
        f"List items must be short descriptors, not full sentences.",
        f"{output_path}",
    ])
    return role_text + "\n---\n\n" + "\n".join(lines)


def _build_evaluator_prompt(
    *,
    packet: dict,
    candidate_set: dict,
    prohibitions: list[str],
    role_prompt_path: Path,
    packet_context_path: Path,
    candidate_context_path: Path,
    candidate_full_context_path: Path,
    schema_path: Path,
    output_path: Path,
) -> str:
    """Build the prompt text for the market_book_evaluator role.

    The role's prompt_file (from the role config) is embedded verbatim as the
    instruction head; the dynamic block that follows carries only the
    run-specific data: candidate entry IDs, the validated packet's summary and
    observations, the context bundle paths, prohibitions, and the output
    contract.
    """
    role_text = Path(role_prompt_path).read_text(encoding="utf-8")

    candidates = candidate_set.get("candidates", [])
    candidate_ids = [c.get("entry_id", "") for c in candidates]

    observations = packet.get("observations", []) or []
    observation_lines = []
    for o in observations:
        observation_lines.append(
            f"  - statement={o.get('statement','')}, dimension={o.get('dimension','')}, "
            f"direction={o.get('direction') or ''}, magnitude={o.get('magnitude') or ''}"
        )

    lines = [
        "# Run Inputs: Evaluate Market Book Candidates",
        "",
        f"Observation ID: {packet.get('observation_id', '')}",
        f"Candidate set ID: {candidate_set.get('candidate_set_id', '')}",
        f"Number of candidate entries: {len(candidates)}",
        "",
        "## Constraint: reference ONLY the provided candidate entries",
        "",
        "You may match, partially match, or reject ONLY the candidate entries",
        "listed below. Do NOT invent entry IDs, state IDs, or candidate set IDs.",
        "Do NOT cite rejected or needs-review claims.",
        "",
        "## Candidate Entry IDs (use ONLY these)",
        "",
    ]
    for cid in candidate_ids:
        lines.append(f"- {cid}")
    lines.extend([
        "",
        "## Validated Observation Packet (inlined summary)",
        "",
        f"- observation_id: {packet.get('observation_id', '')}",
        f"- summary: {packet.get('summary', '')}",
        "- observations:",
    ])
    lines.extend(observation_lines or ["  (none)"])
    lines.extend([
        "",
        "## Full Context Inputs (files on disk)",
        "",
        f"- validated packet (full):     {packet_context_path}",
        f"- candidate retrieval metadata: {candidate_context_path}",
        f"- candidate entries (full):     {candidate_full_context_path}",
        f"- evaluation schema:           {schema_path}",
        "",
        "## Prohibited Phrases",
        "",
        "The following direct trading-instruction phrases must NOT appear in",
        "evidence_caveats or overall_interpretation.summary (they ARE allowed in",
        "the prohibited_actions field):",
    ])
    for phrase in prohibitions:
        lines.append(f"- {phrase}")
    lines.extend([
        "",
        "## Output",
        "",
        f"Output ONLY the YAML document conforming to the evaluation schema.",
        f"Print the YAML to standard output. Do not write files. Do not include",
        f"Markdown code fences, commentary, or any text before or after the YAML.",
        f"Every string value that contains a colon (\":\") MUST be wrapped in double quotes.",
        f"List items must be short descriptors, not full sentences.",
        f"{output_path}",
    ])
    return role_text + "\n---\n\n" + "\n".join(lines)


def _write_run_manifest(
    *,
    run_dir: Path,
    run_id: str,
    created_at_utc: str,
    backend: str,
    raw_input_hash: str,
    overall_status: str,
    steps: list[dict],
    failed_step: str | None = None,
    reason: str | None = None,
    default_backend: str | None = None,
    backend_profiles: dict | None = None,
) -> None:
    """Write the run_manifest.json file."""
    manifest = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "created_at_utc": created_at_utc,
        "backend": backend,
        "default_backend": default_backend if default_backend is not None else backend,
        "backend_profiles": backend_profiles or {},
        "raw_input_hash": raw_input_hash,
        "market_book_version": MARKET_BOOK_VERSION,
        "overall_status": overall_status,
        "failed_step": failed_step,
        "reason": reason,
        "steps": steps,
    }
    write_json(run_dir / "run_manifest.json", manifest)


def _fail_backend(
    run_dir: Path,
    *,
    subdir: str,
    role_key: str,
    reason: str,
    run_id: str,
    created_at_utc: str,
    backend: str,
    raw_input_hash: str,
    backend_result: dict | None = None,
    default_backend: str | None = None,
    backend_profiles: dict | None = None,
) -> None:
    """Record a controlled backend-output failure and exit nonzero cleanly.

    Writes ``<subdir>/backend_failure.json`` and a failed ``run_manifest.json``
    so the failure is understandable without reading a Python traceback. When
    ``backend_result`` is supplied, its diagnostics are included; otherwise the
    diagnostic fields are null.
    """
    role_dir = ensure_dir(run_dir / subdir)
    bd = backend_result or {}
    write_json(role_dir / "backend_failure.json", {
        "failed_step": role_key,
        "reason": reason,
        "failed_at_utc": now_utc_iso(),
        "exit_code": bd.get("exit_code"),
        "timed_out": bd.get("timed_out"),
        "expected_output_exists": bd.get("expected_output_exists"),
        "parse_error": bd.get("parse_error"),
        "output_path": bd.get("output_path"),
    })
    manifest_backend = default_backend if default_backend is not None else backend
    _write_run_manifest(
        run_dir=run_dir,
        run_id=run_id,
        created_at_utc=created_at_utc,
        backend=manifest_backend,
        raw_input_hash=raw_input_hash,
        overall_status="failed",
        failed_step=role_key,
        reason=reason,
        steps=[{"name": role_key, "status": "failed", "reason": reason}],
        default_backend=manifest_backend,
        backend_profiles=backend_profiles or {},
    )
    print(
        f"Connector pipeline FAILED at step '{role_key}': {reason}",
        file=sys.stderr,
    )
    sys.exit(1)


def _write_builder_context(run_dir: Path, *, raw_text: str, output_path: Path) -> Path:
    """Materialise the builder context bundle under builder/context/."""
    ctx = ensure_dir(run_dir / "builder" / "context")
    write_text(ctx / "raw_input.md", raw_text)
    (ctx / "market_observation_packet.schema.yaml").write_text(
        (schemas_dir() / "market_observation_packet.schema.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    write_text(ctx / "expected_output_file.txt", str(output_path))
    return ctx


def _write_evaluator_context(
    run_dir: Path,
    *,
    packet: dict,
    candidate_set: dict,
    catalog: dict,
    output_path: Path,
) -> Path:
    """Materialise the evaluator context bundle under evaluator/context/.

    Writes the retrieval metadata (candidate_entries.json) unchanged plus a
    full Market Book candidate bundle (market_book_candidate_entries_full.json)
    that carries the actual catalog content for each candidate, so a real CLI
    evaluator can reason over the entries rather than IDs alone.
    """
    ctx = ensure_dir(run_dir / "evaluator" / "context")
    write_yaml(ctx / "market_observation_packet.yaml", packet)
    write_json(ctx / "candidate_entries.json", candidate_set)

    catalog_by_entry: dict[str, dict] = {}
    for entry in catalog.get("entries", []):
        if isinstance(entry, dict) and entry.get("entry_id"):
            catalog_by_entry[entry["entry_id"]] = entry

    full_entries: list[dict] = []
    for cand in candidate_set.get("candidates", []):
        entry_id = cand.get("entry_id", "")
        if entry_id not in catalog_by_entry:
            raise ValueError(f"candidate entry missing from catalog: {entry_id}")
        ce = catalog_by_entry[entry_id]
        full_entries.append({
            "entry_id": entry_id,
            "state_id": cand.get("state_id", ce.get("state_id", "")),
            "retrieval_score": cand.get("retrieval_score"),
            "retrieval_reasons": cand.get("retrieval_reasons", []),
            "state_summary": ce.get("state_summary", ""),
            "observable_signatures": ce.get("observable_signatures", []),
            "failure_patterns": ce.get("failure_patterns", []),
            "policy_implications": ce.get("policy_implications", []),
            "supporting_claims": ce.get("supporting_claims", []),
            "tags": ce.get("tags", []),
            "text": ce.get("text", ""),
        })

    write_json(ctx / "market_book_candidate_entries_full.json", {
        "market_book_version": MARKET_BOOK_VERSION,
        "observation_id": packet.get("observation_id", ""),
        "candidate_set_id": candidate_set.get("candidate_set_id", ""),
        "candidate_count": len(full_entries),
        "entries": full_entries,
    })

    (ctx / "market_book_evaluation.schema.yaml").write_text(
        (schemas_dir() / "market_book_evaluation.schema.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    write_text(ctx / "expected_output_file.txt", str(output_path))
    return ctx


if __name__ == "__main__":
    main()
