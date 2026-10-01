"""CLI backend runner for the Market Book downstream connector.

Builds and executes a command from a backend profile, captures stdout/stderr,
writes all prompt artifacts, and reads the expected output file produced by
the CLI. No direct API/SDK calls; only subprocess invocation.
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from connector.tools.io_utils import (
    load_json,
    load_yaml,
    now_utc_iso,
    sha256_bytes,
    write_json,
    write_text,
)
from connector.tools.paths import ensure_dir, project_root


def _format_placeholders(template: str, replacements: dict[str, str]) -> str:
    """Replace every ``{placeholder}`` token in ``template`` with its value."""
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def _strip_code_fences(text: str) -> str:
    """Extract a YAML document from possibly chatty CLI stdout.
    Handles: clean YAML; a ```/```yaml fenced block anywhere in the text
    (preamble/postamble stripped); and as a fallback, trims any prose before
    the first top-level 'key:' line. Returns text unchanged if nothing matches."""
    import re as _re
    m = _re.search(r"```(?:ya?ml)?\s*\n(.*?)\n```", text, _re.DOTALL)
    if m:
        return m.group(1).strip() + "\n"
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if _re.match(r"^[A-Za-z_][\w-]*:", ln):
            return "\n".join(lines[i:]).strip() + "\n"
    return text.strip() + "\n"


def _classify_backend_error(role_dir, backend_reason):
    """Classify a backend failure. Returns one of: 'transient', 'malformed', 'none'.

    transient: upstream/gateway problem (HTTP 529/5xx, 'overloaded', rate limit,
               connection/timeout) -- the model never produced output, so feeding
               the error back as a 'fix your YAML' correction is pointless. Back off.
    malformed: the model ran but produced unparseable / schema-invalid output -- the
               existing feedback-and-retry is the right response.
    """
    if not backend_reason:
        return "none"
    import re as _re
    blob = str(backend_reason)
    try:
        for nm in ("stdout.txt", "stderr.txt"):
            f = role_dir / nm
            if f.exists():
                blob += "\n" + f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    low = blob.lower()
    transient_markers = (
        "529", "overloaded", "rate limit", "rate_limit", "too many requests",
        "503", "502", "504", "500 ", "temporarily", "try again",
        "timed out", "timeout", "connection reset", "connection refused",
        "service unavailable", "server error", "gateway",
    )
    # An HTTP error code like "error: 529" or status 5xx, or explicit overload text.
    if any(m in low for m in transient_markers):
        return "transient"
    if _re.search(r"\b5\d\d\b", low) and ("api error" in low or "status" in low or "http" in low):
        return "transient"
    return "malformed"


def invoke_cli_with_retry(
    *,
    profile,
    role,
    prompt_text,
    run_dir,
    expected_filename,
    context_manifest,
    validate=None,
    max_attempts=3,
    backoff_base_seconds=10,
    backoff_max_seconds=120,
):
    """Run a CLI backend with bounded validate-and-retry.

    Runs invoke_cli; if the backend failed (timeout/nonzero/missing/unparseable)
    OR the optional ``validate(output_obj) -> (ok: bool, errors: list[str])`` check
    fails, re-invoke with the failure appended to the prompt. Returns the first
    result whose backend succeeded AND validator passed; otherwise returns the last
    result (so the caller's existing fail-closed handling fires). Every attempt's
    artifacts are preserved under ``<role>/attempts/attempt_<n>/`` and an
    ``<role>/retry_history.json`` records the sequence. Mock path never uses this.
    """
    import json as _json
    from pathlib import Path as _Path
    role_dir = _Path(run_dir) / role
    attempts_dir = role_dir / "attempts"
    history = []
    last = None
    base_prompt = prompt_text
    for attempt in range(1, max_attempts + 1):
        result = invoke_cli(
            profile=profile,
            role=role,
            prompt_text=base_prompt if attempt == 1 else feedback_prompt,
            run_dir=run_dir,
            expected_filename=expected_filename,
            context_manifest=context_manifest,
        )
        last = result
        backend_reason = result.get("failure_reason")
        if backend_reason is None and result.get("output_obj") is None:
            backend_reason = "expected output not produced"
        val_ok, val_errors = True, []
        if backend_reason is None and validate is not None:
            try:
                val_ok, val_errors = validate(result.get("output_obj"))
            except Exception as exc:  # validator must never crash the run
                val_ok, val_errors = False, [f"validator raised: {exc}"]
        failure_class = _classify_backend_error(role_dir, backend_reason)
        ok = backend_reason is None and val_ok
        # snapshot this attempt's key artifacts
        try:
            adir = attempts_dir / f"attempt_{attempt}"
            adir.mkdir(parents=True, exist_ok=True)
            for nm in ("stdout.txt", "stderr.txt", "exit_code.txt"):
                src = role_dir / nm
                if src.exists():
                    (adir / nm).write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        history.append({
            "attempt": attempt,
            "backend_failure": backend_reason,
            "validation_ok": val_ok,
            "validation_errors": val_errors,
            "failure_class": failure_class,
            "passed": ok,
        })
        if ok:
            break
        if attempt >= max_attempts:
            break
        if failure_class == "transient":
            # Upstream was unavailable; the model never produced output. Do NOT feed
            # a correction back (there is nothing to correct). Back off, then re-send
            # the SAME prompt and hope the gateway has recovered.
            import time as _time
            delay = min(backoff_max_seconds, backoff_base_seconds * (2 ** (attempt - 1)))
            history[-1]["backoff_seconds"] = delay
            _time.sleep(delay)
            feedback_prompt = base_prompt
            continue
        # malformed output: feed the parse/validation errors back for correction.
        problems = []
        if backend_reason:
            problems.append(f"- backend/parse failure: {backend_reason}")
        for e in (val_errors or []):
            problems.append(f"- validation error: {e}")
        feedback_prompt = (
            base_prompt
            + "\n\n## Correction Required\n"
            + "Your previous output failed and was rejected. Fix EXACTLY these problems and\n"
            + "return ONLY the corrected YAML document (no commentary, no code fences):\n"
            + "\n".join(problems)
            + "\nRemember: every string value containing a ':' must be wrapped in double quotes,\n"
            + "and list items must be short descriptors, not full sentences."
        )
    try:
        (role_dir / "retry_history.json").write_text(_json.dumps(history, indent=2), encoding="utf-8")
    except Exception:
        pass
    if last is not None:
        last["retry_history"] = history
    return last


def invoke_cli(
    profile: dict,
    role: str,
    prompt_text: str,
    run_dir: Path,
    expected_filename: str,
    context_manifest: dict,
    *,
    env: dict | None = None,
    timeout: int | None = None,
) -> dict:
    """Invoke a CLI backend for a given role.

    Builds the command list from profile["command"], formatting placeholders.
    Runs via subprocess.run with capture_output. Writes the standard 7 prompt
    artifacts plus backend_invocation.json. Reads the expected output file
    that the CLI should have written.

    Controlled-failure contract: a timeout, nonzero exit, missing expected
    output, or unparseable expected output is reported as a ``failure_reason``
    with ``output_obj`` set to ``None`` rather than raising. ``invoke_cli``
    never raises because the expected output file is unparseable.

    Returns dict with output_path, output_obj, artifacts (list of relpaths),
    plus exit_code, timed_out, expected_output_exists, parse_error,
    process_failed, failure_reason.
    """
    run_dir = Path(run_dir)
    role_dir = ensure_dir(run_dir / role)

    run_dir_str = str(run_dir)
    run_dir_resolved = run_dir.resolve()
    project_root_str = str(project_root())

    # Base placeholders available to expected-output-file resolution and to
    # command templates.
    base_replacements: dict[str, str] = {
        "{run_dir}": run_dir_str,
        "{role}": role,
        "{project_root}": project_root_str,
        "{expected_filename}": expected_filename,
    }

    # Resolve the expected output file from the profile template (default
    # {run_dir}/{role}/{expected_filename}). It must resolve inside run_dir.
    expected_template = profile.get(
        "expected_output_file",
        "{run_dir}/{role}/{expected_filename}",
    )
    expected_output_text = _format_placeholders(expected_template, base_replacements)
    expected_output_abs = Path(expected_output_text)
    if not expected_output_abs.is_absolute():
        expected_output_abs = run_dir / expected_output_abs
    expected_output_abs = expected_output_abs.resolve()

    # Run-directory guard: reject an expected output file that resolves outside
    # run_dir before the subprocess is launched.
    try:
        expected_output_abs.relative_to(run_dir_resolved)
    except ValueError:
        raise ValueError(
            f"expected_output_file must resolve inside run_dir: {expected_output_abs}"
        )

    expected_output_rel = str(expected_output_abs.relative_to(run_dir_resolved))

    # Prompt delivery mode decides how the prompt text reaches the CLI.
    command_template = profile.get("command", [])
    env_snapshot = dict(env or os.environ)
    user_command = env_snapshot.get("USER_COMMAND", "")
    delivery = profile.get("prompt_delivery") or "instruction_arg"

    # Placeholders available to every profile command template.
    replacements: dict[str, str] = dict(base_replacements)
    replacements["{expected_output_file}"] = str(expected_output_abs)
    replacements["{user_command}"] = user_command

    prompt_file_abs: Path | None = None
    stdin_text: str | None = None

    if delivery == "stdin":
        # Full prompt goes to process stdin; the command arg stays short.
        stdin_text = prompt_text
        replacements["{instruction}"] = "<prompt delivered via stdin>"
        replacements["{prompt_file}"] = ""
    elif delivery == "prompt_file_path":
        # Write the prompt to a file and pass its path to the CLI.
        prompt_file_abs = role_dir / "prompt_body.txt"
        write_text(prompt_file_abs, prompt_text)
        replacements["{prompt_file}"] = str(prompt_file_abs)
        # {instruction} is aliased to the path so profiles that use
        # {instruction} as a prompt-file argument also work.
        replacements["{instruction}"] = str(prompt_file_abs)
    else:
        # instruction_arg (default): the full prompt text is a command arg.
        replacements["{instruction}"] = prompt_text
        replacements["{prompt_file}"] = ""

    # Build command from template, substituting every supported placeholder.
    formatted_cmd = [_format_placeholders(token, replacements) for token in command_template]

    # Resolve cwd.
    profile_cwd = profile.get("cwd", "{run_dir}").replace("{run_dir}", run_dir_str)

    # Resolve timeout.
    effective_timeout = timeout if timeout is not None else profile.get("timeout_seconds", 120)

    # Prompt hash.
    prompt_hash = sha256_bytes(prompt_text.encode("utf-8"))

    # Build subprocess env: use caller env or process env.
    subprocess_env = dict(os.environ)
    if env:
        subprocess_env.update(env)

    started_at_utc = now_utc_iso()

    # Run subprocess.
    try:
        result = subprocess.run(
            formatted_cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=profile_cwd,
            env=subprocess_env,
            input=stdin_text,
        )
        exit_code = result.returncode
        stdout_text = result.stdout
        stderr_text = result.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout_text = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr_text = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        timed_out = True

    completed_at_utc = now_utc_iso()

    # If the profile declares output_from: stdout, the CLI is a pure text
    # transformer: write captured stdout to the expected output file so the
    # connector (not the model) owns the file write. Mock path is unaffected.
    if profile.get("output_from") == "stdout" and not timed_out and exit_code == 0:
        try:
            expected_output_abs.parent.mkdir(parents=True, exist_ok=True)
            write_text(expected_output_abs, _strip_code_fences(stdout_text))
        except Exception:
            pass  # leave missing; the exists-check below records the failure

    expected_output_exists = expected_output_abs.exists()

    # Write 7 prompt artifacts.
    write_text(role_dir / "prompt.md", prompt_text)
    write_text(role_dir / "prompt_hash.txt", prompt_hash)
    write_json(role_dir / "context_manifest.json", context_manifest)
    write_text(role_dir / "stdout.txt", stdout_text)
    write_text(role_dir / "stderr.txt", stderr_text)
    write_text(role_dir / "exit_code.txt", str(exit_code))

    # Build env summary for backend_invocation: keys present, values redacted.
    redact_env = profile.get("redact_env", True)
    env_summary: dict[str, str] = {}
    for key in sorted(subprocess_env.keys()):
        if redact_env:
            env_summary[key] = "<redacted>"
        else:
            env_summary[key] = subprocess_env[key]

    # Determine controlled-failure state. The expected output file is parsed
    # only when the process did not time out and exited 0. A timeout, nonzero
    # exit, missing file, or unparseable file becomes a failure_reason with
    # output_obj=None instead of raising.
    process_failed = bool(timed_out or exit_code != 0)
    parse_error = None
    failure_reason = None
    output_obj = None

    if timed_out:
        failure_reason = "backend process timed out"
    elif exit_code != 0:
        failure_reason = f"backend process exited nonzero: {exit_code}"
    elif not expected_output_exists:
        failure_reason = "expected output file missing"
    else:
        try:
            if expected_output_abs.suffix in (".yaml", ".yml"):
                output_obj = load_yaml(expected_output_abs)
            elif expected_output_abs.suffix == ".json":
                output_obj = load_json(expected_output_abs)
            else:
                output_obj = expected_output_abs.read_text(encoding="utf-8")
        except Exception as exc:
            output_obj = None
            parse_error = str(exc)
            failure_reason = f"expected output file unparseable: {parse_error}"

    # Write backend_invocation.json.
    invocation = {
        "backend": profile.get("backend_type", "cli"),
        "profile_backend_type": profile.get("backend_type", "cli"),
        "prompt_delivery": delivery,
        "prompt_file": str(prompt_file_abs) if prompt_file_abs else None,
        "command_template": command_template,
        "command_resolved": formatted_cmd[:1] + ["<...>"] if len(formatted_cmd) > 1 else formatted_cmd,
        "cwd": profile_cwd,
        "timeout_seconds": effective_timeout,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "process_failed": process_failed,
        "failure_reason": failure_reason,
        "parse_error": parse_error,
        "stdout_len": len(stdout_text),
        "stderr_len": len(stderr_text),
        "expected_output_file": expected_output_rel,
        "expected_output_file_abs": str(expected_output_abs),
        "expected_output_exists": expected_output_exists,
        "prompt_hash": prompt_hash,
        "env_redacted": redact_env,
        "env_keys": list(env_summary.keys()),
    }
    write_json(role_dir / "backend_invocation.json", invocation)

    artifacts = [
        f"{role}/prompt.md",
        f"{role}/prompt_hash.txt",
        f"{role}/context_manifest.json",
        f"{role}/stdout.txt",
        f"{role}/stderr.txt",
        f"{role}/exit_code.txt",
        f"{role}/backend_invocation.json",
    ]
    if prompt_file_abs is not None:
        artifacts.append(f"{role}/prompt_body.txt")
    if expected_output_exists:
        artifacts.append(expected_output_rel)

    return {
        "output_path": str(expected_output_abs),
        "output_obj": output_obj,
        "artifacts": artifacts,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "expected_output_exists": expected_output_exists,
        "parse_error": parse_error,
        "process_failed": process_failed,
        "failure_reason": failure_reason,
    }
