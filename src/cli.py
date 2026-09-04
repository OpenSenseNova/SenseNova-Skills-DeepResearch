from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Optional, Sequence

from deepresearch_cli.config import RunRequest
from deepresearch_cli.config.paths import builtin_docs_dir
from deepresearch_cli.driver import ExecutionSessionConfig, projection_summary
from deepresearch_cli.harness.registry import PRODUCTION_BACKENDS
from deepresearch_cli.progress import TerminalProgressReporter
from deepresearch_cli.search.paths import builtin_search_dir
from deepresearch_cli.search.registry import (
    DomainRegistry,
    ProviderRegistry,
    load_search_environment,
)
from deepresearch_cli.service import WorkflowService


_COMMANDS = {
    "doctor", "status", "resume", "nodes", "node", "sources", "domains",
    "research", "web", "browser", "diagnostics",
}
_REPORT_FORMAT_OPTIONS = (
    ("brief", "简报", "快速给出核心结论，篇幅精简"),
    ("formal_report", "正式报告", "深入分析，并按场景匹配内容模板"),
)


class _RootParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        if values and values[0] not in _COMMANDS and values[0] not in {"-h", "--help"}:
            values.insert(0, "research")
        return super().parse_args(values, namespace)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _provider_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 50:
        raise argparse.ArgumentTypeError("must be between 1 and 50")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runs-dir", type=Path, default=Path("./runs"))
    parser.add_argument("--output-dir", type=Path, default=Path("./output"))
    parser.add_argument("--nodes-dir", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true")


def _add_execution(
    parser: argparse.ArgumentParser, *, harness_required: bool = True
) -> None:
    parser.add_argument(
        "--harness",
        choices=PRODUCTION_BACKENDS,
        required=harness_required,
        default="hermes",
    )
    parser.add_argument("--harness-profile")
    parser.add_argument("--harness-command")
    parser.add_argument("--harness-model")
    parser.add_argument(
        "--harness-timeout-seconds",
        type=_positive_float,
        default=300.0,
        help="minimum Harness-internal model request timeout checked before startup",
    )
    timeout = parser.add_mutually_exclusive_group()
    timeout.add_argument(
        "--node-timeout-seconds",
        type=_positive_float,
        default=900.0,
        help="fallback timeout for agent steps without a workflow timeouts entry",
    )
    timeout.add_argument(
        "--no-node-timeout",
        dest="node_timeout_seconds",
        action="store_const",
        const=None,
        help="disable the agent fallback timeout; workflow agent timeouts still apply",
    )
    parser.add_argument("--max-concurrency", type=_positive_int, default=4)
    parser.add_argument("--progress", choices=["auto", "tools", "off"], default="auto")
    parser.add_argument("--no-search-mcp", dest="search_mcp", action="store_false")
    parser.set_defaults(search_mcp=True)
    parser.add_argument(
        "--search-dir",
        type=Path,
        help="search registry containing .env and one sources/*.yaml file per provider",
    )
    parser.add_argument("--search-provider-python")
    parser.add_argument("--search-provider-limit", type=_provider_limit, default=20)
    camofox = parser.add_mutually_exclusive_group()
    camofox.add_argument(
        "--camofox-fallback",
        dest="camofox_fallback",
        action="store_true",
        help=(
            "enable the code-controlled HTTP-first Camofox fallback in Research "
            "fetch_url (default when the search MCP is enabled)"
        ),
    )
    camofox.add_argument(
        "--no-camofox-fallback",
        dest="camofox_fallback",
        action="store_false",
        help="disable the default Camofox fallback",
    )
    parser.set_defaults(camofox_fallback=True)
    parser.add_argument(
        "--camofox-home",
        type=Path,
        help="CLI-managed Camofox installation directory",
    )
    parser.add_argument(
        "--camofox-base-url",
        help="Camofox REST server URL (default: http://127.0.0.1:9377)",
    )


def _add_report_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report-format",
        choices=[option[0] for option in _REPORT_FORMAT_OPTIONS],
        default=None,
        help=(
            "writing form; when omitted, an interactive CLI asks you to choose "
            "(required explicitly in scripts and other non-interactive environments)"
        ),
    )


