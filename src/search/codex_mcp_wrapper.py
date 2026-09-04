"""Validated stdio entrypoint for Codex-launched Search MCP processes."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .mcp_server import main as mcp_main


def _log(message: str) -> None:
    path = os.environ.get("DEEPRESEARCH_SEARCH_STARTUP_LOG")
    if path:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.open("a", encoding="utf-8").write(message + "\n")
        except OSError:
            pass


def main() -> None:
    _log(
        "MCP process starting: "
        f"pid={os.getpid()} cwd={os.getcwd()} argv={sys.argv!r}"
    )
    lease = os.environ.get("DEEPRESEARCH_SEARCH_LEASE_FILE")
    if lease and not Path(lease).is_file():
        _log(f"lease file does not exist: {lease}")
        raise SystemExit(2)
    # Do not probe the coordinator here.  The MCP process receives a
    # namespace-scoped token, while the coordinator health endpoint requires
    # the root token.  Probing with the scoped token only produces a misleading
    # 401 on stderr before Codex performs the actual MCP initialize handshake.
    try:
        mcp_main()
    except BaseException as exc:
        _log(f"MCP process failed: {type(exc).__name__}: {exc}")
        raise
    else:
        _log("MCP process returned normally")


if __name__ == "__main__":
    main()
