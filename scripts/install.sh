#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${DEEPRESEARCH_PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python 3.10+ is required; set DEEPRESEARCH_PYTHON to its executable." >&2
  exit 1
fi
"$python_bin" -c 'import sys; sys.exit("Python >=3.10 required") if sys.version_info < (3, 10) else None'

"$python_bin" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --upgrade build
.venv/bin/python scripts/build_npm_package.py

shopt -s nullglob
packages=(dist/*.tgz)
if (( ${#packages[@]} != 1 )); then
  echo "Expected exactly one npm package in dist/, found ${#packages[@]}." >&2
  exit 1
fi
npm install --global "${packages[0]}"
deepresearch --help
