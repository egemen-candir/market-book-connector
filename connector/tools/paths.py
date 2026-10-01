"""Path resolution for the Market Book connector module.

All paths are derived from the location of this file so that no
hard-coded paths exist anywhere in the connector.
"""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONNECTOR_ROOT = _PROJECT_ROOT / "connector"


def project_root() -> Path:
    return _PROJECT_ROOT


def connector_root() -> Path:
    return _CONNECTOR_ROOT


def entries_dir() -> Path:
    return _PROJECT_ROOT / "research_loop" / "market_book" / "entries"


def catalog_dir() -> Path:
    return _CONNECTOR_ROOT / "catalog"


def runs_dir() -> Path:
    return _CONNECTOR_ROOT / "runs"


def vocab_dir() -> Path:
    return _CONNECTOR_ROOT / "vocab"


def schemas_dir() -> Path:
    return _CONNECTOR_ROOT / "schemas"


def prompts_dir() -> Path:
    return _CONNECTOR_ROOT / "prompts"


def configs_dir() -> Path:
    return _CONNECTOR_ROOT / "configs"


def golden_outputs_dir(scenario: str) -> Path:
    return _CONNECTOR_ROOT / "examples" / "golden_mock_outputs" / scenario


def golden_inputs_dir() -> Path:
    return _CONNECTOR_ROOT / "examples" / "golden_inputs"


def ensure_dir(path: Path) -> Path:
    """Create directory (and parents) if it does not already exist."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def main():
    for name, fn in [
        ("project_root", project_root),
        ("connector_root", connector_root),
        ("entries_dir", entries_dir),
        ("catalog_dir", catalog_dir),
        ("runs_dir", runs_dir),
        ("vocab_dir", vocab_dir),
        ("schemas_dir", schemas_dir),
        ("prompts_dir", prompts_dir),
        ("configs_dir", configs_dir),
        ("golden_inputs_dir", golden_inputs_dir),
        ("golden_outputs_dir (risk_on_breadth_weak)", lambda: golden_outputs_dir("risk_on_breadth_weak")),
    ]:
        print(f"{name}: {fn()}")


if __name__ == "__main__":
    main()