def _prompt_report_format(*, input_fn=None, output=None, is_tty=None) -> str:
    """Ask an interactive terminal user to choose the report writing form."""
    input_fn = input if input_fn is None else input_fn
    output = sys.stderr if output is None else output
    is_tty = sys.stdin.isatty() if is_tty is None else is_tty
    if not is_tty:
        raise ValueError(
            "--report-format is required in non-interactive mode; choose "
            "brief or formal_report"
        )

    print("请选择报告形式：", file=output)
    for index, (value, label, description) in enumerate(
        _REPORT_FORMAT_OPTIONS, start=1
    ):
        print(f"  {index}. {label}（{value}）— {description}", file=output)

    choices = {
        str(index): value
        for index, (value, _label, _description) in enumerate(
            _REPORT_FORMAT_OPTIONS, start=1
        )
    }
    choices.update(
        {
            value: value
            for value, _label, _description in _REPORT_FORMAT_OPTIONS
        }
    )
    choice_numbers = [str(index) for index in range(1, len(_REPORT_FORMAT_OPTIONS) + 1)]
    choice_hint = " 或 ".join(choice_numbers)
    while True:
        print(f"请输入 {choice_hint}：", end="", file=output, flush=True)
        try:
            selected = input_fn("").strip()
        except EOFError as exc:
            raise ValueError("report format selection was cancelled") from exc
        if selected in choices:
            return choices[selected]
        print(f"无效选择，请输入 {choice_hint}。", file=output)


def build_parser() -> argparse.ArgumentParser:
    parser = _RootParser(
        prog="deepresearch",
        description="Configuration-first DeepResearch workflow runtime",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    research = commands.add_parser(
        "research", help="run a workflow (this command word is normally omitted)"
    )
    research.add_argument("query")
    research.add_argument("--language", default="zh-CN")
    research.add_argument(
        "--mode",
        choices=["quick", "normal", "heavy"],
        default=None,
        help="node behavior mode; defaults to normal for a custom workflow",
    )
    research.add_argument(
        "--workflow",
        type=Path,
        help="custom workflow topology; may be combined with --mode",
    )
    research.add_argument(
        "--output-format",
        dest="output_format",
        choices=["markdown", "html", "pdf", "docx"],
        default="markdown",
    )
    research.add_argument("--max-steps", type=_nonnegative_int)
    research.add_argument("--run-id", help=argparse.SUPPRESS)
    _add_common(research)
    _add_execution(research)
    _add_report_format(research)

    doctor = commands.add_parser("doctor", help="verify nodes and the Agent runtime")
    _add_common(doctor)
    _add_execution(doctor)

    diagnostics = commands.add_parser(
        "diagnostics", help="show the installed diagnostics guide path"
    )
    diagnostics.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="show a persisted run")
    status.add_argument("run_id")
    _add_common(status)

    resume = commands.add_parser("resume", help="continue a config-workflow run")
    resume.add_argument("run_id")
    resume.add_argument("--max-steps", type=_nonnegative_int)
    _add_common(resume)
    _add_execution(resume)

    nodes = commands.add_parser("nodes", help="discover registered nodes")
    node_commands = nodes.add_subparsers(dest="nodes_command", required=True)
    nodes_list = node_commands.add_parser("list")
    _add_common(nodes_list)
    nodes_describe = node_commands.add_parser("describe")
    nodes_describe.add_argument("node_id")
    _add_common(nodes_describe)

    node = commands.add_parser("node", help="run one registered node")
    node_commands = node.add_subparsers(dest="node_command", required=True)
    node_run = node_commands.add_parser("run")
    node_run.add_argument("node_id")
    node_run.add_argument("--input", action="append", default=[], metavar="PORT=PATH")
    node_run.add_argument("--language", default="zh-CN")
    node_run.add_argument("--query", default="Run the selected capability node")
    _add_common(node_run)
    _add_execution(node_run, harness_required=False)

    sources = commands.add_parser("sources", help="discover configured search sources")
    source_commands = sources.add_subparsers(dest="sources_command", required=True)
    for command_name in ("list", "describe", "init"):
        source_command = source_commands.add_parser(command_name)
        if command_name == "describe":
            source_command.add_argument("source_name")
        source_command.add_argument("--search-dir", type=Path)
        source_command.add_argument("--search-provider-python")
        source_command.add_argument("--json", action="store_true")
        if command_name == "init":
            source_command.add_argument("--force", action="store_true")

    domains = commands.add_parser("domains", help="discover configured search domains")
    domain_commands = domains.add_subparsers(dest="domains_command", required=True)
    for command_name in ("list", "describe"):
        domain_command = domain_commands.add_parser(command_name)
        if command_name == "describe":
            domain_command.add_argument("domain_name")
        domain_command.add_argument("--search-dir", type=Path)
        domain_command.add_argument("--search-provider-python")
        domain_command.add_argument("--json", action="store_true")

    browser = commands.add_parser("browser", help="manage the CLI-owned Camofox browser")
    browser_commands = browser.add_subparsers(dest="browser_command", required=True)
    for command_name in ("setup", "start", "status", "stop"):
        browser_command = browser_commands.add_parser(command_name)
        browser_command.add_argument("--home", type=Path)
        browser_command.add_argument("--base-url")
        browser_command.add_argument("--json", action="store_true")
        if command_name == "setup":
            browser_command.add_argument("--npm-command", default="npm")

    web = commands.add_parser("web", help="start the local research progress console")
    web.add_argument("query", nargs="?", help="start this research immediately")
    web.add_argument("--language", default="zh-CN")
    web.add_argument("--mode", choices=["quick", "normal", "heavy"], default="heavy")
    web.add_argument(
        "--output-format", dest="output_format",
        choices=["markdown", "html", "pdf", "docx"], default="markdown",
    )
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=_port, default=8765)
    _add_common(web)
    _add_execution(web, harness_required=False)
    _add_report_format(web)
    return parser


