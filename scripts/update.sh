#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree has uncommitted changes; refusing to overwrite them." >&2
  exit 1
fi

echo "Updating the local SenseNova-Skills-DeepResearch installation from this checkout."
echo "Pull or switch branches yourself before running this script; it does not modify Git history."
exec "$repo_root/scripts/install.sh"
