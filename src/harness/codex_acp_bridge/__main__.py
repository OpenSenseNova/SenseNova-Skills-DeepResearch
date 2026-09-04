"""Run the Codex App Server bridge as an ACP agent over stdio."""

from __future__ import annotations

import argparse
import asyncio

import acp

from .agent import CodexAcpBridgeAgent
from .app_server import CodexAppServerClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deepresearch-codex-acp-bridge")
    parser.add_argument("--codex-command", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--model")
    parser.add_argument("--codex-home")
    return parser


async def _run(args: argparse.Namespace) -> None:
    app_server = CodexAppServerClient(
        args.codex_command,
        profile=args.profile,
        codex_home=args.codex_home,
    )
    await app_server.start()
    try:
        await app_server.initialize()
        await acp.run_agent(
            CodexAcpBridgeAgent(app_server, model=args.model),
            use_unstable_protocol=True,
            stdio_buffer_limit_bytes=16_000_000,
        )
    finally:
        await app_server.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
