"""Thin shell for rendering connector dashboard reports and serving a local web app.

Delegates all pipeline work to a subprocess.  Does NOT import the pipeline
module or make direct model calls.  Provides three modes:

  render_run <run_dir>   Render an existing run's artifacts.
  run <raw_input>         Run the full pipeline via subprocess, then render.
  serve [--host] [--port] Serve the local dashboard web app (stdlib HTTP).

``run`` passes the required ``--backend-config`` / ``--role-config`` arguments
to the pipeline, defaulting to the shipped example configs so the documented
smoke command works out of the box.  If the pipeline subprocess fails, the
shell fails loudly (nonzero exit, clear summary) and never pretends a report
was rendered.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# This file lives at <root>/dashboard/connector_dashboard.py.
# parents[0] = dashboard/  ;  parents[1] = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from connector.tools.render_dashboard_report import write_report  # noqa: E402

_DEFAULT_BACKEND_CONFIG = _REPO_ROOT / "connector" / "configs" / "backend_profiles.example.yaml"
_DEFAULT_ROLE_CONFIG = _REPO_ROOT / "connector" / "configs" / "roles.example.yaml"

# ---------------------------------------------------------------------------
# Runs root -- defaults to connector/runs/ but can be overridden for tests.
# ---------------------------------------------------------------------------
_RUNS_ROOT = _REPO_ROOT / "connector" / "runs"


def _set_runs_root(path: str | Path) -> None:
    """Override the runs root directory (for tests only)."""
    global _RUNS_ROOT
    _RUNS_ROOT = Path(path)


def _invoke_pipeline(
    raw_input_path: str,
    backend: str,
    run_dir: str | None,
    backend_config: str,
    role_config: str,
    builder_backend: str | None = None,
    evaluator_backend: str | None = None,
) -> tuple[int, str, str, str]:
    """Invoke the connector pipeline via subprocess.

    Returns ``(returncode, run_dir, stdout, stderr)``.  Does NOT print or
    exit.  Creates a temp run directory under ``_RUNS_ROOT`` when *run_dir*
    is None.
    """
    actual_run_dir = run_dir or tempfile.mkdtemp(
        prefix="connector_run_",
        dir=str(_RUNS_ROOT),
    )

    cmd = [
        sys.executable,
        "-m",
        "connector.tools.run_connector_pipeline",
        "--raw-input", raw_input_path,
        "--backend-config", backend_config,
        "--role-config", role_config,
        "--backend", backend,
        "--out", actual_run_dir,
    ]
    if builder_backend:
        cmd.extend(["--builder-backend", builder_backend])
    if evaluator_backend:
        cmd.extend(["--evaluator-backend", evaluator_backend])
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return result.returncode, actual_run_dir, result.stdout, result.stderr


def _run_pipeline(
    raw_input_path: str,
    backend: str,
    run_dir: str | None,
    backend_config: str,
    role_config: str,
    builder_backend: str | None = None,
    evaluator_backend: str | None = None,
) -> str:
    """Invoke the connector pipeline via subprocess and return the run dir.

    Exits the process nonzero with a clear summary if the pipeline subprocess
    fails.  Never renders a report on failure.
    """
    import tempfile

    actual_run_dir = run_dir or tempfile.mkdtemp(
        prefix="connector_run_",
        dir=str(_RUNS_ROOT),
    )

    cmd = [
        sys.executable,
        "-m",
        "connector.tools.run_connector_pipeline",
        "--raw-input", raw_input_path,
        "--backend-config", backend_config,
        "--role-config", role_config,
        "--backend", backend,
        "--out", actual_run_dir,
    ]
    if builder_backend:
        cmd.extend(["--builder-backend", builder_backend])
    if evaluator_backend:
        cmd.extend(["--evaluator-backend", evaluator_backend])
    print(f"Running pipeline: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        # Fail loudly: surface stderr, do NOT pretend a report was generated.
        print("Connector pipeline FAILED.", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        print(f"Pipeline exited with code {result.returncode}.", file=sys.stderr)
        print("No report was rendered.", file=sys.stderr)
        sys.exit(result.returncode)
    return actual_run_dir


def _require_run_artifacts(run_dir: str) -> None:
    """Refuse to render a run dir that lacks the minimal report artifacts."""
    run_path = Path(run_dir)
    if not run_path.is_dir():
        print(f"Error: run directory does not exist: {run_path}", file=sys.stderr)
        sys.exit(2)
    manifest = run_path / "run_manifest.json"
    if not manifest.exists():
        print(
            f"Error: cannot render report -- required artifact missing: "
            f"{manifest}",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Dashboard HTTP server (stdlib only)
# ---------------------------------------------------------------------------

_INDEX_PATH = _REPO_ROOT / "dashboard" / "static" / "index.html"
_CSS_PATH = _REPO_ROOT / "dashboard" / "static" / "app.css"
_JS_PATH = _REPO_ROOT / "dashboard" / "static" / "app.js"
_GOLDEN_INPUTS_DIR = _REPO_ROOT / "connector" / "examples" / "golden_inputs"

_INDEX_LOCK = threading.Lock()


def _load_backend_config(config_path: Path) -> dict:
    """Load backend profiles YAML and return profiles dict."""
    from connector.tools.io_utils import load_yaml
    return load_yaml(config_path)


def _role_pins(role_config_path: Path) -> dict:
    """Return the backend profile pinned for each pipeline role, or None if unpinned.

    Only profile names are returned, never paths or prompts. Any read error
    yields no pins (the page then falls back to its generic wording).
    """
    pins = {"builder": None, "evaluator": None}
    try:
        roles = (_load_backend_config(role_config_path) or {}).get("roles", {}) or {}
    except Exception:
        return pins
    for key, role in (("builder", "observation_packet_builder"),
                      ("evaluator", "market_book_evaluator")):
        value = (roles.get(role) or {}).get("backend_profile")
        pins[key] = value if isinstance(value, str) and value else None
    return pins


def _scan_examples() -> list[dict]:
    """Scan golden_inputs dir for .md files and return example metadata."""
    examples = []
    if not _GOLDEN_INPUTS_DIR.is_dir():
        return examples
    for f in sorted(_GOLDEN_INPUTS_DIR.glob("*.md")):
        try:
            body = f.read_text(encoding="utf-8")
            examples.append({
                "name": f.stem,
                "path": str(f),
                "preview": body[:200],
                "body": body,
            })
        except Exception:
            continue
    return examples


def _read_run_index() -> dict:
    """Read the run index file, returning empty dict if missing."""
    idx_path = _RUNS_ROOT / "_dashboard" / "run_index.json"
    try:
        return json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return {"runs": []}


def _write_run_index(idx: dict) -> None:
    """Write the run index atomically under a lock."""
    dash_dir = _RUNS_ROOT / "_dashboard"
    dash_dir.mkdir(parents=True, exist_ok=True)
    idx_path = dash_dir / "run_index.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(dash_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(idx_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _record_run(run_id: str, run_dir: str, status: str, backend: str) -> None:
    """Append a run entry to the index file."""
    from connector.tools.io_utils import now_utc_iso
    with _INDEX_LOCK:
        idx = _read_run_index()
        idx["runs"].append({
            "run_id": run_id,
            "run_dir": run_dir,
            "created_at_utc": now_utc_iso(),
            "status": status,
            "backend": backend,
        })
        _write_run_index(idx)


def _make_upload(text: str) -> str:
    """Write pasted input text to the uploads dir and return the path."""
    uploads_dir = _RUNS_ROOT / "_dashboard" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + ".md"
    p = uploads_dir / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _is_safe_run_id(run_id: str) -> bool:
    """Reject run ids that could escape the runs root or collide with internals."""
    return (
        bool(run_id)
        and not run_id.startswith("_")
        and "/" not in run_id
        and "\\" not in run_id
        and ".." not in run_id
    )


def _resolve_run_dir(run_id: str) -> str | None:
    """Resolve a run_id to its run directory path, or None if not found."""
    if not _is_safe_run_id(run_id):
        return None
    idx = _read_run_index()
    for entry in idx.get("runs", []):
        if entry.get("run_id") == run_id:
            return entry.get("run_dir")
    candidate = _RUNS_ROOT / run_id
    if candidate.is_dir() and (candidate / "run_manifest.json").exists():
        return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Catalog resolver (lazy singleton) and run view-model assembly
# ---------------------------------------------------------------------------

_BANNER = "Research context for analyst review — not a trading instruction."

_CATALOG_PATH = (
    _REPO_ROOT / "connector" / "catalog" / "market_book_catalog.stage6_enriched.json"
)
_CURATED_PATH = _REPO_ROOT / "dashboard" / "curated_runs.json"

_CATALOG_LOCK = threading.Lock()
_CATALOG: dict | None = None

# In-memory job table for asynchronous live runs (POST /api/runs).
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()

# The two verdict-class literals must never be serialized to the client.
_LEAK_LITERALS = ("no_match_not_in_book", "no_match_out_of_scope")

_VALIDATION_FILES = {
    "observation_packet": "validation/observation_packet_validation.json",
    "market_book_evaluation": "validation/market_book_evaluation_validation.json",
    "signal_context": "signal_context/validation.json",
}

# Pipeline steps whose stage directory holds a stderr.txt worth surfacing.
_STEP_STDERR_DIRS = {
    "observation_packet_builder": "builder",
    "market_book_evaluator": "evaluator",
}


def _load_catalog() -> dict:
    """Load the enriched catalog once; index entries and linked records by id."""
    global _CATALOG
    with _CATALOG_LOCK:
        if _CATALOG is None:
            by_entry: dict = {}
            by_record: dict = {}
            try:
                data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for entry in data.get("entries", []):
                entry_id = entry.get("entry_id")
                state_id = entry.get("state_id")
                if entry_id:
                    by_entry[entry_id] = entry
                if state_id:
                    by_entry[state_id] = entry
                for rec in entry.get("linked_records", []):
                    rid = rec.get("id")
                    if rid:
                        by_record[rid] = {
                            "kind": rec.get("kind"),
                            "text": rec.get("text"),
                            "entry_id": entry_id,
                            "state_id": state_id,
                        }
            _CATALOG = {"by_entry": by_entry, "by_record": by_record}
    return _CATALOG


def _resolve_state(any_id: str) -> dict | None:
    """Resolve an entry_* or state_* id to title/summary, or None."""
    entry = _load_catalog()["by_entry"].get(any_id)
    if entry is None:
        return None
    full = entry.get("state_summary") or ""
    head, sep, rest = full.partition(". ")
    if sep:
        title, summary = head, rest
    else:
        title, summary = full[:80], full
    return {
        "title": title or None,
        "summary": summary or None,
        "entry_id": entry.get("entry_id"),
        "state_id": entry.get("state_id"),
    }


def _resolve_record(record_id: str) -> dict | None:
    """Resolve a linked-record id to its catalog text, or None."""
    rec = _load_catalog()["by_record"].get(record_id)
    if rec is not None:
        return rec
    state = _resolve_state(record_id)
    if state is None:
        return None
    return {
        "kind": "state",
        "text": state["summary"],
        "entry_id": state["entry_id"],
        "state_id": state["state_id"],
    }


def _snippet(text: str | None, limit: int = 160) -> str | None:
    """First ~limit chars of text, cut at a word boundary, ellipsis appended."""
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _read_json_artifact(path: Path) -> dict | None:
    """Read a JSON artifact; None when missing or unreadable (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_yaml_artifact(path: Path) -> tuple[dict | None, bool]:
    """Read a YAML artifact. Returns (data, ok); ok=False means unparseable."""
    if not path.is_file():
        return None, True
    try:
        from connector.tools.io_utils import load_yaml
        data = load_yaml(path)
    except Exception:
        return None, False
    return (data if isinstance(data, dict) else None), True


def _load_curated_registry() -> list[dict]:
    """Read dashboard/curated_runs.json; empty list when missing/invalid."""
    data = _read_json_artifact(_CURATED_PATH) or {}
    entries = data.get("curated_runs", [])
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _evaluation_has_verdict(run_dir: str) -> bool:
    """True when the run's evaluation artifact parses and carries a verdict."""
    eval_path = Path(run_dir) / "evaluator" / "market_book_evaluation.yaml"
    data, ok = _read_yaml_artifact(eval_path)
    return ok and isinstance(data, dict) and "verdict" in data


def _scrub_view(node):
    """Strip the raw verdict and verdict-class literals from the view-model.

    Rule 2 leak guard: the ``verdict`` key and the two no_match_* literals
    must never reach the client, regardless of artifact content.
    """
    if isinstance(node, dict):
        return {k: _scrub_view(v) for k, v in node.items() if k != "verdict"}
    if isinstance(node, list):
        return [_scrub_view(v) for v in node]
    if isinstance(node, str):
        for lit in _LEAK_LITERALS:
            if lit in node:
                node = node.replace(lit, "[redacted]")
        return node
    return node


def _step_stderr_tail(run_dir: Path, failed_step: str | None, limit: int = 4000) -> str | None:
    """Tail of the failed LLM step's stderr.txt, when available."""
    step_dir = _STEP_STDERR_DIRS.get(failed_step or "")
    if not step_dir:
        return None
    try:
        text = (run_dir / step_dir / "stderr.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except Exception:
        return None
    text = text.strip()
    return text[-limit:] if text else None


def build_run_view(run_dir, job: dict | None = None) -> dict | None:
    """Assemble the client view-model for one run directory.

    Returns None when the run has no manifest and is not a known running job
    (caller answers 404). Every artifact load degrades to an honest absence;
    this function never raises on corrupt input.
    """
    run_path = Path(run_dir)
    run_id = run_path.name
    base = {"run_id": run_id, "banner": _BANNER}

    manifest_path = run_path / "run_manifest.json"
    if not manifest_path.is_file():
        if job and job.get("status") == "running":
            return {**base, "outcome": "running",
                    "meta": {"backend": job.get("backend")}}
        if job and job.get("status") == "failed":
            # The pipeline died before writing its manifest: fail-closed
            # screen with the captured stderr tail, never a 404 void.
            return _scrub_view({
                **base,
                "outcome": "failed",
                "meta": {"backend": job.get("backend")},
                "failure": {
                    "failed_step": None,
                    "reason": "pipeline did not produce a run manifest",
                    "stderr_tail": job.get("error"),
                    "validator_errors": [],
                },
            })
        return None
    manifest = _read_json_artifact(manifest_path)
    if manifest is None:
        return _scrub_view({
            **base,
            "outcome": "failed",
            "failure": {
                "failed_step": None,
                "reason": "run manifest unreadable",
                "stderr_tail": None,
                "validator_errors": [],
            },
        })

    meta = {
        "backend": manifest.get("backend"),
        "backend_profiles": manifest.get("backend_profiles"),
        "created_at_utc": manifest.get("created_at_utc"),
        "raw_input_hash": manifest.get("raw_input_hash"),
        "market_book_version": manifest.get("market_book_version"),
        "evaluation_version": None,
    }

    if job and job.get("status") == "running":
        return {**base, "outcome": "running", "meta": meta}

    # -- shared artifact loads (each degrades independently) --

    packet, _packet_ok = _read_yaml_artifact(
        run_path / "builder" / "market_observation_packet.yaml"
    )
    try:
        raw_text = (run_path / "raw_input.md").read_text(encoding="utf-8")
    except Exception:
        raw_text = None
    observations = []
    if packet:
        for i, obs in enumerate(packet.get("observations", []) or []):
            observations.append({
                "index": i,
                "dimension": obs.get("dimension"),
                "direction": obs.get("direction"),
                "magnitude": obs.get("magnitude"),
                "statement": obs.get("statement"),
            })
    view_input = {
        "raw_text": raw_text,
        "summary": packet.get("summary") if packet else None,
        "asset_scope": packet.get("asset_scope") if packet else None,
        "as_of": packet.get("as_of") if packet else None,
    }

    validation = {}
    for name, rel in _VALIDATION_FILES.items():
        data = _read_json_artifact(run_path / rel)
        validation[name] = (
            None if data is None
            else {"valid": data.get("valid"), "errors": data.get("errors", [])}
        )

    near_miss = None
    trace = _read_json_artifact(run_path / "retrieval" / "retrieval_trace.json")
    if trace is not None:
        near_miss = []
        for item in trace.get("near_miss_band", []) or []:
            state = _resolve_state(item.get("entry_id") or "")
            top = item.get("top_reason") or {}
            src = top.get("source_record_id")
            rec = _resolve_record(src) if src else None
            near_miss.append({
                "entry_id": item.get("entry_id"),
                "state_id": item.get("state_id"),
                "title": state["title"] if state else None,
                "retrieval_score": item.get("retrieval_score"),
                "closest_on": {
                    "record_id": src,
                    "text_snippet": _snippet(rec["text"]) if rec else None,
                },
            })

    candidates_by_entry = {}
    cand = _read_json_artifact(run_path / "retrieval" / "candidate_entries.json")
    if cand:
        for c in cand.get("candidates", []) or []:
            candidates_by_entry[c.get("entry_id")] = c

    def _surfaced_by(entry_id):
        c = candidates_by_entry.get(entry_id)
        if not c:
            return []
        out = []
        for r in (c.get("retrieval_reasons") or [])[:2]:
            rid = r.get("source_record_id")
            rec = _resolve_record(rid) if rid else None
            out.append({
                "record_id": rid,
                "text_snippet": _snippet(rec["text"]) if rec else None,
            })
        return out

    context_raw = _read_json_artifact(
        run_path / "signal_context" / "market_state_context.json"
    )
    context = None
    if context_raw is not None:
        context = {k: context_raw.get(k) for k in (
            "policy_posture", "risk_posture", "caveats", "constraints",
            "missing_evidence", "failure_modes", "required_validations",
            "not_trading_instruction",
        )}

    source_artifacts = context_raw.get("source_artifacts") if context_raw else None
    if not source_artifacts:
        source_artifacts = {}
        for logical, rel in (
            ("run_manifest", "run_manifest.json"),
            ("observation_packet", "builder/market_observation_packet.yaml"),
            ("candidate_entries", "retrieval/candidate_entries.json"),
            ("retrieval_trace", "retrieval/retrieval_trace.json"),
            ("market_book_evaluation", "evaluator/market_book_evaluation.yaml"),
            ("observation_packet_validation",
             "validation/observation_packet_validation.json"),
            ("market_book_evaluation_validation",
             "validation/market_book_evaluation_validation.json"),
        ):
            if (run_path / rel).is_file():
                source_artifacts[logical] = rel
    provenance = {
        "source_artifacts": source_artifacts,
        "downloads": {
            "context_json": f"/api/runs/{run_id}/signal_context/download/json",
            "context_yaml": f"/api/runs/{run_id}/signal_context/download/yaml",
        },
    }

    steps = [
        {"name": s.get("name"), "status": s.get("status"), "valid": s.get("valid")}
        for s in manifest.get("steps", []) or []
    ]

    def _failure_view(reason, failed_step=None, stderr_tail=None, validator_errors=None):
        return _scrub_view({
            **base,
            "outcome": "failed",
            "meta": meta,
            "input": view_input,
            "observations": observations,
            "near_miss_band": near_miss,
            "validation": validation,
            "failure": {
                "failed_step": failed_step,
                "reason": reason,
                "stderr_tail": stderr_tail,
                "validator_errors": validator_errors or [],
            },
            "steps": steps,
            "provenance": provenance,
        })

    # -- outcome taxonomy --

    evaluation, eval_ok = _read_yaml_artifact(
        run_path / "evaluator" / "market_book_evaluation.yaml"
    )
    overall_status = manifest.get("overall_status")

    if overall_status != "ok":
        failed_step = manifest.get("failed_step")
        if (not failed_step and isinstance(overall_status, str)
                and overall_status.startswith("failed_")):
            failed_step = overall_status[len("failed_"):]
        validator_errors = []
        for gate in validation.values():
            if gate and gate.get("errors"):
                validator_errors.extend(gate["errors"])
        stderr_tail = (job or {}).get("error") or _step_stderr_tail(run_path, failed_step)
        reason = manifest.get("reason") or f"pipeline did not complete ({overall_status})"
        return _failure_view(reason, failed_step, stderr_tail, validator_errors)

    if not eval_ok:
        return _failure_view("evaluation artifact unreadable")
    if evaluation is None:
        return _failure_view("evaluation artifact missing")

    if "verdict" not in evaluation:
        return _scrub_view({
            **base,
            "outcome": "legacy",
            "meta": meta,
            "steps": steps,
        })

    meta["evaluation_version"] = evaluation.get("evaluation_version")
    is_match = evaluation.get("verdict") == "matched"

    matched_states = []
    for ms in evaluation.get("matched_states", []) or []:
        state = _resolve_state(ms.get("entry_id") or ms.get("state_id") or "")
        matched_obs = []
        for idx in ms.get("matched_observations", []) or []:
            item = {"index": idx}
            if isinstance(idx, int) and 0 <= idx < len(observations):
                o = observations[idx]
                item.update({
                    "dimension": o["dimension"],
                    "direction": o["direction"],
                    "statement": o["statement"],
                })
            matched_obs.append(item)
        citations = []
        for cit in ms.get("citations", []) or []:
            rid = cit.get("record_id")
            rec = _resolve_record(rid) if rid else None
            citations.append({
                "kind": cit.get("kind"),
                "record_id": rid,
                "text": rec["text"] if rec else None,
            })
        matched_states.append({
            "entry_id": ms.get("entry_id"),
            "state_id": ms.get("state_id"),
            "title": state["title"] if state else None,
            "state_summary": state["summary"] if state else None,
            "match_strength": ms.get("match_strength"),
            "matched_observations": matched_obs,
            "citations": citations,
            "policy_posture": ms.get("policy_posture"),
            "failure_modes": ms.get("failure_modes", []),
            "evidence_caveats": ms.get("evidence_caveats"),
        })

    non_matches = []
    for nm in evaluation.get("non_matches", []) or []:
        state = _resolve_state(nm.get("entry_id") or "")
        non_matches.append({
            "entry_id": nm.get("entry_id"),
            "state_id": nm.get("state_id"),
            "title": state["title"] if state else None,
            "reason_code": nm.get("reason_code"),
            "reason": nm.get("reason"),
            "surfaced_by": _surfaced_by(nm.get("entry_id")),
        })

    return _scrub_view({
        **base,
        "outcome": "matched" if is_match else "no_match",
        "meta": meta,
        "input": view_input,
        "observations": observations,
        "interpretation": evaluation.get("overall_interpretation"),
        "no_match_rationale": (
            None if is_match else evaluation.get("no_match_rationale")
        ),
        "matched_states": matched_states,
        "non_matches": non_matches,
        "near_miss_band": near_miss,
        "context": context,
        "validation": validation,
        "steps": steps,
        "provenance": provenance,
    })


def _execute_job(
    run_id: str,
    run_dir: str,
    raw_input_path: str,
    backend: str,
    backend_config: str,
    role_config: str,
    builder_backend: str | None = None,
    evaluator_backend: str | None = None,
) -> None:
    """Background worker for POST /api/runs: run the pipeline, record the result."""
    from connector.tools.io_utils import now_utc_iso
    try:
        returncode, _actual, stdout, stderr = _invoke_pipeline(
            raw_input_path=raw_input_path,
            backend=backend,
            run_dir=run_dir,
            backend_config=backend_config,
            role_config=role_config,
            builder_backend=builder_backend,
            evaluator_backend=evaluator_backend,
        )
        error = (stderr or stdout or "pipeline failed")[-4000:] if returncode != 0 else None
    except Exception as exc:  # invocation itself blew up
        returncode, error = 1, f"pipeline invocation error: {exc}"
    status = "ok" if returncode == 0 else "failed"
    with _JOBS_LOCK:
        job = _JOBS.get(run_id) or {}
        job.update({"status": status, "finished_at": now_utc_iso()})
        if error:
            job["error"] = error
        _JOBS[run_id] = job
    _record_run(run_id, run_dir, status, backend)


class _DashboardHandler(BaseHTTPRequestHandler):
    """Request handler for the connector dashboard."""

    # Silence per-request log lines in test output
    def log_message(self, format, *args):
        pass

    def _local_host_ok(self) -> bool:
        # Reject DNS-rebinding requests: the Host header must name this machine.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def do_GET(self):  # noqa: N802
        if not self._local_host_ok():
            self._send_json({"error": "forbidden host"}, status=403)
            return
        if self.path == "/":
            self._serve_file(_INDEX_PATH, "text/html")
        elif self.path == "/static/app.css":
            self._serve_file(_CSS_PATH, "text/css")
        elif self.path == "/static/app.js":
            self._serve_file(_JS_PATH, "text/javascript")
        elif self.path == "/api/backend-profiles":
            self._handle_backend_profiles()
        elif self.path == "/api/examples":
            self._handle_examples()
        elif self.path == "/api/curated-runs":
            self._handle_curated_runs()
        elif self.path == "/api/runs":
            self._handle_list_runs()
        elif self.path.startswith("/api/runs/"):
            rest = self.path[len("/api/runs/"):]
            run_id, slash, sub = rest.partition("/")
            if not slash:
                self._handle_get_run(run_id)
            elif sub.startswith("signal_context"):
                self._handle_get_signal_context(rest)
            elif sub == "status":
                self._handle_get_run_status(run_id)
            elif sub == "view":
                self._handle_get_run_view(run_id)
            else:
                self._serve_404()
        else:
            self._serve_404()

    def do_POST(self):  # noqa: N802
        # application/json forces a CORS preflight, so other web pages cannot post here.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not self._local_host_ok() or ctype != "application/json":
            self._send_json({"error": "forbidden"}, status=403)
            return
        if self.path == "/api/runs":
            self._handle_post_runs()
        else:
            self._serve_404()

    # -- static file serving with traversal guard --

    def _serve_file(self, fpath: Path, content_type: str) -> None:
        if not fpath.is_file():
            self._serve_404()
            return
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_404(self) -> None:
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        # default=str: run YAML may carry datetime.date values (unquoted dates
        # emitted by a backend); serialize them instead of dropping the connection.
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- API handlers --

    def _handle_backend_profiles(self) -> None:
        try:
            cfg = _load_backend_config(self.server.backend_config)  # type: ignore[attr-defined]
            profiles = cfg.get("profiles", {})
            # Strip command argv: the UI only needs type/timeout for labeling.
            self._send_json({
                "profiles_version": cfg.get("profiles_version", ""),
                "default": "mock",
                "profiles": {
                    name: {
                        "backend_type": p.get("backend_type", ""),
                        "timeout_seconds": p.get("timeout_seconds"),
                    }
                    for name, p in profiles.items()
                },
                # Profile names pinned per role in the roles file the server was
                # started with, so the page can tell what "Use my roles file" means.
                "role_pins": _role_pins(self.server.role_config),  # type: ignore[attr-defined]
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_examples(self) -> None:
        self._send_json({"examples": _scan_examples()})

    def _handle_curated_runs(self) -> None:
        """List curated runs that resolve to a current-dialect run dir."""
        out = []
        for entry in _load_curated_registry():
            run_id = entry.get("id", "")
            if not _is_safe_run_id(run_id):
                continue
            run_dir = _resolve_run_dir(run_id)
            if run_dir is None:
                continue
            # Legacy guard: never list a run the view cannot render.
            if not _evaluation_has_verdict(run_dir):
                continue
            out.append({
                "id": run_id,
                "label": entry.get("label", run_id),
                "expect": entry.get("expect"),
            })
        self._send_json({"curated_runs": out})

    def _handle_list_runs(self) -> None:
        """Recent runs from the run index, newest first (no absolute paths)."""
        idx = _read_run_index()
        runs = [{
            "run_id": entry.get("run_id"),
            "status": entry.get("status"),
            "backend": entry.get("backend"),
            "created_at_utc": entry.get("created_at_utc"),
        } for entry in reversed(idx.get("runs", []))]
        self._send_json({"runs": runs})

    def _handle_get_run_status(self, run_id: str) -> None:
        if not _is_safe_run_id(run_id):
            self._serve_404()
            return
        with _JOBS_LOCK:
            job = _JOBS.get(run_id)
        if job is not None:
            body: dict = {"run_id": run_id, "status": job.get("status")}
            if job.get("status") == "failed" and job.get("error"):
                body["error"] = job["error"]
            self._send_json(body)
            return
        idx = _read_run_index()
        for entry in idx.get("runs", []):
            if entry.get("run_id") == run_id:
                self._send_json({"run_id": run_id, "status": entry.get("status")})
                return
        run_dir = _RUNS_ROOT / run_id
        manifest = (
            _read_json_artifact(run_dir / "run_manifest.json")
            if run_dir.is_dir() else None
        )
        if manifest is None:
            self._serve_404()
            return
        overall = manifest.get("overall_status")
        self._send_json({
            "run_id": run_id,
            "status": "ok" if overall == "ok" else "failed",
        })

    def _handle_get_run_view(self, run_id: str) -> None:
        if not _is_safe_run_id(run_id):
            self._serve_404()
            return
        run_dir = _resolve_run_dir(run_id)
        with _JOBS_LOCK:
            job = _JOBS.get(run_id)
        if run_dir is None and job and job.get("status") == "running":
            # The run dir exists (created up front) but has no manifest yet.
            candidate = _RUNS_ROOT / run_id
            if candidate.is_dir():
                run_dir = str(candidate)
        if run_dir is None:
            self._serve_404()
            return
        view = build_run_view(run_dir, job=job)
        if view is None:
            self._serve_404()
            return
        self._send_json(view)

    def _handle_post_runs(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw) if raw else {}
        except Exception:
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        backend = payload.get("backend", "")
        raw_input_path = ""  # HTTP clients may not name server-side files; send raw_input_text
        raw_input_text = payload.get("raw_input_text", "")
        builder_backend = payload.get("builder_backend") or None
        evaluator_backend = payload.get("evaluator_backend") or None

        # Validate backend exists
        try:
            cfg = _load_backend_config(self.server.backend_config)  # type: ignore[attr-defined]
            profiles = cfg.get("profiles", {})
        except Exception as exc:
            self._send_json({"error": f"cannot load backend config: {exc}"}, status=400)
            return

        if not backend or backend not in profiles:
            self._send_json({"error": f"invalid or missing backend: {backend!r}"}, status=400)
            return

        # Resolve the input file path
        if not raw_input_path and not raw_input_text:
            self._send_json({"error": "raw_input_path or raw_input_text required"}, status=400)
            return

        if raw_input_text and not raw_input_path:
            raw_input_path = _make_upload(raw_input_text)

        if not os.path.isfile(raw_input_path):
            self._send_json({"error": f"raw_input_path does not exist: {raw_input_path}"}, status=400)
            return

        # Create the run dir up front so the run_id is known immediately,
        # then launch the pipeline on a background thread (live LLM runs can
        # take minutes; the HTTP request must not stay open).
        run_dir = tempfile.mkdtemp(prefix="connector_run_", dir=str(_RUNS_ROOT))
        run_id = Path(run_dir).name
        # Reject run_ids starting with _ defensively
        if run_id.startswith("_"):
            self._send_json({"error": "internal: run_id reserved"}, status=500)
            return

        from connector.tools.io_utils import now_utc_iso
        with _JOBS_LOCK:
            _JOBS[run_id] = {
                "status": "running",
                "backend": backend,
                "started_at": now_utc_iso(),
            }
        worker = threading.Thread(
            target=_execute_job,
            args=(
                run_id,
                run_dir,
                raw_input_path,
                backend,
                str(self.server.backend_config),  # type: ignore[attr-defined]
                str(self.server.role_config),  # type: ignore[attr-defined]
                builder_backend,
                evaluator_backend,
            ),
            daemon=True,
        )
        worker.start()
        self._send_json({"run_id": run_id, "status": "running"}, status=202)

    def _handle_get_run(self, run_id: str) -> None:
        """Serve run details by run_id."""
        if not _is_safe_run_id(run_id):
            self._serve_404()
            return
        run_dir = _resolve_run_dir(run_id)

        if run_dir is None:
            self._serve_404()
            return

        run_path = Path(run_dir)
        result: dict = {"run_id": run_id, "run_dir": str(run_dir)}

        # Attach manifest
        manifest_path = run_path / "run_manifest.json"
        if manifest_path.exists():
            try:
                result["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                result["manifest"] = None

        # Attach report JSON if present
        report_path = run_path / "dashboard" / "dashboard_report.json"
        if report_path.exists():
            try:
                result["report"] = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                result["report"] = None

        # Attach signal context if present
        sc_path = run_path / "signal_context" / "market_state_context.json"
        if sc_path.exists():
            try:
                result["signal_context"] = json.loads(sc_path.read_text(encoding="utf-8"))
            except Exception:
                result["signal_context"] = None

        sc_val_path = run_path / "signal_context" / "validation.json"
        if sc_val_path.exists():
            try:
                result["signal_context_validation"] = json.loads(sc_val_path.read_text(encoding="utf-8"))
            except Exception:
                result["signal_context_validation"] = None

        self._send_json(result)

    def _handle_get_signal_context(self, rest: str) -> None:
        """Serve signal context files for a run.

        rest is everything after "/api/runs/", e.g. "run_id/signal_context/download/json"
        """
        parts = rest.split("/")
        # Expected patterns:
        #   run_id/signal_context
        #   run_id/signal_context/download/json
        #   run_id/signal_context/download/yaml
        if len(parts) < 2:
            self._serve_404()
            return
        run_id = parts[0]
        if not _is_safe_run_id(run_id):
            self._serve_404()
            return
        run_dir = _resolve_run_dir(run_id)
        if run_dir is None:
            self._serve_404()
            return

        run_path = Path(run_dir)
        sc_dir = run_path / "signal_context"

        if len(parts) == 2:
            # /api/runs/<run_id>/signal_context
            fpath = sc_dir / "market_state_context.json"
            if not fpath.is_file():
                self._serve_404()
                return
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif len(parts) == 4 and parts[1] == "signal_context" and parts[2] == "download":
            fmt = parts[3]
            if fmt == "json":
                fpath = sc_dir / "market_state_context.json"
                if not fpath.is_file():
                    self._serve_404()
                    return
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{run_id}_market_state_context.json"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif fmt == "yaml":
                fpath = sc_dir / "market_state_context.yaml"
                if not fpath.is_file():
                    self._serve_404()
                    return
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{run_id}_market_state_context.yaml"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._serve_404()
        else:
            self._serve_404()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Market Book connector dashboard shell."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # render_run
    p_render = sub.add_parser("render_run", help="Render an existing run directory.")
    p_render.add_argument("run_dir", help="Path to the connector run directory.")

    # run
    p_run = sub.add_parser("run", help="Run pipeline and render.")
    p_run.add_argument("raw_input", help="Path to raw market observation input.")
    p_run.add_argument("--backend", default="mock", help="Backend profile (default: mock).")
    p_run.add_argument("--out", default=None, help="Run output directory (default: auto).")
    p_run.add_argument(
        "--backend-config",
        default=str(_DEFAULT_BACKEND_CONFIG),
        help=f"Backend profiles YAML (default: {_DEFAULT_BACKEND_CONFIG.name} under connector/configs).",
    )
    p_run.add_argument(
        "--role-config",
        default=str(_DEFAULT_ROLE_CONFIG),
        help=f"Roles config YAML (default: {_DEFAULT_ROLE_CONFIG.name} under connector/configs).",
    )
    p_run.add_argument(
        "--builder-backend",
        default=None,
        help="Override backend profile for the observation_packet_builder role.",
    )
    p_run.add_argument(
        "--evaluator-backend",
        default=None,
        help="Override backend profile for the market_book_evaluator role.",
    )

    # serve
    p_serve = sub.add_parser("serve", help="Serve the local dashboard web app.")
    p_serve.add_argument("run_dir", nargs="?", default=None,
                         help="Optional existing run dir to inspect (default: none).")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--backend-config", default=str(_DEFAULT_BACKEND_CONFIG))
    p_serve.add_argument("--role-config", default=str(_DEFAULT_ROLE_CONFIG))

    args = parser.parse_args()

    if args.mode == "render_run":
        _require_run_artifacts(args.run_dir)
        md_path, json_path = write_report(Path(args.run_dir))
        print(f"Report written: {md_path}")
        print(f"JSON report:   {json_path}")

    elif args.mode == "run":
        run_dir = _run_pipeline(
            args.raw_input,
            args.backend,
            args.out,
            args.backend_config,
            args.role_config,
            builder_backend=args.builder_backend,
            evaluator_backend=args.evaluator_backend,
        )
        md_path, json_path = write_report(Path(run_dir))
        print(f"Report written: {md_path}")
        print(f"JSON report:   {json_path}")

    elif args.mode == "serve":
        httpd = ThreadingHTTPServer(
            (args.host, args.port),
            _DashboardHandler,
        )
        httpd.backend_config = Path(args.backend_config)  # type: ignore[attr-defined]
        httpd.role_config = Path(args.role_config)  # type: ignore[attr-defined]
        print(f"Serving dashboard at http://{args.host}:{args.port}/")
        sys.stdout.flush()
        httpd.serve_forever()


if __name__ == "__main__":
    main()
