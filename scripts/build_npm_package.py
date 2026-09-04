#!/usr/bin/env python3
"""Build the Python wheel and the npm wrapper tarball as one versioned release."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NPM_ROOT = ROOT / "npm"
DIST = ROOT / "dist"
VENDOR = NPM_ROOT / "vendor"


def run(command: list[str], *, cwd: Path = ROOT, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )
    return completed.stdout if capture else ""


def python_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("cannot find project version in pyproject.toml")
    version = match.group(1)
    package_text = (ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
    package_match = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', package_text, re.MULTILINE)
    if package_match is None or package_match.group(1) != version:
        found = package_match.group(1) if package_match else "missing"
        raise RuntimeError(
            f"version mismatch: pyproject.toml={version}, deepresearch_cli={found}"
        )
    return version


def npm_manifest() -> dict[str, object]:
    return json.loads((NPM_ROOT / "package.json").read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    version = python_version()
    manifest = npm_manifest()
    if manifest.get("version") != version:
        raise RuntimeError(
            f"version mismatch: Python={version}, npm={manifest.get('version')}"
        )
    if shutil.which("node") is None or shutil.which("npm") is None:
        raise RuntimeError("Node.js and npm are required to build the npm package")

    DIST.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deepresearch-package-build-") as temp:
        build_cwd = Path(temp)
        try:
            run([sys.executable, "-m", "build", "--version"], cwd=build_cwd)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "Python package 'build' is required; run: "
                "python -m pip install build"
            ) from exc
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--outdir",
                str(DIST),
                str(ROOT),
            ],
            cwd=build_cwd,
        )
    expected = DIST / f"deepresearch_cli-{version}-py3-none-any.whl"
    source = DIST / f"deepresearch_cli-{version}.tar.gz"
    if not expected.is_file():
        raise RuntimeError(f"wheel was not produced at {expected}")
    if not source.is_file():
        raise RuntimeError(f"source distribution was not produced at {source}")

    VENDOR.mkdir(parents=True, exist_ok=True)
    for existing in VENDOR.glob("*.whl"):
        existing.unlink()
    bundled = VENDOR / expected.name
    shutil.copy2(expected, bundled)

    run(["node", "--test", "tests/*.test.js"], cwd=NPM_ROOT)
    dry_run = run(["npm", "pack", "--dry-run", "--json"], cwd=NPM_ROOT, capture=True)
    dry_run_value = json.loads(dry_run)
    files = {
        item["path"]
        for item in dry_run_value[0].get("files", [])
        if isinstance(item, dict) and "path" in item
    }
    if f"vendor/{expected.name}" not in files:
        raise RuntimeError("npm dry run did not include the bundled Python wheel")
    if any(path.startswith(("tests/", "node_modules/", "dashboard/")) for path in files):
        raise RuntimeError("npm dry run included development-only files")

    packed = run(
        ["npm", "pack", "--json", "--pack-destination", str(DIST)],
        cwd=NPM_ROOT,
        capture=True,
    )
    packed_value = json.loads(packed)
    filename = packed_value[0]["filename"]
    output = DIST / filename
    checksum_file = DIST / "SHA256SUMS"
    checksum_file.write_text(
        "".join(
            f"{sha256(path)}  {path.name}\n"
            for path in (expected, source, output)
        ),
        encoding="utf-8",
    )
    print(f"npm package: {output}")
    print(f"bundled wheel: {bundled}")
    print(f"checksums: {checksum_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
