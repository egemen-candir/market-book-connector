"""Build a deterministic, observable-only embedding index for the connector.

Replaces the rule/vocabulary retriever's data source with a precomputed,
hash-verified, record-level chunked index over the *enriched* catalog.

Binding facts (PRD connector_embedding_retriever_001):
  F1 - observable-only: the 5 ``methodological_caveat`` states are EXCLUDED;
       26 observable states are indexed.
  F2 - tokenizer-true chunking: ``state_summary`` + each ``linked_records[].text``
       (clean text, NOT the ID-labeled ``embed_text``), chunked to <=256 wordpieces
       measured by the MiniLM tokenizer (target ~200), sentence-aware greedy
       packing, word-level split for any single over-cap sentence.
  F5 - offline model: ``all-MiniLM-L6-v2`` cached at ``project_root()/.hf_cache``;
       NO network. ``model_sha`` is a sorted-concat digest over
       {model.safetensors (or pytorch_model.bin), tokenizer.json, config.json}.
  F8 - determinism: ``torch.set_num_threads(1)``; the determinism fingerprint is
       over concatenated raw array bytes + a stable manifest subset -- NOT the
       .npz file bytes (np.savez zips carry timestamps).

Outputs (under ``connector/catalog/``):
  catalog_embedding_index.npz         vectors + parallel arrays
  catalog_embedding_index.manifest.json   hashes + provenance + chunk params

This module also exposes the NORMATIVE sentence splitter and the chunking helper
that probe-mode retrieval reuses (template Rule 4: do not re-implement).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from connector.tools.io_utils import dump_json, load_yaml, now_utc_iso, sha256_file
from connector.tools.paths import catalog_dir, configs_dir, project_root

# ---------------------------------------------------------------------------
# Offline environment (F5). MUST be set before torch/transformers import so the
# hub never reaches the network under Landlock. The model is expected to already
# be cached at project_root()/.hf_cache; we fail loud if it is not.
# ---------------------------------------------------------------------------
_HF_CACHE = project_root() / ".hf_cache"
os.environ.setdefault("HF_HOME", str(_HF_CACHE))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np  # noqa: E402
import torch  # noqa: E402

torch.set_num_threads(1)  # F8: deterministic CPU embedding

from transformers import AutoTokenizer  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # incumbent / default cell (F9 minilm)

# F9 cell registry: exact HF repo ids + each model's retrieval-recipe prefixes.
# minilm is the default (incumbent); bge/e5 are the per-model cells built to
# re-test the 10-state academic-register gap. Do NOT guess bge/e5 ids or default
# bge to empty prefixes -- bge's query instruction is its documented retrieval
# recipe, and an off-convention bge would mis-adjudicate the register-gap question.
CELL_REGISTRY = {
    "minilm": {
        "model_name": "sentence-transformers/all-MiniLM-L6-v2",
        "query_prefix": "",
        "passage_prefix": "",
    },
    "bge": {
        "model_name": "BAAI/bge-small-en-v1.5",
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    "e5": {
        "model_name": "intfloat/e5-small-v2",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}
DEFAULT_CELL = "minilm"


def _repo_dirname(model_name: str) -> str:
    """HF hub cache dir name for a repo id (``org/model`` -> ``models--org--model``)."""
    return "models--" + model_name.replace("/", "--")

CATALOG_FILENAME = "market_book_catalog.stage6_enriched.json"
RETRIEVAL_TARGETS_FILENAME = "retrieval_targets.v1.yaml"
GLOSSES_FILENAME = "glosses.v1.yaml"
INDEX_FILENAME = "catalog_embedding_index.npz"
MANIFEST_FILENAME = "catalog_embedding_index.manifest.json"

# D2 (PRD connector_gloss_rebaseline_003): the accepted index-chunk kinds. The
# record families mirror the enriched-catalog linked-record kinds
# (build_market_book_catalog._ENRICH_KIND_ORDER) plus ``state_summary``; ``gloss``
# is admitted here as a RETRIEVAL-SURFACE kind. PRD-3 intent: a gloss is the
# corpus's own paraphrase of a state, surfaced to retrieval (FOUND) but never
# assembled into an evidence payload (NOT CITED). ``build_index`` asserts every
# emitted chunk kind is in this set, so admitting ``gloss`` here is what lets the
# glossed merge pass validation.
ACCEPTED_KINDS = ("state_summary", "signature", "claim", "policy", "failure", "gloss")

# Chunking knobs (F2). wordpieces are measured with add_special_tokens=True so a
# chunk never exceeds the model's real 256-token truncation point ([CLS]+content+[SEP]).
DEFAULT_MAX_TOKENS = 256
DEFAULT_TARGET_TOKENS = 200

# F5: the exact file set whose hashes compose model_sha. We prefer
# model.safetensors; pytorch_model.bin is the documented fallback.
MODEL_SHA_FILES = ["model.safetensors", "tokenizer.json", "config.json"]
MODEL_SHA_FILES_FALLBACK = ["pytorch_model.bin", "tokenizer.json", "config.json"]

# ---------------------------------------------------------------------------
# Normative sentence splitter (probe-mode template Rule 2). Copy verbatim; do
# not "improve" -- its failure modes are uniform across all probes.
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'])|\n+')


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ---------------------------------------------------------------------------
# Chunking helper (F2). Used by the index builder AND reused by probe-mode
# retrieval for over-cap queries (template Rule 4).
# ---------------------------------------------------------------------------
def chunk_text(
    text: str,
    tokenizer,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
) -> list[str]:
    """Sentence-aware greedy packing into chunks of <= ``max_tokens`` wordpieces.

    A single sentence longer than ``max_tokens`` is word-level split. Chunks are
    flushed once they reach ``target_tokens`` so we do not always ride the cap.
    """
    text = _normalize(text)
    if not text:
        return []

    def ntok(s: str) -> int:
        return len(tokenizer.encode(s, add_special_tokens=True))

    raw_sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]

    # Break any over-cap sentence into word-level sub-units <= max_tokens.
    units: list[str] = []
    for sent in raw_sentences:
        if ntok(sent) <= max_tokens:
            units.append(sent)
            continue
        cur = ""
        for word in sent.split():
            cand = f"{cur} {word}".strip() if cur else word
            if ntok(cand) <= max_tokens:
                cur = cand
            else:
                if cur:
                    units.append(cur)
                cur = word  # a lone word cannot exceed max_tokens in practice
        if cur:
            units.append(cur)

    # Greedy packing with a target flush.
    chunks: list[str] = []
    cur = ""
    for unit in units:
        cand = f"{cur} {unit}".strip() if cur else unit
        if ntok(cand) <= max_tokens:
            cur = cand
            if ntok(cur) >= target_tokens:
                chunks.append(cur)
                cur = ""
        else:
            if cur:
                chunks.append(cur)
            cur = unit
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Model resolution + model_sha (F5)
# ---------------------------------------------------------------------------
def model_snapshot_dir(model_name: str = MODEL_NAME) -> Path:
    """Resolve the cached snapshot dir from refs/main. Fail loud if absent."""
    repo_dir = _HF_CACHE / "hub" / _repo_dirname(model_name)
    refs = repo_dir / "refs" / "main"
    if not refs.exists():
        raise RuntimeError(
            f"Offline model not cached at {_HF_CACHE}. Expected "
            f"{refs} to exist. Pre-cache '{model_name}' (no network under Landlock)."
        )
    rev = refs.read_text(encoding="utf-8").strip()
    snapshot = repo_dir / "snapshots" / rev
    if not snapshot.is_dir():
        raise RuntimeError(
            f"Model snapshot dir missing: {snapshot} (cache is corrupt)."
        )
    return snapshot


def model_file_hashes(model_name: str = MODEL_NAME) -> list[dict]:
    """Per-file sha256 for the F5 file set that is present in the snapshot."""
    snapshot = model_snapshot_dir(model_name)
    chosen = MODEL_SHA_FILES
    if not (snapshot / "model.safetensors").exists() and (
        snapshot / "pytorch_model.bin"
    ).exists():
        chosen = MODEL_SHA_FILES_FALLBACK
    files: list[dict] = []
    for name in chosen:
        fpath = snapshot / name
        if not fpath.exists():
            raise RuntimeError(
                f"model_sha file missing in snapshot: {name} ({snapshot})"
            )
        files.append({"name": name, "sha256": sha256_file(fpath)})
    return files


def compute_model_sha(file_hashes: list[dict] | None = None) -> str:
    """F5: sha256 of the sorted-concat digest over (filename, filehash) pairs."""
    files = file_hashes if file_hashes is not None else model_file_hashes()
    pairs = sorted((f["name"], f["sha256"]) for f in files)
    concat = "".join(f"{name}{digest}" for name, digest in pairs)
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()


def load_tokenizer(model_name: str = MODEL_NAME):
    return AutoTokenizer.from_pretrained(model_name)


def load_model(model_name: str = MODEL_NAME):
    """Load SentenceTransformer on CPU (offline). Deterministic via set_num_threads(1)."""
    # Touch the snapshot first so a missing model fails loud with our message
    # before SentenceTransformer attempts any hub resolution.
    model_snapshot_dir(model_name)
    return SentenceTransformer(model_name, device="cpu")


# ---------------------------------------------------------------------------
# State classification (F1)
# ---------------------------------------------------------------------------
def load_state_classification(targets_path: Path) -> tuple[list[str], list[str], dict]:
    """Return (observable_state_ids, methodological_state_ids, class_map)."""
    targets = load_yaml(targets_path)
    observable: list[str] = []
    methodological: list[str] = []
    class_map: dict[str, str] = {}
    for st in targets.get("states") or []:
        sid = st.get("state_id", "")
        cls = st.get("class", "")
        if not sid:
            continue
        class_map[sid] = cls
        if cls == "observable":
            observable.append(sid)
        elif cls == "methodological_caveat":
            methodological.append(sid)
    return observable, methodological, class_map


# ---------------------------------------------------------------------------
# Determinism fingerprint (F8): over array bytes + a stable manifest subset.
# ---------------------------------------------------------------------------
def _array_bytes_concat(arrays: dict) -> bytes:
    keys = ["vectors", "chunk_id", "state_id", "entry_id", "source_record_id", "kind", "chunk_text"]
    parts = [np.ascontiguousarray(arrays[k]).tobytes() for k in keys]
    return b"".join(parts)


def content_sha256(arrays: dict) -> str:
    return hashlib.sha256(_array_bytes_concat(arrays)).hexdigest()


def determinism_sha256(arrays: dict, stable_manifest: dict) -> str:
    raw = _array_bytes_concat(arrays)
    raw += json.dumps(stable_manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------
def build_index(
    *,
    catalog_path: Path | None = None,
    targets_path: Path | None = None,
    index_dir: Path | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    model_name: str = MODEL_NAME,
    query_prefix: str = "",
    passage_prefix: str = "",
    glosses_path: Path | None = None,
) -> dict:
    """Build the npz + manifest. Returns the manifest dict.

    ``model_name``/``query_prefix``/``passage_prefix`` select the F9 model cell.
    The default (MiniLM + empty prefixes) reproduces the frozen incumbent index
    byte-for-byte (``content_sha256`` unchanged).

    ``glosses_path`` (PRD connector_gloss_rebaseline_003, D1) opts into the
    connector-side gloss merge: each gloss record in the YAML emits exactly one
    observable ``gloss`` chunk under its ``state_id``. ``None`` (default) keeps
    the incumbent path byte-identical to the frozen anchor, so the
    reproducibility guard stays valid; pass the gloss YAML to build a glossed
    cell to ``glossed/<cell>/``."""

    catalog_path = Path(catalog_path) if catalog_path else catalog_dir() / CATALOG_FILENAME
    targets_path = Path(targets_path) if targets_path else configs_dir() / RETRIEVAL_TARGETS_FILENAME
    index_dir = Path(index_dir) if index_dir else catalog_dir()
    index_dir.mkdir(parents=True, exist_ok=True)

    # --- inputs ---
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    observable_sids, methodological_sids, _ = load_state_classification(targets_path)
    if len(observable_sids) != 26 or len(methodological_sids) != 5:
        raise RuntimeError(
            f"Unexpected state classification: observable={len(observable_sids)} "
            f"methodological={len(methodological_sids)} (expected 26 / 5)."
        )
    observable_set = set(observable_sids)

    entries_by_state = {e.get("state_id"): e for e in catalog.get("entries", [])}

    # --- model ---
    tokenizer = load_tokenizer(model_name)
    model = load_model(model_name)
    model_files = model_file_hashes(model_name)
    msha = compute_model_sha(model_files)
    st_max_len = int(getattr(model, "max_seq_length", 0) or 0)

    # --- chunk ---
    chunk_texts: list[str] = []
    chunk_state_id: list[str] = []
    chunk_entry_id: list[str] = []
    chunk_source_record_id: list[str] = []
    chunk_kind: list[str] = []

    kind_counts: dict[str, int] = {}

    def add_chunks(text: str, *, state_id: str, entry_id: str, source_record_id: str, kind: str) -> None:
        for piece in chunk_text(text, tokenizer, max_tokens=max_tokens, target_tokens=target_tokens):
            chunk_texts.append(piece)
            chunk_state_id.append(state_id)
            chunk_entry_id.append(entry_id)
            chunk_source_record_id.append(source_record_id)
            chunk_kind.append(kind)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

    for sid in observable_sids:  # F1: observable only
        entry = entries_by_state.get(sid)
        if entry is None:
            raise RuntimeError(f"Observable state {sid} missing from catalog entries.")
        entry_id = entry.get("entry_id", "")
        # F2: state_summary (clean text), tagged as the state summary record.
        add_chunks(
            entry.get("state_summary", ""),
            state_id=sid,
            entry_id=entry_id,
            source_record_id=sid,
            kind="state_summary",
        )
        # F2: each linked_records[].text (clean text, NOT embed_text).
        for rec in entry.get("linked_records") or []:
            rid = rec.get("id", "")
            rkind = rec.get("kind", "")
            rtext = rec.get("text", "")
            if not (rid and rkind and _normalize(rtext)):
                continue
            add_chunks(rtext, state_id=sid, entry_id=entry_id, source_record_id=rid, kind=rkind)

    # --- gloss merge (PRD connector_gloss_rebaseline_003, D1/F1) ---
    # Connector-side artifact merged at build (like retrieval_targets). Each gloss
    # record emits exactly ONE observable chunk of gloss_text under its state_id
    # (kind=gloss). Opt-in via glosses_path: None keeps the incumbent path
    # byte-identical to the frozen anchor (the reproducibility guard stays valid).
    gloss_provenance: dict = {}
    if glosses_path is not None:
        gpath = Path(glosses_path)
        glosses = load_yaml(gpath)
        gloss_provenance = {
            "glosses_path": str(gpath),
            "glosses_sha256": sha256_file(gpath),
            "glosses_version": str(glosses.get("gloss_set_version", "")),
        }
        for g in glosses.get("glosses") or []:
            gsid = g.get("state_id", "")
            gtext = _normalize(g.get("gloss_text", ""))
            if not (gsid and gtext):
                continue
            # F4: a gloss attaches to an already-observable state; methodological
            # states never gain a gloss (all 3 targets are observable).
            if gsid not in observable_set:
                raise RuntimeError(
                    f"gloss state {gsid} is not an observable indexed state "
                    f"(methodological states never gain a gloss)."
                )
            gentry = entries_by_state.get(gsid)
            if gentry is None:
                raise RuntimeError(f"gloss state {gsid} missing from catalog entries.")
            # Exactly one gloss chunk per state (F4: chunk_count rises by 3, 313->316).
            chunk_texts.append(gtext)
            chunk_state_id.append(gsid)
            chunk_entry_id.append(gentry.get("entry_id", ""))
            chunk_source_record_id.append(f"gloss_{gsid}")
            chunk_kind.append("gloss")
            kind_counts["gloss"] = kind_counts.get("gloss", 0) + 1

    # D2: validate every emitted chunk kind is admitted (catches a malformed
    # config; admits `gloss` for the glossed merge above).
    bad = sorted({k for k in chunk_kind if k not in ACCEPTED_KINDS})
    if bad:
        raise RuntimeError(f"unaccepted chunk kind(s): {bad} (accepted: {ACCEPTED_KINDS})")

    if not chunk_texts:
        raise RuntimeError("No chunks produced; catalog/targets mismatch.")

    # --- embed (L2-normalized, float32, deterministic) ---
    # F6: passage_prefix applies to the ENCODE INPUT ONLY -- ``chunk_text`` (below)
    # stays the raw text so retrieval_reasons/provenance and the stored corpus are
    # unaffected. Empty prefix (MiniLM) -> encode input unchanged -> vectors
    # byte-identical -> content_sha256 unchanged.
    encode_input = chunk_texts
    if passage_prefix:
        encode_input = [passage_prefix + t for t in chunk_texts]
    vectors = model.encode(
        encode_input,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    ).astype(np.float32)

    dim = int(vectors.shape[1])
    n_chunks = int(vectors.shape[0])

    arrays = {
        "vectors": np.ascontiguousarray(vectors),
        "chunk_id": np.array([f"chk_{i:06d}" for i in range(n_chunks)]),
        "state_id": np.array(chunk_state_id),
        "entry_id": np.array(chunk_entry_id),
        "source_record_id": np.array(chunk_source_record_id),
        "kind": np.array(chunk_kind),
        "chunk_text": np.array(chunk_texts),
    }

    # --- write npz (arrays only; file bytes are intentionally NOT the determinism check) ---
    npz_path = index_dir / INDEX_FILENAME
    np.savez(npz_path, **arrays)

    # --- hashes for manifest ---
    catalog_sha = sha256_file(catalog_path)
    targets_sha = sha256_file(targets_path)
    csha = content_sha256(arrays)

    manifest_stable = {
        "index_version": "1",
        "model_name": model_name,
        "query_prefix": query_prefix,
        "passage_prefix": passage_prefix,
        "model_sha": msha,
        "model_files": model_files,
        "model_sha_recipe": (
            "sha256(sorted-concat of (filename+sha256hex) pairs over "
            "{model.safetensors, tokenizer.json, config.json})"
        ),
        "sentence_transformers_version": str(_pkg_version("sentence_transformers")),
        "transformers_version": str(_pkg_version("transformers")),
        "torch_version": str(torch.__version__),
        "numpy_version": str(np.__version__),
        "dim": dim,
        "tokenizer_max_len": st_max_len,
        "chunk_params": {
            "max_tokens": max_tokens,
            "target_tokens": target_tokens,
            "measurement": "len(tokenizer.encode(text, add_special_tokens=True))",
        },
        "chunk_count": n_chunks,
        "observable_state_count": len(observable_sids),
        "methodological_state_count_in_index": 0,
        "observable_state_ids": observable_sids,
        "excluded_methodological_state_ids": methodological_sids,
        "catalog_path": str(catalog_path),
        "catalog_sha256": catalog_sha,
        "retrieval_targets_path": str(targets_path),
        "retrieval_targets_sha256": targets_sha,
        "kind_counts": dict(sorted(kind_counts.items())),
        "arrays": ["vectors", "chunk_id", "state_id", "entry_id", "source_record_id", "kind", "chunk_text"],
        "content_sha256": csha,
    }
    # D1: gloss provenance (path/sha/version) recorded only on a glossed build;
    # absent on the default build so manifest_stable (and determinism_sha256) is
    # byte-identical to the frozen incumbent anchor.
    manifest_stable.update(gloss_provenance)
    dsha = determinism_sha256(arrays, manifest_stable)
    manifest = dict(manifest_stable)
    manifest["determinism_sha256"] = dsha
    manifest["build_utc"] = now_utc_iso()

    manifest_path = index_dir / MANIFEST_FILENAME
    manifest_path.write_text(dump_json(manifest), encoding="utf-8")

    return manifest


def _pkg_version(pkg: str) -> str:
    try:
        import importlib.metadata as md
        return md.version(pkg)
    except Exception:
        mod = __import__(pkg)
        return getattr(mod, "__version__", "unknown")


def ensure_index(
    *,
    catalog_path: Path | None = None,
    targets_path: Path | None = None,
    index_dir: Path | None = None,
) -> bool:
    """Build the index only if the npz or manifest is absent. Returns True if built.

    **Not on the serving path — D3 — and it has no call sites at all.**
    Retrieval *did* call this. The 2026-07-06 config freeze removed the call and its
    now-orphaned import (`eval_assets/freeze_arc/apply_freeze_wiring.sh`, steps ii-iii).
    The serving path now **reads** the frozen index and fails loudly if the npz/manifest is
    absent, rather than reconstructing it. As of 2026-07-17 nothing in the repo calls this
    function — verified by grep, not assumed.
    **Do not restore the call.** A serving path that can silently rebuild its own index can
    silently change what it serves, which is precisely what D1's freeze forbids: the failure
    would be invisible, because a rebuilt index still answers.
    """
    index_dir = Path(index_dir) if index_dir else catalog_dir()
    npz = index_dir / INDEX_FILENAME
    manifest = index_dir / MANIFEST_FILENAME
    if npz.exists() and manifest.exists():
        return False
    build_index(catalog_path=catalog_path, targets_path=targets_path, index_dir=index_dir)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Build the connector embedding index.")
    ap.add_argument("--catalog", default=None, help="Path to enriched catalog JSON (defaults to catalog dir).")
    ap.add_argument("--targets", default=None, help="Path to retrieval_targets.v1.yaml (defaults to configs dir).")
    ap.add_argument("--index-dir", default=None, help="Output dir for npz + manifest (defaults to catalog dir).")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    ap.add_argument(
        "--model",
        default=DEFAULT_CELL,
        choices=sorted(CELL_REGISTRY),
        help="Model cell (F9): exact repo id + retrieval prefixes baked in. Default: minilm.",
    )
    ap.add_argument(
        "--query-prefix",
        default=None,
        help="Override the cell's query_prefix (default: the cell registry value).",
    )
    ap.add_argument(
        "--passage-prefix",
        default=None,
        help="Override the cell's passage_prefix (default: the cell registry value).",
    )
    ap.add_argument(
        "--glosses",
        default=None,
        help="Path to glosses YAML to merge (glossed build, PRD connector_gloss_rebaseline_003). "
             "Default: no gloss merge (incumbent-reproducible, content_sha256 unchanged).",
    )
    args = ap.parse_args(argv)

    cell = CELL_REGISTRY[args.model]
    query_prefix = args.query_prefix if args.query_prefix is not None else cell["query_prefix"]
    passage_prefix = args.passage_prefix if args.passage_prefix is not None else cell["passage_prefix"]

    manifest = build_index(
        catalog_path=Path(args.catalog) if args.catalog else None,
        targets_path=Path(args.targets) if args.targets else None,
        index_dir=Path(args.index_dir) if args.index_dir else None,
        max_tokens=args.max_tokens,
        target_tokens=args.target_tokens,
        model_name=cell["model_name"],
        query_prefix=query_prefix,
        passage_prefix=passage_prefix,
        glosses_path=Path(args.glosses) if args.glosses else None,
    )
    print(f"index_dir: {args.index_dir or str(catalog_dir())}")
    print(f"model: {manifest['model_name']}")
    print(f"query_prefix: {manifest['query_prefix']!r}  passage_prefix: {manifest['passage_prefix']!r}")
    print(f"model_sha: {manifest['model_sha']}")
    print(f"dim: {manifest['dim']}  chunks: {manifest['chunk_count']}")
    print(
        f"observable: {manifest['observable_state_count']}  "
        f"methodological_in_index: {manifest['methodological_state_count_in_index']}"
    )
    if "glosses_sha256" in manifest:
        print(
            f"glosses: {manifest['glosses_version']}  sha256: {manifest['glosses_sha256']}  "
            f"gloss kind_count: {manifest['kind_counts'].get('gloss', 0)}"
        )
    print(f"content_sha256: {manifest['content_sha256']}")
    print(f"determinism_sha256: {manifest['determinism_sha256']}")


if __name__ == "__main__":
    main()
