"""frozen_index_resolver.py — load-bearing resolver for the config freeze of record.

Two guarantees: the retrieval params are load-bearing (Fix A), and the index anchor is recomputed
from the loaded arrays (Fix B). The default-resolution points in retrieve_candidate_entries.py pull
BOTH the index dir AND the retrieval params from the pointer when no explicit args are given.

Fail-loud semantics (no fallback):
  - pointer (connector/configs/frozen_config.yaml) MUST exist                       -> RuntimeError
  - resolved index dir MUST exist with npz + manifest                              -> RuntimeError
  - anchor check is THREE-WAY: pointer anchor == RECOMPUTED-from-npz == manifest    -> else RuntimeError
    (Fix B: recompute from the loaded arrays via the builder's canonical helper; a manifest's
     self-report is not trusted — a swapped npz beside an intact manifest must not pass.)

Fix A (params load-bearing): frozen_retrieval_params() returns {top_k, alpha, min_floor, gate} from the
pointer so the no-args production path runs the WHOLE freeze (k=15, alpha=0, gate off), not just the
index. Explicit caller args still override (the eval harness's --top-k 26 --alpha 0 is unaffected).
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import yaml

from connector.tools.paths import configs_dir
# Fix B: import the builder's canonical content-hash helper so the recompute uses IDENTICAL semantics.
# content_sha256 (build_embedding_index.py L278) = sha256(_array_bytes_concat(arrays)); it is the exact
# function the builder calls (L461) to write the manifest's content_sha256, so recompute == build.
# If importing the builder ever creates a cycle, lift content_sha256 into connector/tools/index_hash.py
# and have both the builder and this resolver import it from there.
from connector.tools.build_embedding_index import content_sha256  # canonical helper (L278): sha256(_array_bytes_concat(arrays))

_POINTER_NAME = "frozen_config.yaml"
_MANIFEST_FILENAME = "catalog_embedding_index.manifest.json"
_NPZ_FILENAME = "catalog_embedding_index.npz"


def _pointer_path() -> Path:
    return configs_dir() / _POINTER_NAME


def load_frozen_config() -> dict:
    """Return frozen_config_of_record, or raise if the pointer is absent/malformed."""
    p = _pointer_path()
    if not p.exists():
        raise RuntimeError(
            f"frozen config pointer missing: {p}. The freeze of record IS this file; its absence is an "
            f"error, not a reason to fall back to the pre-gloss catalog or to signature defaults."
        )
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = doc.get("frozen_config_of_record")
    if not isinstance(cfg, dict):
        raise RuntimeError(f"frozen config pointer malformed (no frozen_config_of_record): {p}")
    for req in ("index_dir", "content_sha256", "k", "alpha", "min_floor", "gate"):
        if req not in cfg:
            raise RuntimeError(f"frozen config pointer missing required key '{req}': {p}")
    return cfg


def frozen_retrieval_params() -> dict:
    """Fix A: the retrieval params of record. Used at the default-resolution point so the no-args
    production path runs the whole freeze. Returns {top_k, alpha, min_floor, gate_enabled}."""
    cfg = load_frozen_config()
    gate = cfg["gate"]
    gate_enabled = False if str(gate).lower() in ("disabled", "off", "false", "0") else bool(gate)
    return {
        "top_k": int(cfg["k"]),
        "alpha": float(cfg["alpha"]),
        "min_floor": float(cfg["min_floor"]),
        "gate_enabled": gate_enabled,
    }


def resolve_frozen_index_dir() -> Path:
    """Resolve the frozen index dir from the pointer with a THREE-WAY anchor check (Fix B).

    pointer anchor == recomputed-from-npz == manifest.content_sha256, or RuntimeError.
    """
    cfg = load_frozen_config()
    connector_root = configs_dir().parent
    idx = (connector_root / cfg["index_dir"]).resolve()

    npz = idx / _NPZ_FILENAME
    man = idx / _MANIFEST_FILENAME
    if not npz.exists() or not man.exists():
        raise RuntimeError(
            f"frozen index incomplete at {idx} (need {_NPZ_FILENAME} + {_MANIFEST_FILENAME}); "
            f"pointer {_pointer_path()} points at a missing/partial index."
        )

    pointer_anchor = cfg["content_sha256"]
    manifest_anchor = json.loads(man.read_text(encoding="utf-8")).get("content_sha256")

    # Fix B: recompute from the actual arrays via the builder's canonical helper — do not trust the
    # manifest's self-report. Load the embeddings the same way the builder hashes them.
    with np.load(npz, allow_pickle=False) as data:
        # content_sha256 (builder L278) hashes _array_bytes_concat over the arrays dict; materialize
        # the NpzFile into a plain {name: ndarray} mapping so the recompute matches the build exactly.
        recomputed = content_sha256({k: data[k] for k in data.files})

    if not (pointer_anchor == recomputed == manifest_anchor):
        raise RuntimeError(
            "frozen index anchor mismatch (three-way check failed) at "
            f"{idx}: pointer={pointer_anchor} recomputed={recomputed} manifest={manifest_anchor}. "
            "Refusing to serve an index that is not, byte-for-byte, the frozen config of record."
        )
    return idx


# --- wiring (documentation for the wiring step; not executed here) ----------------------------------
# retrieve_candidate_entries.py, at each default-resolution point (retrieve L523, retrieve_from_texts
# L563, build_trace L599, CLI default L706): change the signature defaults for top_k/alpha/min_floor/
# gate_enabled to None sentinels, and where index_dir currently falls back to catalog_dir():
#
#     if index_dir is None:
#         index_dir = resolve_frozen_index_dir()
#         _fp = frozen_retrieval_params()
#         if top_k is None:       top_k = _fp["top_k"]
#         if alpha is None:       alpha = _fp["alpha"]
#         if min_floor is None:   min_floor = _fp["min_floor"]
#         if gate_enabled is None: gate_enabled = _fp["gate_enabled"]
#     else:
#         # explicit index_dir -> keep existing signature defaults for any still-None params
#         ...
# Explicit caller args (eval harness --top-k 26 --alpha 0; tests passing index_dir=INDEX_DIR) override
# throughout, so all existing guards and the eval harness are unaffected. Only the no-args production
# path changes — and it now gets the whole freeze (index + k=15 + alpha=0 + gate off), not a third of it.
