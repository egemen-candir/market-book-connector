"""Deterministic candidate-entry retrieval via local embedding similarity.

Replaces the rule/vocabulary retriever. Retrieval runs offline over a
precomputed, hash-verified, record-level chunked index of the *enriched* catalog
(``build_embedding_index.py``). No LLM, no network.

ONE scoring core (template Rule 1):
    retrieve(packet, ...)            -> _queries_from_packet(packet)   ┐
    retrieve_from_texts(text, ...)   -> _queries_from_probe_text(text) ├─> _retrieve_core(queries, ...)
                                                                      ┘
``_retrieve_core`` is the only place that embeds queries and scores them against
the index. Packet mode and probe mode differ ONLY in how their query lists are
built -- never in scoring.

Binding facts (PRD connector_embedding_retriever_001):
  F3 - packet shape: ``summary`` + ``observations[]`` (statement/dimension/direction/magnitude).
  F4 - candidate_set is PACKET-LEVEL; one per packet. Preserve candidate_set_id,
       observation_id, candidate_count, candidates[] (entry_id, state_id,
       retrieval_score, retrieval_reasons), non_retrieved_notes. Drop
       rules_version; add a ``retrieval`` metadata block.
  F8 - determinism: torch.set_num_threads(1); scores 6 dp; tie-break (-score, entry_id).
  F9 - probe mode constructs the query set mechanically and routes through the
       SAME scoring core as packet mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
from pathlib import Path

# Offline env (F5) -- set before any HF/torch import. Reuses the index builder's
# helpers (model_sha, chunk_text, splitter) so there is one source of truth.
_HF_CACHE_ENV = os.environ.setdefault("HF_HOME", "")
if not _HF_CACHE_ENV:
    from connector.tools.paths import project_root as _project_root
    os.environ["HF_HOME"] = str(_project_root() / ".hf_cache")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np  # noqa: E402

from connector.tools.build_embedding_index import (  # noqa: E402
    _normalize,
    _SENT_SPLIT,
    chunk_text,
    compute_model_sha,
    load_model,
    load_tokenizer,
    model_file_hashes,
)
from connector.tools.io_utils import dump_json, load_json, load_yaml, sha256_file, write_json  # noqa: E402
from connector.tools.paths import catalog_dir, configs_dir, ensure_dir  # noqa: E402
from connector.tools.frozen_index_resolver import resolve_frozen_index_dir, frozen_retrieval_params  # noqa: E402

DEFAULT_TOP_K = 12
DEFAULT_ALPHA = 0.7
DEFAULT_MIN_FLOOR = 0.0  # F4/confirmed: disabled; Phase C calibrates from the OOB distribution.
DEFAULT_QUERY_MODE = "statements"
ID_HASH_LEN = 16

# Gate defaults (F8): shipped DISABLED. Retrieval-side no-match is OFF at MiniLM
# (baseline ruling: no operating point); the plumbing exists only because the
# gate-viability cost curves are model-dependent and re-emitted per cell. No
# calibrated floor is written here -- calibration is a POST-PASS step.
DEFAULT_GATE_ENABLED = False
DEFAULT_GATE_MODE = "margin"           # "margin" (top1 - basis) | "absolute" (raw top1 floor)
DEFAULT_GATE_MARGIN_BASIS = "median"   # "median" of scored | "top5" (5th-best)
DEFAULT_GATE_MIN_MARGIN = 0.0          # margin threshold (mode=margin)
DEFAULT_GATE_MIN_SCORE = 0.0           # absolute floor on top1 (mode=absolute)

# Legacy-compat defaults for EXPLICIT-index_dir callers (tests, fixture generator, eval harness
# that omit params). Unreachable from the production no-args path, which gets its params from the
# frozen pointer. Reproduces the values the T-cfg/glossed goldens were generated under.
_LEGACY_DEFAULTS = dict(top_k=12, alpha=0.7, min_floor=0.0, gate_enabled=False)


def _gate_config_block(
    gate_enabled: bool,
    gate_mode: str,
    gate_margin_basis: str,
    gate_min_margin: float,
    gate_min_score: float,
) -> dict | None:
    """F4/F8: the canonical gate config used for ``_config_hash`` and ``retrieval_meta``.

    Returns ``None`` when the gate is DISABLED (the default) so a disabled gate
    contributes nothing to the ``candidate_set_id`` and writes no ``retrieval_meta``
    field -- this is what keeps the default-off candidate_set byte-identical to the
    frozen output (T-cfg1). The block is the config only (no runtime verdict)."""
    if not gate_enabled:
        return None
    return {
        "mode": gate_mode,
        "margin_basis": gate_margin_basis,
        "min_margin": gate_min_margin,
        "min_score": gate_min_score,
    }

_INDEX_FILENAME = "catalog_embedding_index.npz"
_MANIFEST_FILENAME = "catalog_embedding_index.manifest.json"
_CATALOG_FILENAME = "market_book_catalog.stage6_enriched.json"
_TARGETS_FILENAME = "retrieval_targets.v1.yaml"

# ---------------------------------------------------------------------------
# Process-level caches (deterministic; single-threaded CPU). Per model_name: a
# cell-built index pins a different model, so the model / tokenizer / model_sha
# are resolved from the manifest's model_name, not a hard-coded incumbent.
# ---------------------------------------------------------------------------
_MODEL_CACHE: dict[str, object] = {}
_TOKENIZER_CACHE: dict[str, object] = {}
_MODEL_FILES_SHA_CACHE: dict[str, tuple[list[dict], str]] = {}
_INDEX_CACHE: dict[str, dict] = {}


def _get_model(model_name: str, model=None):
    if model is not None:
        return model
    if model_name not in _MODEL_CACHE:
        _MODEL_CACHE[model_name] = load_model(model_name)
    return _MODEL_CACHE[model_name]


def _get_tokenizer(model_name: str):
    if model_name not in _TOKENIZER_CACHE:
        _TOKENIZER_CACHE[model_name] = load_tokenizer(model_name)
    return _TOKENIZER_CACHE[model_name]


def _model_files_and_sha(model_name: str):
    if model_name not in _MODEL_FILES_SHA_CACHE:
        files = model_file_hashes(model_name)
        _MODEL_FILES_SHA_CACHE[model_name] = (files, compute_model_sha(files))
    return _MODEL_FILES_SHA_CACHE[model_name]


# ---------------------------------------------------------------------------
# Query constructors (Rule 1: each mode builds a query list, nothing more).
# ---------------------------------------------------------------------------
def _queries_from_packet(packet: dict, query_mode: str = DEFAULT_QUERY_MODE) -> list[dict]:
    """F3: summary + each observation.statement. magnitude is never folded."""
    queries: list[dict] = []
    summary = _normalize(packet.get("summary", ""))
    if summary:
        queries.append({"text": summary, "source": "packet_summary"})
    for obs in packet.get("observations") or []:
        stmt = _normalize(obs.get("statement", ""))
        if not stmt:
            continue
        text = stmt
        if query_mode == "statements_with_dims":
            parts = [_normalize(obs.get("dimension", ""))]
            direction = obs.get("direction")
            if direction:
                parts.append(_normalize(str(direction)))
            parts = [p for p in parts if p]
            if parts:
                text = f"{stmt} ({', '.join(parts)})"
        queries.append({"text": text, "source": "packet_observation"})
    return queries


def _queries_from_probe_text(text: str) -> list[dict]:
    """Template Rule 3: whole text (summary-analog) + each sentence (statement-analog),
    exact-string dedupe, order-preserving (whole text first)."""
    whole = _normalize(text)
    sentences = [_normalize(s) for s in _SENT_SPLIT.split(text or "")]
    sentences = [s for s in sentences if len(s) >= 3]
    queries: list[dict] = []
    seen: set[str] = set()
    if whole:
        queries.append({"text": whole, "source": "probe_full_text"})
        seen.add(whole)
    for s in sentences:
        if s in seen:  # Rule 3 dedupe
            continue
        seen.add(s)
        queries.append({"text": s, "source": "probe_sentence"})
    return queries


def _split_overlong_queries(queries: list[dict], tokenizer, max_tokens: int) -> list[dict]:
    """Template Rule 4: any query > max_tokens is split via the index builder's
    chunking helper (reused, not re-implemented) so the whole text is represented."""
    out: list[dict] = []
    for q in queries:
        t = q["text"]
        if len(tokenizer.encode(t, add_special_tokens=True)) <= max_tokens:
            out.append({"text": t, "source": q["source"]})
            continue
        for piece in chunk_text(t, tokenizer, max_tokens=max_tokens):
            if piece.strip():
                out.append({"text": piece, "source": q["source"]})
    return out


# ---------------------------------------------------------------------------
# Index loading + hash verification (fail loud; no silent rebuild, no network).
# ---------------------------------------------------------------------------
def _load_index(index_dir: Path | str) -> tuple[dict, dict]:
    """Load + cache the npz/manifest for ``index_dir``. Reloads when the npz file
    changes (mtime/size), so a rebuild into the same dir is picked up. Verifies
    model_sha once per load (catalog/targets are verified per call)."""
    index_dir = Path(index_dir)
    npz = index_dir / _INDEX_FILENAME
    manifest = index_dir / _MANIFEST_FILENAME
    if not npz.exists() or not manifest.exists():
        raise RuntimeError(
            f"Embedding index missing in {index_dir}. "
            f"Run: python3 -m connector.tools.build_embedding_index"
        )
    key = str(index_dir.resolve())
    stat = npz.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _INDEX_CACHE.get(key)
    if cached and cached.get("stamp") == stamp:
        return cached["arrays"], cached["manifest"]

    data = np.load(npz, allow_pickle=False)
    arrays = {k: data[k] for k in data.files}
    manifest_dict = json.loads(manifest.read_text(encoding="utf-8"))

    # Required arrays present.
    for k in ["vectors", "chunk_id", "state_id", "entry_id", "source_record_id", "kind", "chunk_text"]:
        if k not in arrays:
            raise RuntimeError(f"Index npz missing array '{k}': {npz}")

    # Verify model_sha (F5) against THIS cell's cached model files (the manifest
    # pins the cell's model_name; resolution is manifest-driven so bge/e5 indexes
    # verify against their own model, not a hard-coded incumbent).
    _, model_sha = _model_files_and_sha(manifest_dict["model_name"])
    if model_sha != manifest_dict.get("model_sha"):
        raise RuntimeError(
            "model_sha mismatch: the cached model no longer matches the index "
            f"manifest. Rebuild the index. (model={manifest_dict['model_name']}, "
            f"current={model_sha[:12]}.., "
            f"manifest={str(manifest_dict.get('model_sha'))[:12]}..)"
        )

    _INDEX_CACHE[key] = {"arrays": arrays, "manifest": manifest_dict, "stamp": stamp}
    return arrays, manifest_dict


def _verify_input_hashes(manifest: dict) -> None:
    """Per-call verification of catalog + retrieval_targets (the inputs that can
    change). Fail loud on mismatch -- the human must rebuild deliberately."""
    cat_path = catalog_dir() / _CATALOG_FILENAME
    tgt_path = configs_dir() / _TARGETS_FILENAME
    cat_sha = sha256_file(cat_path)
    tgt_sha = sha256_file(tgt_path)
    if cat_sha != manifest.get("catalog_sha256"):
        raise RuntimeError(
            "catalog_sha256 mismatch: the enriched catalog changed after the "
            "index was built. Rebuild: python3 -m connector.tools.build_embedding_index"
        )
    if tgt_sha != manifest.get("retrieval_targets_sha256"):
        raise RuntimeError(
            "retrieval_targets_sha256 mismatch: retrieval_targets.v1.yaml changed "
            "after the index was built. Rebuild the index."
        )


# ---------------------------------------------------------------------------
# candidate_set identity (F4: packet-level, deterministic).
# ---------------------------------------------------------------------------
def _config_hash(
    top_k: int, alpha: float, min_floor: float, query_mode: str, *, gate: dict | None = None
) -> str:
    # F4: the gate block enters the hash input ONLY when non-default (gate enabled).
    # A disabled gate contributes nothing, so the dict stays the 4-field incumbent
    # shape and the default-off candidate_set_id is byte-identical to the frozen
    # golden cset_id (T-cfg2). Canonicalize; never append keys unconditionally.
    h = {"top_k": top_k, "alpha": alpha, "min_floor": min_floor, "query_mode": query_mode}
    if gate is not None:
        h["gate"] = gate
    return hashlib.sha256(json.dumps(h, sort_keys=True).encode("utf-8")).hexdigest()


def _candidate_set_id(identity: str, catalog_sha: str, model_sha: str, config_hash: str) -> str:
    return "cand_" + hashlib.sha256(
        (identity + catalog_sha + model_sha + config_hash).encode("utf-8")
    ).hexdigest()[:ID_HASH_LEN]


# ---------------------------------------------------------------------------
# THE scoring core (Rule 1: nothing else scores).
# ---------------------------------------------------------------------------
def _retrieve_core(
    queries: list[dict],
    *,
    index_dir: Path | str,
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    min_floor: float = DEFAULT_MIN_FLOOR,
    query_mode: str = DEFAULT_QUERY_MODE,
    gate_enabled: bool = DEFAULT_GATE_ENABLED,
    gate_mode: str = DEFAULT_GATE_MODE,
    gate_margin_basis: str = DEFAULT_GATE_MARGIN_BASIS,
    gate_min_margin: float = DEFAULT_GATE_MIN_MARGIN,
    gate_min_score: float = DEFAULT_GATE_MIN_SCORE,
    provenance: dict,
    model=None,
) -> tuple[dict, list[dict], list[dict], dict]:
    """Embed queries, cosine-score vs the index, max-pool to entry, rank, gate.

    Returns (candidate_set, scored_entries, final_queries, retrieval_meta).
    """
    arrays, manifest = _load_index(index_dir)
    _verify_input_hashes(manifest)

    # F5/F7: the manifest is the source of truth for the cell -- both the model
    # (resolved per model_name) and the query prefix (read here, applied once at
    # the query embed below). packet/probe equivalence holds by construction
    # because both entry points route through this single core.
    model_name = manifest["model_name"]
    query_prefix = manifest.get("query_prefix", "")
    gate_cfg = _gate_config_block(
        gate_enabled, gate_mode, gate_margin_basis, gate_min_margin, gate_min_score
    )

    max_tokens = int(manifest["chunk_params"]["max_tokens"])
    tokenizer = _get_tokenizer(model_name)
    final_queries = _split_overlong_queries(queries, tokenizer, max_tokens)
    final_queries = [dict(q, query_id=f"q{i}") for i, q in enumerate(final_queries)]

    is_probe = provenance.get("query_source") == "probe"
    if is_probe:
        probe_id = provenance.get("probe_id")
        if probe_id:
            identity = str(probe_id)
        else:
            base = final_queries[0]["text"] if final_queries else ""
            identity = "probe_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:ID_HASH_LEN]
        observation_id_out = ""
        probe_id_out = probe_id or identity
    else:
        identity = str(provenance.get("observation_id", ""))
        observation_id_out = identity
        probe_id_out = None

    catalog_sha = manifest["catalog_sha256"]
    model_sha = manifest["model_sha"]
    cset_id = _candidate_set_id(
        identity, catalog_sha, model_sha,
        _config_hash(top_k, alpha, min_floor, query_mode, gate=gate_cfg),
    )

    retrieval_meta = {
        "query_source": provenance.get("query_source", "packet"),
        "model_name": model_name,  # F7: from the manifest, not the imported incumbent constant
        "model_sha": model_sha,
        "catalog_sha256": catalog_sha,
        "retrieval_targets_sha256": manifest["retrieval_targets_sha256"],
        "index_chunk_count": int(manifest["chunk_count"]),
        "dim": int(manifest["dim"]),
        "top_k": top_k,
        "alpha": alpha,
        "min_floor": min_floor,
        "query_mode": query_mode,
        "query_count": len(final_queries),
    }
    if probe_id_out is not None:
        retrieval_meta["probe_id"] = probe_id_out
    # F4 canonical provenance: query_prefix is recorded ONLY when non-empty -- an
    # empty prefix (the frozen MiniLM manifest) writes nothing, so the default-off
    # candidate_set stays byte-identical (T-cfg1).
    if query_prefix:
        retrieval_meta["query_prefix"] = query_prefix

    non_retrieved_notes: list[str] = []
    if not final_queries:
        non_retrieved_notes.append("No queries constructed from input; nothing retrieved.")
        candidate_set = {
            "candidate_set_id": cset_id,
            "observation_id": observation_id_out,
            "candidate_count": 0,
            "candidates": [],
            "non_retrieved_notes": non_retrieved_notes,
            "retrieval": retrieval_meta,
        }
        return candidate_set, [], final_queries, retrieval_meta

    # Embed queries (L2-normalized; cosine == dot product). F5: the manifest
    # query_prefix is applied to the encode input here -- the ONE encode call that
    # both packet and probe entry points route through (so they share the prefix).
    m = _get_model(model_name, model)
    q_texts = [q["text"] for q in final_queries]
    if query_prefix:
        q_texts = [query_prefix + t for t in q_texts]
    qvecs = m.encode(
        q_texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    ).astype(np.float32)
    vectors = arrays["vectors"]  # (n_chunks, dim), already L2-normalized
    sim = qvecs @ vectors.T  # (n_q, n_chunks)

    entry_ids = [str(x) for x in arrays["entry_id"].tolist()]
    state_ids = [str(x) for x in arrays["state_id"].tolist()]
    source_ids = [str(x) for x in arrays["source_record_id"].tolist()]

    # Group chunk indices by entry (first-seen order).
    order: list[str] = []
    entry_chunks: dict[str, dict] = {}
    for ci, eid in enumerate(entry_ids):
        if eid not in entry_chunks:
            entry_chunks[eid] = {"state_id": state_ids[ci], "idxs": []}
            order.append(eid)
        entry_chunks[eid]["idxs"].append(ci)

    n_q = sim.shape[0]
    scored: list[dict] = []
    for eid in order:
        info = entry_chunks[eid]
        idxs = info["idxs"]
        sub = sim[:, idxs]  # (n_q, m)
        best = float(sub.max())
        # Top-3 DISTINCT contributing records per candidate (fix plan item 4): for
        # each source_record_id keep its highest cosine + the query_id that produced
        # it, then sort those per-record maxima and emit at most three reason objects
        # (never the same record twice).
        per_record: dict[str, tuple[float, str]] = {}
        for ci in idxs:
            rid = source_ids[ci]
            for qi in range(n_q):
                s = float(sim[qi, ci])
                qid = final_queries[qi]["query_id"]
                cur = per_record.get(rid)
                if cur is None or s > cur[0] or (s == cur[0] and qid < cur[1]):
                    per_record[rid] = (s, qid)
        record_list = [(s, rid, qid) for rid, (s, qid) in per_record.items()]
        record_list.sort(key=lambda t: (-t[0], t[2], t[1]))
        reasons = [
            {"source_record_id": rid, "cosine": round(s, 6), "query_id": qid}
            for s, rid, qid in record_list[:3]
        ]
        scored.append({
            "entry_id": eid,
            "state_id": info["state_id"],
            "retrieval_score": round(best, 6),
            "retrieval_reasons": reasons,
        })

    # Rank (F8: tie-break (-score, entry_id)).
    scored.sort(key=lambda e: (-e["retrieval_score"], e["entry_id"]))
    top1 = scored[0]["retrieval_score"]
    threshold = max(alpha * top1, min_floor)
    selected = [e for e in scored if e["retrieval_score"] >= threshold][:top_k]

    # Gate (F8): default OFF = no-op -- the selection above is byte-identical to the
    # pre-change max(alpha*top1, floor) selection. When enabled, evaluate a no-match
    # flag over the ranked shortlist and emit the FULL shortlist PLUS a no-match flag
    # (retrieval never silently drops the shortlist; the evaluator returns the empty).
    # gate_cfg is None when disabled -> nothing is recorded (T-cfg1 byte-identity).
    if gate_cfg is not None:
        top1_score = top1
        if gate_mode == "absolute":
            basis_score = None
            no_match = top1_score < gate_min_score
            trigger = f"top1={top1_score:.6f} < min_score={gate_min_score:.6f}"
        else:  # margin
            scores = [e["retrieval_score"] for e in scored]
            if gate_margin_basis == "top5":
                basis_score = scores[min(4, len(scores) - 1)]
            else:  # median
                basis_score = statistics.median(scores)
            margin = top1_score - basis_score
            no_match = margin < gate_min_margin
            trigger = (
                f"margin={margin:.6f} (top1={top1_score:.6f} - "
                f"{gate_margin_basis}={basis_score:.6f}) < min_margin={gate_min_margin:.6f}"
            )
        gate_meta = dict(gate_cfg)
        gate_meta.update({
            "no_match": bool(no_match),
            "top1": top1_score,
            "basis_score": basis_score,
        })
        retrieval_meta["gate"] = gate_meta
        if no_match:
            non_retrieved_notes.append(
                f"gate_no_match: {trigger}; candidate set flagged no-match "
                f"(full shortlist retained; evaluator returns the honest empty)."
            )

    candidates = [
        {
            "entry_id": e["entry_id"],
            "state_id": e["state_id"],
            "retrieval_score": e["retrieval_score"],
            "retrieval_reasons": e["retrieval_reasons"],
        }
        for e in selected
    ]

    candidate_set = {
        "candidate_set_id": cset_id,
        "observation_id": observation_id_out,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "non_retrieved_notes": non_retrieved_notes,
        "retrieval": retrieval_meta,
    }
    return candidate_set, scored, final_queries, retrieval_meta


# ---------------------------------------------------------------------------
# Public entry points (Rule 1: both route through _retrieve_core).
# ---------------------------------------------------------------------------
def retrieve(
    packet: dict,
    *,
    index_dir: Path | str | None = None,
    top_k: int | None = None,
    alpha: float | None = None,
    min_floor: float | None = None,
    query_mode: str = DEFAULT_QUERY_MODE,
    gate_enabled: bool | None = None,
    gate_mode: str = DEFAULT_GATE_MODE,
    gate_margin_basis: str = DEFAULT_GATE_MARGIN_BASIS,
    gate_min_margin: float = DEFAULT_GATE_MIN_MARGIN,
    gate_min_score: float = DEFAULT_GATE_MIN_SCORE,
) -> dict:
    """Packet-mode retrieval. Returns a candidate_set dict (one per packet, F4)."""
    if index_dir is None:
        # production no-args path: the whole freeze comes from the pointer (index + k/alpha/floor/gate)
        index_dir = resolve_frozen_index_dir()
        _fp = frozen_retrieval_params()
        if top_k is None: top_k = _fp["top_k"]
        if alpha is None: alpha = _fp["alpha"]
        if min_floor is None: min_floor = _fp["min_floor"]
        if gate_enabled is None: gate_enabled = _fp["gate_enabled"]
    else:
        # explicit-index_dir callers (tests, generator, eval harness): legacy-compat defaults for any
        # param left unspecified -- reproduces the values the frozen goldens were generated under.
        if top_k is None: top_k = _LEGACY_DEFAULTS["top_k"]
        if alpha is None: alpha = _LEGACY_DEFAULTS["alpha"]
        if min_floor is None: min_floor = _LEGACY_DEFAULTS["min_floor"]
        if gate_enabled is None: gate_enabled = _LEGACY_DEFAULTS["gate_enabled"]
    queries = _queries_from_packet(packet, query_mode=query_mode)
    candidate_set, _scored, _queries, _meta = _retrieve_core(
        queries,
        index_dir=index_dir,
        top_k=top_k,
        alpha=alpha,
        min_floor=min_floor,
        query_mode=query_mode,
        gate_enabled=gate_enabled,
        gate_mode=gate_mode,
        gate_margin_basis=gate_margin_basis,
        gate_min_margin=gate_min_margin,
        gate_min_score=gate_min_score,
        provenance={
            "query_source": "packet",
            "observation_id": packet.get("observation_id", ""),
        },
    )
    return candidate_set


def retrieve_from_texts(
    probe_text: str,
    *,
    index_dir: Path | str | None = None,
    top_k: int | None = None,
    alpha: float | None = None,
    min_floor: float | None = None,
    probe_id: str | None = None,
    gate_enabled: bool | None = None,
    gate_mode: str = DEFAULT_GATE_MODE,
    gate_margin_basis: str = DEFAULT_GATE_MARGIN_BASIS,
    gate_min_margin: float = DEFAULT_GATE_MIN_MARGIN,
    gate_min_score: float = DEFAULT_GATE_MIN_SCORE,
    model=None,
) -> tuple[dict, dict]:
    """Probe-mode retrieval (F9): deterministic, no LLM, SAME scoring core as
    ``retrieve()``. Returns (candidate_set, trace) with identical shapes to packet
    mode (template Rule 5) and provenance query_source="probe"."""
    if index_dir is None:
        # production no-args path: the whole freeze comes from the pointer (index + k/alpha/floor/gate)
        index_dir = resolve_frozen_index_dir()
        _fp = frozen_retrieval_params()
        if top_k is None: top_k = _fp["top_k"]
        if alpha is None: alpha = _fp["alpha"]
        if min_floor is None: min_floor = _fp["min_floor"]
        if gate_enabled is None: gate_enabled = _fp["gate_enabled"]
    else:
        # explicit-index_dir callers (tests, generator, eval harness): legacy-compat defaults for any
        # param left unspecified -- reproduces the values the frozen goldens were generated under.
        if top_k is None: top_k = _LEGACY_DEFAULTS["top_k"]
        if alpha is None: alpha = _LEGACY_DEFAULTS["alpha"]
        if min_floor is None: min_floor = _LEGACY_DEFAULTS["min_floor"]
        if gate_enabled is None: gate_enabled = _LEGACY_DEFAULTS["gate_enabled"]
    queries = _queries_from_probe_text(probe_text)
    candidate_set, scored, final_queries, meta = _retrieve_core(
        queries,
        index_dir=index_dir,
        top_k=top_k,
        alpha=alpha,
        min_floor=min_floor,
        query_mode="probe",
        gate_enabled=gate_enabled,
        gate_mode=gate_mode,
        gate_margin_basis=gate_margin_basis,
        gate_min_margin=gate_min_margin,
        gate_min_score=gate_min_score,
        provenance={"query_source": "probe", "probe_id": probe_id},
        model=model,
    )
    return candidate_set, _trace_from_core(candidate_set, scored, final_queries, meta)


def build_trace(
    packet: dict,
    *,
    index_dir: Path | str | None = None,
    top_k: int | None = None,
    alpha: float | None = None,
    min_floor: float | None = None,
    query_mode: str = DEFAULT_QUERY_MODE,
    gate_enabled: bool | None = None,
    gate_mode: str = DEFAULT_GATE_MODE,
    gate_margin_basis: str = DEFAULT_GATE_MARGIN_BASIS,
    gate_min_margin: float = DEFAULT_GATE_MIN_MARGIN,
    gate_min_score: float = DEFAULT_GATE_MIN_SCORE,
) -> dict:
    """Diagnostic trace: ranked observable entries + top contributing records +
    a near-miss band (the 5 highest-ranked entries that were not retrieved)."""
    if index_dir is None:
        # production no-args path: the whole freeze comes from the pointer (index + k/alpha/floor/gate)
        index_dir = resolve_frozen_index_dir()
        _fp = frozen_retrieval_params()
        if top_k is None: top_k = _fp["top_k"]
        if alpha is None: alpha = _fp["alpha"]
        if min_floor is None: min_floor = _fp["min_floor"]
        if gate_enabled is None: gate_enabled = _fp["gate_enabled"]
    else:
        # explicit-index_dir callers (tests, generator, eval harness): legacy-compat defaults for any
        # param left unspecified -- reproduces the values the frozen goldens were generated under.
        if top_k is None: top_k = _LEGACY_DEFAULTS["top_k"]
        if alpha is None: alpha = _LEGACY_DEFAULTS["alpha"]
        if min_floor is None: min_floor = _LEGACY_DEFAULTS["min_floor"]
        if gate_enabled is None: gate_enabled = _LEGACY_DEFAULTS["gate_enabled"]
    queries = _queries_from_packet(packet, query_mode=query_mode)
    candidate_set, scored, final_queries, meta = _retrieve_core(
        queries,
        index_dir=index_dir,
        top_k=top_k,
        alpha=alpha,
        min_floor=min_floor,
        query_mode=query_mode,
        gate_enabled=gate_enabled,
        gate_mode=gate_mode,
        gate_margin_basis=gate_margin_basis,
        gate_min_margin=gate_min_margin,
        gate_min_score=gate_min_score,
        provenance={
            "query_source": "packet",
            "observation_id": packet.get("observation_id", ""),
        },
    )
    return _trace_from_core(candidate_set, scored, final_queries, meta)


def _trace_from_core(candidate_set: dict, scored: list[dict], final_queries: list[dict], meta: dict) -> dict:
    selected_ids = {c["entry_id"] for c in candidate_set["candidates"]}
    ranked = [
        {
            "rank": i + 1,
            "entry_id": e["entry_id"],
            "state_id": e["state_id"],
            "retrieval_score": e["retrieval_score"],
            "retrieved": e["entry_id"] in selected_ids,
            "top_source_record_ids": [r["source_record_id"] for r in e["retrieval_reasons"][:3]],
        }
        for i, e in enumerate(scored)
    ]
    near_miss = [
        {
            "entry_id": e["entry_id"],
            "state_id": e["state_id"],
            "retrieval_score": e["retrieval_score"],
            "top_reason": e["retrieval_reasons"][0] if e["retrieval_reasons"] else None,
        }
        for e in scored
        if e["entry_id"] not in selected_ids
    ][:5]
    return {
        "candidate_set_id": candidate_set["candidate_set_id"],
        "observation_id": candidate_set["observation_id"],
        "queries": [
            {"query_id": q["query_id"], "text": q["text"], "source": q["source"]}
            for q in final_queries
        ],
        "ranked_observable_entries": ranked,
        "near_miss_band": near_miss,
        "retrieval": meta,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _iter_probes(probe_doc) -> list[tuple[str, str]]:
    """Normalize a frozen probe set into [(probe_id, text), ...]."""
    items = probe_doc.get("probes") if isinstance(probe_doc, dict) else probe_doc
    if not isinstance(items, list):
        raise ValueError("Probe set must be a list under 'probes:' (or a top-level list).")
    out = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            pid = item.get("probe_id") or item.get("id") or f"probe_{i:04d}"
            text = item.get("text") or item.get("commentary") or item.get("probe_text") or ""
        else:
            pid = f"probe_{i:04d}"
            text = str(item)
        if text and text.strip():
            out.append((str(pid), str(text)))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Retrieve candidate Market Book entries (embedding similarity).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--packet", help="Path to observation packet YAML (packet mode).")
    g.add_argument("--probe-file", help="Path to a frozen probe set YAML/JSON (batch probe mode).")
    g.add_argument("--probe-text", help="A single ad-hoc probe string (probe mode).")
    ap.add_argument("--index-dir", default=None, help="Dir containing the index npz+manifest (defaults to catalog dir).")
    ap.add_argument("--out-dir", default=None, help="Output directory.")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--min-floor", type=float, default=None)
    ap.add_argument(
        "--query-mode",
        default=DEFAULT_QUERY_MODE,
        choices=["statements", "statements_with_dims"],
        help="Packet query construction (ignored for probe mode).",
    )
    ap.add_argument(
        "--gate-enabled",
        action="store_true",
        help="Enable the no-match gate (default OFF; retrieval-side no-match is OFF at MiniLM).",
    )
    ap.add_argument("--gate-mode", default=DEFAULT_GATE_MODE, choices=["margin", "absolute"])
    ap.add_argument("--gate-margin-basis", default=DEFAULT_GATE_MARGIN_BASIS, choices=["median", "top5"])
    ap.add_argument("--gate-min-margin", type=float, default=DEFAULT_GATE_MIN_MARGIN)
    ap.add_argument("--gate-min-score", type=float, default=DEFAULT_GATE_MIN_SCORE)
    args = ap.parse_args(argv)

    # CLI resolves here, before calling; the library pointer/legacy branch is unreachable-by-design on
    # this path (CLI passes everything explicitly downstream). Same logic, different location.
    if args.index_dir:
        index_dir = Path(args.index_dir)
        _lg = _LEGACY_DEFAULTS
        if args.top_k is None: args.top_k = _lg["top_k"]
        if args.alpha is None: args.alpha = _lg["alpha"]
        if args.min_floor is None: args.min_floor = _lg["min_floor"]
    else:
        index_dir = resolve_frozen_index_dir()
        _fp = frozen_retrieval_params()
        if args.top_k is None: args.top_k = _fp["top_k"]
        if args.alpha is None: args.alpha = _fp["alpha"]
        if args.min_floor is None: args.min_floor = _fp["min_floor"]
    # NOTE: --gate-enabled is store_true, so 'unspecified' and 'explicitly off' are indistinguishable
    # on the CLI. Harmless here because legacy and pointer BOTH say gate off; a future gate-on freeze
    # must rework this flag (e.g. store None / a --gate/--no-gate pair).

    if args.packet:
        packet = load_yaml(Path(args.packet))
        candidate_set = retrieve(
            packet, index_dir=index_dir, top_k=args.top_k, alpha=args.alpha,
            min_floor=args.min_floor, query_mode=args.query_mode,
            gate_enabled=args.gate_enabled, gate_mode=args.gate_mode,
            gate_margin_basis=args.gate_margin_basis, gate_min_margin=args.gate_min_margin,
            gate_min_score=args.gate_min_score,
        )
        trace = build_trace(
            packet, index_dir=index_dir, top_k=args.top_k, alpha=args.alpha,
            min_floor=args.min_floor, query_mode=args.query_mode,
            gate_enabled=args.gate_enabled, gate_mode=args.gate_mode,
            gate_margin_basis=args.gate_margin_basis, gate_min_margin=args.gate_min_margin,
            gate_min_score=args.gate_min_score,
        )
        if args.out_dir:
            out = ensure_dir(Path(args.out_dir))
            write_json(out / "candidate_entries.json", candidate_set)
            write_json(out / "retrieval_trace.json", trace)
        print(f"candidate_set_id: {candidate_set['candidate_set_id']}")
        print(f"candidate_count:  {candidate_set['candidate_count']}")
        for c in candidate_set["candidates"]:
            print(f"  {c['entry_id']}  score={c['retrieval_score']}")
        return

    if args.probe_file:
        ppath = Path(args.probe_file)
        probe_doc = load_yaml(ppath) if ppath.suffix in (".yaml", ".yml") else load_json(ppath)
        probes = _iter_probes(probe_doc)
        if not probes:
            raise ValueError(f"No probes with non-empty text found in {ppath}")
        if not args.out_dir:
            raise SystemExit("--out-dir is required for --probe-file (batch writes per-probe JSONs).")
        out = ensure_dir(Path(args.out_dir))
        # One model load for the whole batch (F9) -- resolved from the index's
        # manifest so a cell-built index uses its own model, not a hard-coded incumbent.
        _, _batch_manifest = _load_index(index_dir)
        model = _get_model(_batch_manifest["model_name"])
        written = []
        for probe_id, text in probes:
            candidate_set, _trace = retrieve_from_texts(
                text, index_dir=index_dir, top_k=args.top_k, alpha=args.alpha,
                min_floor=args.min_floor, probe_id=probe_id, model=model,
                gate_enabled=args.gate_enabled, gate_mode=args.gate_mode,
                gate_margin_basis=args.gate_margin_basis, gate_min_margin=args.gate_min_margin,
                gate_min_score=args.gate_min_score,
            )
            write_json(out / f"{probe_id}.candidate_set.json", candidate_set)
            written.append(probe_id)
        print(f"probes processed: {len(written)}")
        print(f"per-probe JSONs:  {out}")
        return

    # args.probe_text
    candidate_set, trace = retrieve_from_texts(
        args.probe_text, index_dir=index_dir, top_k=args.top_k, alpha=args.alpha,
        min_floor=args.min_floor,
        gate_enabled=args.gate_enabled, gate_mode=args.gate_mode,
        gate_margin_basis=args.gate_margin_basis, gate_min_margin=args.gate_min_margin,
        gate_min_score=args.gate_min_score,
    )
    if args.out_dir:
        out = ensure_dir(Path(args.out_dir))
        pid = candidate_set["retrieval"].get("probe_id", "probe_adhoc")
        write_json(out / f"{pid}.candidate_set.json", candidate_set)
        write_json(out / "retrieval_trace.json", trace)
    print(dump_json(candidate_set))


if __name__ == "__main__":
    main()