def _service(args) -> WorkflowService:
    progress = None if getattr(args, "progress", "off") == "off" or getattr(args, "json", False) else TerminalProgressReporter()
    config = ExecutionSessionConfig(
        harness=getattr(args, "harness", "hermes"),
        harness_profile=getattr(args, "harness_profile", None),
        harness_model=getattr(args, "harness_model", None),
        node_timeout_seconds=getattr(args, "node_timeout_seconds", 600.0),
        harness_timeout_seconds=getattr(args, "harness_timeout_seconds", 300.0),
        max_concurrency=getattr(args, "max_concurrency", 4),
        search_mcp_enabled=getattr(args, "search_mcp", True),
        search_dir=getattr(args, "search_dir", None),
        search_provider_python=getattr(args, "search_provider_python", None),
        search_provider_limit=getattr(args, "search_provider_limit", 20),
        camofox_fallback_enabled=(
            getattr(args, "camofox_fallback", True)
            and getattr(args, "search_mcp", True)
        ),
        camofox_home=getattr(args, "camofox_home", None),
        camofox_base_url=getattr(args, "camofox_base_url", None),
    )
    return WorkflowService(
        args.runs_dir,
        config,
        output_dir=args.output_dir,
        node_dirs=getattr(args, "nodes_dir", ()),
        harness_command=getattr(args, "harness_command", None),
        progress_reporter=progress,
    )


def _emit(value, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, list):
        for item in value:
            print(
                item
                if isinstance(item, str)
                else item.get("id") or item.get("provider") or item.get("domain") or item
            )
        return
    for key in ("run_id", "status", "workflow", "output_format"):
        if key in value:
            print(f"{key}: {value[key]}")
    progress = value.get("progress") if isinstance(value, dict) else None
    if isinstance(progress, dict):
        print(
            f"progress: {progress.get('percent', 0)}% · "
            f"{progress.get('phase_label', progress.get('phase', '处理中'))}"
        )
    result = value.get("result") if isinstance(value, dict) else None
    if isinstance(result, dict):
        print(f"result_type: {result['type']}")
        print(f"result_path: {result['path']}")
        if result.get("source_report_path"):
            print(f"source_report_path: {result['source_report_path']}")
    elif isinstance(value, dict):
        for key in sorted(value):
            if key not in {"run_id", "status", "workflow", "output_format", "progress", "result"}:
                print(f"{key}: {value[key]}")


