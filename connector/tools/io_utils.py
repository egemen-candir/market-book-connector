"""Deterministic I/O utilities for the Market Book connector module.

Only stdlib + PyYAML are used.  All serialisation is deterministic
(sort_keys=True, fixed indentation).
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# text / bytes helpers
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    Path(path).write_bytes(data)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def dump_yaml(obj: Any) -> str:
    return yaml.dump(obj, sort_keys=True, allow_unicode=True, default_flow_style=False)


def write_yaml(path: Path, obj: Any) -> None:
    write_text(path, dump_yaml(obj))


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def write_json(path: Path, obj: Any) -> None:
    write_text(path, dump_json(obj))


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    data = Path(path).read_bytes()
    return sha256_bytes(data)


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = {"a": 2, "c": [3, 1]}
        p = Path(tmp) / "test.json"
        write_json(p, d)
        assert load_json(p) == d

        p2 = Path(tmp) / "test.yaml"
        write_yaml(p2, d)
        assert load_yaml(p2) == d

        print("sha256 of 'hello':", sha256_bytes(b"hello"))
        print("now_utc_iso():", now_utc_iso())
        print("dump_yaml:", dump_yaml(d))
        print("dump_json:", dump_json(d))
        print("All io_utils checks passed.")


if __name__ == "__main__":
    main()
