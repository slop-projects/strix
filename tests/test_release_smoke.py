"""Tests for the release/install smoke check (scripts/release_smoke.py).

The network checks are exercised by ``make release-smoke``; here we cover the
repo-consistency logic, which must catch documentation drifting away from the
artifacts the release pipeline actually publishes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    import pytest


def _load_module() -> Any:
    script = Path(__file__).resolve().parents[1] / "scripts/release_smoke.py"
    spec = importlib.util.spec_from_file_location("release_smoke", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve ``cls.__module__``.
    sys.modules["release_smoke"] = module
    spec.loader.exec_module(module)
    return module


RELEASE_SMOKE = _load_module()
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_image_matches_installer_and_docs() -> None:
    image = RELEASE_SMOKE.default_sandbox_image(REPO_ROOT)
    assert image == RELEASE_SMOKE.installer_image(REPO_ROOT)
    # Every tagged reference in the docs resolves to the same image.
    assert set(RELEASE_SMOKE.documented_image_tags(REPO_ROOT)) == {image.split(":", 1)[1]}


def test_repo_consistency_passes_on_this_repo() -> None:
    report = RELEASE_SMOKE.Report()
    RELEASE_SMOKE.check_repo_consistency(REPO_ROOT, report)
    assert report.failures == []


def test_offline_main_succeeds_on_this_repo(capsys: pytest.CaptureFixture[str]) -> None:
    result = RELEASE_SMOKE.main(["--offline", "--repo-root", str(REPO_ROOT)])
    assert result == 0
    assert "FAILED" not in capsys.readouterr().out


def _write_minimal_repo(root: Path, *, settings_tag: str, doc_tag: str) -> None:
    (root / "strix/config").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "docs/advanced").mkdir(parents=True, exist_ok=True)
    (root / "strix/interface").mkdir(parents=True, exist_ok=True)
    (root / ".github/workflows").mkdir(parents=True, exist_ok=True)

    (root / "strix/config/settings.py").write_text(
        "class RuntimeSettings:\n"
        "    image: str = Field(\n"
        f'        default="ghcr.io/usestrix/strix-sandbox:{settings_tag}",\n'
        "    )\n",
        encoding="utf-8",
    )
    (root / "scripts/install.sh").write_text(
        f'STRIX_IMAGE="ghcr.io/usestrix/strix-sandbox:{settings_tag}"\n'
        'case "$combo" in\n'
        "  linux-x86_64|linux-arm64|macos-x86_64|macos-arm64|windows-x86_64)\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("curl -sSL https://strix.ai/install | bash\n", encoding="utf-8")
    (root / "docs/advanced/config.mdx").write_text(
        f"curl -sSL https://strix.ai/install | bash\nghcr.io/usestrix/strix-sandbox:{doc_tag}\n",
        encoding="utf-8",
    )
    targets = [
        "linux-x86_64",
        "linux-arm64",
        "macos-x86_64",
        "macos-arm64",
        "windows-x86_64",
    ]
    target_lines = "\n".join(f'        "{target}",' for target in targets)
    (root / "strix/interface/update_check.py").write_text(
        "supported = {\n" + target_lines + "\n}\n", encoding="utf-8"
    )
    matrix = "\n".join(f"          - target: {target}" for target in targets)
    (root / ".github/workflows/build-release.yml").write_text(
        "matrix:\n  include:\n" + matrix + "\n", encoding="utf-8"
    )
    # Presence check only; main() requires a pyproject.toml at the repo root.
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")


def test_detects_documentation_image_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_minimal_repo(tmp_path, settings_tag="1.3.0", doc_tag="1.3.0")

    # A fully consistent tree passes...
    assert RELEASE_SMOKE.main(["--offline", "--repo-root", str(tmp_path)]) == 0

    _write_minimal_repo(tmp_path, settings_tag="1.3.0", doc_tag="9.9.9")
    capsys.readouterr()  # drop the first run's output
    result = RELEASE_SMOKE.main(["--offline", "--repo-root", str(tmp_path)])

    assert result == 1
    assert "'9.9.9'" in capsys.readouterr().out


def test_detects_target_list_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_repo(tmp_path, settings_tag="1.3.0", doc_tag="1.3.0")
    # Drop a platform from the installer: the smoke check must notice.
    installer = tmp_path / "scripts/install.sh"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace("|windows-x86_64", ""),
        encoding="utf-8",
    )

    result = RELEASE_SMOKE.main(["--offline", "--repo-root", str(tmp_path)])

    assert result == 1
    assert "scripts/install.sh targets match" in capsys.readouterr().out