async def _run(args) -> int:
    if args.command == "diagnostics":
        path = builtin_docs_dir() / "diagnostics.md"
        if not path.is_file():
            raise FileNotFoundError(f"diagnostics guide is missing: {path}")
        _emit({"path": str(path)}, as_json=args.json)
        return 0
    if args.command == "browser":
        from deepresearch_cli.camofox_runtime import CamofoxRuntime

        runtime = CamofoxRuntime(home=args.home, base_url=args.base_url)
        if args.browser_command == "setup":
            value = runtime.setup(npm_command=args.npm_command)
        elif args.browser_command == "start":
            value = runtime.start()
        elif args.browser_command == "stop":
            value = runtime.stop()
        else:
            value = runtime.status()
        _emit(value, as_json=args.json)
        return 0
    if args.command in {"sources", "domains"}:
        if args.command == "sources" and args.sources_command == "init":
            from deepresearch_cli.search.paths import initialize_user_search_environment

            path = initialize_user_search_environment(overwrite=args.force)
            _emit(
                {
                    "status": "ready",
                    "path": str(path),
                    "next": "edit this file, then run `deepresearch sources list`",
                },
                as_json=args.json,
            )
            return 0
        search_dir = (args.search_dir or builtin_search_dir()).expanduser().resolve()
        registry = ProviderRegistry(
            search_dir=search_dir,
            python_executable=args.search_provider_python or sys.executable,
            environment=load_search_environment(search_dir),
        )
        if args.command == "sources":
            if args.sources_command == "list":
                value = registry.list_sources()
            else:
                value = registry.describe(args.source_name)
        else:
            domains = DomainRegistry(search_dir=search_dir, source_registry=registry)
            values = domains.list_domains(source_registry=registry)
            if args.domains_command == "list":
                value = values
            else:
                value = next(
                    (item for item in values if item["domain"] == args.domain_name),
                    None,
                )
                if value is None:
                    raise ValueError(f"unsupported search domain: {args.domain_name}")
        _emit(value, as_json=args.json)
        return 0
    service = _service(args)
    if args.command == "doctor":
        _emit(dict(await service.doctor()), as_json=args.json)
        return 0
    if args.command == "nodes":
        registry = service.registry()
        if args.nodes_command == "list":
            value = [
                {"id": item.node_id, "kind": item.kind,
                 "inputs": [port.name for port in item.inputs],
                 "outputs": [port.name for port in item.outputs]}
                for item in registry.list()
            ]
        else:
            value = registry.get(args.node_id).to_dict()
        _emit(value, as_json=args.json)
        return 0
    if args.command == "node":
        inputs = _parse_inputs(args.input)
        projection = await service.run_node(
            args.node_id,
            RunRequest(args.query, args.language, "node", "markdown"),
            inputs=inputs,
        )
    elif args.command == "research":
        mode = args.mode or "normal"
        projection = await service.run(
            RunRequest(
                args.query,
                args.language,
                mode,
                args.output_format,
                args.report_format,
            ),
            workflow_path=args.workflow,
            max_steps=args.max_steps,
            run_id=args.run_id,
        )
    elif args.command == "status":
        projection = service.status(args.run_id)
    elif args.command == "resume":
        projection = await service.resume(args.run_id, max_steps=args.max_steps)
    else:
        raise RuntimeError("unsupported command")
    summary = projection_summary(service.store, projection, output_dir=service.output_dir)
    _emit(summary, as_json=args.json)
    return 1 if args.command in {"research", "resume", "node"} and projection.status == "failed" else 0


def _parse_inputs(values: Sequence[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--input must use PORT=PATH")
        port, path = value.split("=", 1)
        if not port or port in result:
            raise ValueError(f"invalid or duplicate input port: {port!r}")
        requested = Path(path).expanduser()
        if requested.is_symlink() or not requested.is_file():
            raise ValueError(f"input file not found or unsafe: {requested}")
        result[port] = requested.resolve()
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        starts_research = args.command == "research" or (
            args.command == "web" and bool(args.query)
        )
        if starts_research and not args.report_format:
            args.report_format = _prompt_report_format()

        if args.command == "web":
            from deepresearch_cli.web import run_web_server

            return run_web_server(
                host=args.host, port=args.port, runs_dir=args.runs_dir,
                output_dir=args.output_dir, node_dirs=args.nodes_dir,
                startup_request=(
                    {
                        "query": args.query, "language": args.language,
                        "mode": args.mode, "output_format": args.output_format,
                        "report_format": args.report_format,
                        "harness": args.harness,
                        "harness_profile": args.harness_profile,
                        "harness_command": args.harness_command,
                        "harness_model": args.harness_model,
                        "node_timeout_seconds": args.node_timeout_seconds,
                        "max_concurrency": args.max_concurrency,
                        "search_mcp": args.search_mcp,
                        "search_dir": args.search_dir,
                        "search_provider_python": args.search_provider_python,
                        "search_provider_limit": args.search_provider_limit,
                        "camofox_fallback": args.camofox_fallback,
                        "camofox_home": args.camofox_home,
                        "camofox_base_url": args.camofox_base_url,
                    }
                    if args.query else None
                ),
            )
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("deepresearch: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"deepresearch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
