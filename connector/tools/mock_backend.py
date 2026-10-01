"""Mock backend for the Market Book downstream connector.

Copies golden output files, applies runtime patches (e.g. observation_id,
raw_input_hash), and writes all prompt artifacts. Works fully offline.
"""

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from connector.tools.io_utils import (
    dump_json,
    load_yaml,
    now_utc_iso,
    sha256_bytes,
    write_json,
    write_text,
    write_yaml,
)
from connector.tools.paths import ensure_dir


def _apply_patches(obj: dict, patches: dict) -> dict:
    """Apply dotted-path patches to a nested dict.

    Keys like "observation_id" set top-level values. Keys like
    "input_reference.raw_input_hash" navigate nested dicts.
    """
    result = deepcopy(obj)
    for dotted_key, value in patches.items():
        parts = dotted_key.split(".")
        target = result
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    return result


def _write_backend_invocation(
    role_dir: Path,
    *,
    backend: str,
    profile_backend_type: str,
    command_template: str,
    cwd: str,
    timeout_seconds: int,
    started_at_utc: str,
    completed_at_utc: str,
    exit_code: int,
    timed_out: bool,
    stdout_len: int,
    stderr_len: int,
    expected_output_file: str,
    expected_output_exists: bool,
    prompt_hash: str,
    env_redacted: bool,
) -> None:
    invocation = {
        "backend": backend,
        "profile_backend_type": profile_backend_type,
        "command_template": command_template,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_len": stdout_len,
        "stderr_len": stderr_len,
        "expected_output_file": expected_output_file,
        "expected_output_exists": expected_output_exists,
        "prompt_hash": prompt_hash,
        "env_redacted": env_redacted,
    }
    write_json(role_dir / "backend_invocation.json", invocation)


def invoke_mock(
    role: str,
    prompt_text: str,
    run_dir: Path,
    *,
    profile: dict,
    golden_source: Path,
    expected_filename: str,
    patches: dict,
    context_manifest: dict,
) -> dict:
    """Invoke the mock backend for a given role.

    Copies the golden source YAML into run_dir/role/expected_filename after
    applying runtime patches. Writes the standard 7 prompt artifacts plus
    backend_invocation.json.

    Returns dict with output_path, output_obj, artifacts (list of relpaths).
    """
    run_dir = Path(run_dir)
    role_dir = ensure_dir(run_dir / role)

    # Load and patch golden output.
    golden_obj = load_yaml(golden_source)
    patched_obj = _apply_patches(golden_obj, patches)

    # Write patched output.
    output_path = role_dir / expected_filename
    write_yaml(output_path, patched_obj)

    # Prompt hash.
    prompt_hash = sha256_bytes(prompt_text.encode("utf-8"))

    # Timestamps.
    started_at_utc = now_utc_iso()
    completed_at_utc = now_utc_iso()

    # Compute expected_output_file relative path from profile template.
    expected_output_rel = str(Path(role) / expected_filename)

    # Write 7 prompt artifacts.
    write_text(role_dir / "prompt.md", prompt_text)
    write_text(role_dir / "prompt_hash.txt", prompt_hash)
    write_json(role_dir / "context_manifest.json", context_manifest)
    # Mock-visibility warning: loud on stderr AND in the run record, so a fixture
    # emission is never silent. Fires for any role on the mock backend (role
    # interpolated, never hardcoded).
    mock_warning = (
        f"MOCK BACKEND ({role}): emitting fixture output — NOT derived from your input"
    )
    print(mock_warning, file=sys.stderr)
    write_text(role_dir / "stdout.txt", "")
    write_text(role_dir / "stderr.txt", mock_warning + "\n")
    write_text(role_dir / "exit_code.txt", "0")

    # Write backend_invocation.json.
    _write_backend_invocation(
        role_dir,
        backend="mock",
        profile_backend_type=profile.get("backend_type", "mock"),
        command_template="mock:copy_golden",
        cwd=str(run_dir),
        timeout_seconds=profile.get("timeout_seconds", 30),
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        exit_code=0,
        timed_out=False,
        stdout_len=0,
        stderr_len=len((mock_warning + "\n").encode("utf-8")),
        expected_output_file=expected_output_rel,
        expected_output_exists=True,
        prompt_hash=prompt_hash,
        env_redacted=True,
    )

    artifacts = [
        f"{role}/prompt.md",
        f"{role}/prompt_hash.txt",
        f"{role}/context_manifest.json",
        f"{role}/stdout.txt",
        f"{role}/stderr.txt",
        f"{role}/exit_code.txt",
        f"{role}/backend_invocation.json",
        f"{role}/{expected_filename}",
    ]

    return {
        "output_path": str(output_path),
        "output_obj": patched_obj,
        "artifacts": artifacts,
    }
