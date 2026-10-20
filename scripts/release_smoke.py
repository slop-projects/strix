#!/usr/bin/env python3
"""Release & install smoke check.

Confirms that the artifacts Strix tells users to install actually exist, and
that the URLs/tags documented in the repo still point at what the release
pipeline publishes.

Checks
------
Network (default mode)
  - the install one-liner (https://strix.ai/install) resolves and looks like the installer
  - every per-platform release asset exists for the latest (or ``--version``) release
  - the sandbox container image exists, with linux/amd64 and linux/arm64 manifests

Repo consistency (always, also with ``--offline``)
  - every tagged ``ghcr.io/usestrix/strix-sandbox:<tag>`` reference in README/docs/installer
    matches the default in ``strix/config/settings.py``
  - the installer URL is documented
  - the supported target list agrees across the installer, the self-updater, and CI

Exit code 0 when everything passes, 1 otherwise. Network mode is intended to run
manually or from a release/scheduled CI job; ``--offline`` is safe for any CI.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests


INSTALL_URL = "https://strix.ai/install"
GHCR_REGISTRY = "https://ghcr.io"
GHCR_IMAGE = "usestrix/strix-sandbox"
GITHUB_REPO = "usestrix/strix"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
RELEASE_DOWNLOAD_BASE = f"https://github.com/{GITHUB_REPO}/releases/download"

# Left-to-right order is the documented platform order; the set is compared, so
# order only matters for stable output.
SUPPORTED_TARGETS = (
    "linux-x86_64",
    "linux-arm64",
    "macos-x86_64",
    "macos-arm64",
    "windows-x86_64",
)

REQUEST_TIMEOUT_SECONDS = 30

_TARGET_RE = re.compile(r"\b(?:linux|macos|windows)-(?:x86_64|arm64)\b")
_IMAGE_TAG_RE = re.compile(r"ghcr\.io/usestrix/strix-sandbox:([A-Za-z0-9._-]+)")
_SETTINGS_IMAGE_RE = re.compile(
    r'image:\s*str\s*=\s*Field\(\s*default="(ghcr\.io/usestrix/strix-sandbox:[^"]+)"'
)
_INSTALL_SH_IMAGE_RE = re.compile(
    r'^STRIX_IMAGE="(ghcr\.io/usestrix/strix-sandbox:[^"]+)"', re.MULTILINE
)

# Files whose documented URLs/tags must match the published artifacts.
_CONSISTENCY_FILES = ("README.md", "AGENTS.md")


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passes.append(message)
        print(f"  \u2713 {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"  \u2717 {message}")

    def check(self, condition: bool, message: str) -> bool:
        (self.ok if condition else self.fail)(message)
        return condition


# --------------------------------------------------------------------------- #
# Repo consistency (offline)
# --------------------------------------------------------------------------- #


def _read(repo_root: Path, relative: str) -> str:
    return (repo_root / relative).read_text(encoding="utf-8")


def _iter_doc_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for name in _CONSISTENCY_FILES:
        candidate = repo_root / name
        if candidate.is_file():
            files.append(candidate)
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(p for p in docs_dir.rglob("*") if p.suffix in {".md", ".mdx"}))
    return files


def default_sandbox_image(repo_root: Path) -> str:
    """The image/tag the runtime defaults to, read from the settings source."""
    match = _SETTINGS_IMAGE_RE.search(_read(repo_root, "strix/config/settings.py"))
    if match is None:
        raise RuntimeError("could not find the default sandbox image in settings.py")
    return match.group(1)


def documented_image_tags(repo_root: Path) -> dict[str, list[str]]:
    """Map of documented ``ghcr.io/...:<tag>`` tag -> files that mention it."""
    found: dict[str, list[str]] = {}
    files = [*_iter_doc_files(repo_root), repo_root / "scripts/install.sh"]
    for path in files:
        if not path.is_file():
            continue
        for tag in _IMAGE_TAG_RE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(tag, []).append(str(path.relative_to(repo_root)))
    return found


def installer_image(repo_root: Path) -> str | None:
    match = _INSTALL_SH_IMAGE_RE.search(_read(repo_root, "scripts/install.sh"))
    return match.group(1) if match else None


def target_sets(repo_root: Path) -> dict[str, set[str]]:
    """The supported target set as declared by each pipeline file."""
    sources = {
        "scripts/install.sh": repo_root / "scripts/install.sh",
        "strix/interface/update_check.py": repo_root / "strix/interface/update_check.py",
        ".github/workflows/build-release.yml": repo_root / ".github/workflows/build-release.yml",
    }
    return {
        name: set(_TARGET_RE.findall(path.read_text(encoding="utf-8")))
        for name, path in sources.items()
        if path.is_file()
    }


def check_repo_consistency(repo_root: Path, report: Report) -> None:
    print("Repo consistency")

    expected_image = default_sandbox_image(repo_root)
    expected_tag = expected_image.split(":", 1)[1]
    report.ok(f"default sandbox image is {expected_image}")

    for tag, files in sorted(documented_image_tags(repo_root).items()):
        report.check(
            tag == expected_tag,
            f"sandbox image tag '{tag}' in {', '.join(files)} matches default '{expected_tag}'",
        )

    install_image = installer_image(repo_root)
    report.check(
        install_image == expected_image,
        f"installer pins the same image ({install_image})",
    )

    readme = _read(repo_root, "README.md")
    report.check(INSTALL_URL in readme, f"README documents {INSTALL_URL}")

    doc_hits = [
        path
        for path in _iter_doc_files(repo_root)
        if INSTALL_URL in path.read_text(encoding="utf-8")
    ]
    report.check(bool(doc_hits), f"{INSTALL_URL} appears in the docs")

    for name, targets in target_sets(repo_root).items():
        missing = set(SUPPORTED_TARGETS) - targets
        extra = targets - set(SUPPORTED_TARGETS)
        report.check(
            not missing and not extra,
            f"{name} targets match the supported set"
            + (f" (missing={sorted(missing)}, extra={sorted(extra)})" if missing or extra else ""),
        )


# --------------------------------------------------------------------------- #
# Network checks
# --------------------------------------------------------------------------- #


def _github_headers() -> dict[str, str]:
    """Headers for GitHub API calls. A ``GITHUB_TOKEN`` lifts the anonymous rate limit."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _latest_release(session: requests.Session, version: str | None) -> tuple[str, set[str]]:
    if version:
        response = session.get(
            f"{GITHUB_API}/releases/tags/v{version}",
            headers=_github_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    else:
        response = session.get(
            f"{GITHUB_API}/releases/latest",
            headers=_github_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    payload = response.json()
    version = str(payload["tag_name"]).lstrip("v")
    assets = {asset["name"] for asset in payload.get("assets", [])}
    return version, assets


def check_installer_url(session: requests.Session, report: Report) -> None:
    print("Installer URL")
    try:
        response = session.get(INSTALL_URL, allow_redirects=True, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        report.fail(f"GET {INSTALL_URL} failed: {exc}")
        return
    report.check(response.status_code == 200, f"GET {INSTALL_URL} -> {response.status_code}")
    body = response.text
    report.check(
        "usestrix/strix" in body,
        f"{INSTALL_URL} serves the installer script",
    )


def check_release_assets(session: requests.Session, report: Report, version: str | None) -> None:
    print("Release assets")
    try:
        resolved_version, assets = _latest_release(session, version)
    except requests.RequestException as exc:
        report.fail(f"could not fetch release metadata: {exc}")
        return
    report.ok(f"release v{resolved_version} has {len(assets)} assets")

    for target in SUPPORTED_TARGETS:
        archive_ext = ".zip" if target.startswith("windows") else ".tar.gz"
        filename = f"strix-{resolved_version}-{target}{archive_ext}"
        if not report.check(filename in assets, f"asset {filename} is published"):
            continue
        url = f"{RELEASE_DOWNLOAD_BASE}/v{resolved_version}/{filename}"
        try:
            head = session.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            report.fail(f"HEAD {url} failed: {exc}")
            continue
        report.check(
            head.status_code == 200, f"asset {filename} downloads (HTTP {head.status_code})"
        )


def _ghcr_pull_token(session: requests.Session) -> str:
    response = session.get(
        f"{GHCR_REGISTRY}/token",
        params={"scope": f"repository:{GHCR_IMAGE}:pull", "service": "ghcr.io"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return str(response.json()["token"])


def check_sandbox_image(repo_root: Path, session: requests.Session, report: Report) -> None:
    print("Sandbox image")
    image = default_sandbox_image(repo_root)
    repository, _, tag = image.partition(":")
    image_path = repository.removeprefix("ghcr.io/")
    try:
        token = _ghcr_pull_token(session)
        response = session.get(
            f"{GHCR_REGISTRY}/v2/{image_path}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        report.fail(f"could not query GHCR for {image}: {exc}")
        return

    if not report.check(
        response.status_code == 200, f"image {image} exists (HTTP {response.status_code})"
    ):
        return

    try:
        manifest = response.json()
    except ValueError:
        report.ok(f"image {image} manifest is present")
        return

    manifests = manifest.get("manifests")
    if not manifests:
        # A single-platform image: its own config lists os/architecture.
        report.ok(f"image {image} is a single-platform manifest")
        return

    platforms = {
        (entry.get("platform", {}).get("os"), entry.get("platform", {}).get("architecture"))
        for entry in manifests
    }
    for os_name, arch in (("linux", "amd64"), ("linux", "arm64")):
        report.check(
            (os_name, arch) in platforms,
            f"image {image} publishes {os_name}/{arch}",
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip network checks and only verify repo consistency",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="check a specific released version instead of the latest",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="path to the repository root (default: parent of this script)",
    )
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parent.parent
    )
    if not (repo_root / "pyproject.toml").is_file():
        print(f"error: {repo_root} does not look like the Strix repo root", file=sys.stderr)
        return 1

    report = Report()
    check_repo_consistency(repo_root, report)

    if args.offline:
        print("Network checks skipped (--offline)")
    else:
        with requests.Session() as session:
            session.headers["User-Agent"] = "strix-release-smoke"
            check_installer_url(session, report)
            check_release_assets(session, report, args.version)
            check_sandbox_image(repo_root, session, report)

    print()
    if report.failures:
        print(f"FAILED: {len(report.failures)} check(s) failed")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {len(report.passes)} check(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
