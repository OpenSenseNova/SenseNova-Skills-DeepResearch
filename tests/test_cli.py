import asyncio
import io
import json
from pathlib import Path

import pytest
import yaml

from deepresearch_cli.cli import _emit, _prompt_report_format, build_parser, main


def test_web_accepts_an_immediate_research_request():
    args = build_parser().parse_args([
        "web", "直接启动的研究", "--mode", "heavy", "--output-format", "pdf",
        "--report-format", "formal_report", "--harness", "hermes",
        "--max-concurrency", "6",
    ])

    assert args.command == "web"
    assert args.query == "直接启动的研究"
    assert args.mode == "heavy"
    assert args.output_format == "pdf"
    assert args.report_format == "formal_report"
    assert args.max_concurrency == 6


def test_web_accepts_normal_mode():
    args = build_parser().parse_args([
        "web", "Normal research", "--mode", "normal", "--report-format",
        "formal_report", "--harness", "hermes",
    ])

    assert args.command == "web"
    assert args.mode == "normal"


def test_research_accepts_camofox_fallback_configuration():
    args = build_parser().parse_args([
        "Research topic", "--report-format", "formal_report", "--harness", "hermes",
        "--camofox-fallback", "--camofox-home", "/opt/camofox",
        "--camofox-base-url", "http://127.0.0.1:9377",
    ])

    assert args.camofox_fallback is True
    assert args.camofox_home == Path("/opt/camofox")
    assert args.camofox_base_url == "http://127.0.0.1:9377"


def test_camofox_fallback_is_enabled_by_default_and_can_be_disabled():
    default_args = build_parser().parse_args([
        "Research topic", "--report-format", "formal_report", "--harness", "hermes",
    ])
    disabled_args = build_parser().parse_args([
        "Research topic", "--report-format", "formal_report", "--harness", "hermes",
        "--no-camofox-fallback",
    ])

    assert default_args.camofox_fallback is True
    assert disabled_args.camofox_fallback is False


def test_browser_management_commands_are_exposed():
    args = build_parser().parse_args([
        "browser", "setup", "--home", "/opt/camofox", "--npm-command", "npm",
    ])

    assert args.command == "browser"
    assert args.browser_command == "setup"
    assert args.home == Path("/opt/camofox")


def test_diagnostics_returns_the_cli_owned_guide_path(capsys):
    assert main(["diagnostics", "--json"]) == 0

    value = json.loads(capsys.readouterr().out)
    assert Path(value["path"]).resolve() == (
        Path(__file__).resolve().parents[1] / "docs" / "diagnostics.md"
    ).resolve()


from deepresearch_cli.config import NodeRegistry, RunRequest, load_workflow_spec
from deepresearch_cli.driver import ExecutionSessionConfig, WorkflowDriver
from deepresearch_cli.harness.stub import StubHarness
from deepresearch_cli.persistence import RunStore
from deepresearch_cli.service import WorkflowService
from tests.search_test_utils import write_search_source


def _partial_run(tmp_path):
    harness = StubHarness()
    asyncio.run(harness.start())
    store = RunStore(tmp_path / "runs")
    driver = WorkflowDriver(
        store,
        harness,
        ExecutionSessionConfig(harness="stub"),
        output_dir=tmp_path / "output",
    )
    projection = driver.create_run(
        RunRequest("status test", mode="normal"),
        load_workflow_spec(mode="normal"),
        NodeRegistry.load(),
    )
    return store, asyncio.run(driver.drive(projection.run_id, max_steps=1))


def test_query_is_the_top_level_user_command():
    args = build_parser().parse_args(
        ["What changed?", "--mode", "heavy", "--output-format", "html",
         "--report-format", "formal_report", "--harness", "hermes"]
    )

    assert args.command == "research"
    assert args.query == "What changed?"
    assert args.mode == "heavy"
    assert args.output_format == "html"


@pytest.mark.parametrize("output_format", ["markdown", "html", "pdf", "docx"])
def test_research_accepts_each_output_format(output_format):
    args = build_parser().parse_args(
        ["Research topic", "--output-format", output_format, "--report-format",
         "formal_report", "--harness", "hermes"]
    )

    assert args.output_format == output_format


@pytest.mark.parametrize("report_format", ["brief", "formal_report"])
def test_research_accepts_each_report_format(report_format):
    args = build_parser().parse_args(
        ["Research topic", "--report-format", report_format, "--harness", "hermes"]
    )

    assert args.report_format == report_format


def test_research_defers_missing_report_format_to_interactive_prompt():
    args = build_parser().parse_args(["Research topic", "--harness", "hermes"])

    assert args.report_format is None


def test_report_format_prompt_accepts_a_number():
    output = io.StringIO()

    selected = _prompt_report_format(
        input_fn=lambda _prompt: "2", output=output, is_tty=True
    )

    assert selected == "formal_report"
    assert "请选择报告形式" in output.getvalue()
    assert "正式报告" in output.getvalue()
    assert "请输入 1 或 2：" in output.getvalue()
    assert "或 3" not in output.getvalue()


def test_standard_report_is_no_longer_accepted():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["Research topic", "--report-format", "standard_report", "--harness", "hermes"]
        )


def test_run_request_only_accepts_brief_and_formal_report():
    assert RunRequest("brief", report_format="brief").report_format == "brief"
    assert RunRequest("formal", report_format="formal_report").report_format == "formal_report"
    with pytest.raises(ValueError, match="brief or formal_report"):
        RunRequest("removed", report_format="standard_report")


def test_legacy_standard_report_snapshot_migrates_to_formal_report():
    request = RunRequest.from_dict({"query": "legacy", "report_format": "standard_report"})
    assert request.report_format == "formal_report"


def test_report_format_prompt_retries_an_invalid_selection():
    answers = iter(["unknown", "formal_report"])
    output = io.StringIO()

    selected = _prompt_report_format(
        input_fn=lambda _prompt: next(answers), output=output, is_tty=True
    )

    assert selected == "formal_report"
    assert "无效选择" in output.getvalue()
    assert "无效选择，请输入 1 或 2。" in output.getvalue()


def test_report_format_prompt_requires_an_option_in_noninteractive_mode():
    with pytest.raises(ValueError, match="required in non-interactive mode"):
        _prompt_report_format(is_tty=False)


def test_web_landing_page_defers_report_format_to_form():
    args = build_parser().parse_args(["web", "--harness", "hermes"])

    assert args.query is None
    assert args.report_format is None


def test_web_immediate_query_requires_report_format_when_noninteractive(capsys):
    assert main(["web", "Research topic", "--harness", "hermes"]) == 2
    assert (
        "--report-format is required in non-interactive mode"
        in capsys.readouterr().err
    )


def test_removed_format_option_is_not_accepted():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["Research topic", "--format", "pdf"])


def test_old_run_subcommand_is_not_a_compatibility_alias():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "query", "--harness", "hermes"])


def test_removed_node_skill_directory_option_is_not_accepted():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nodes", "list", "--skills-dir", "/tmp/skills"])


def test_search_registry_is_a_user_selectable_directory(tmp_path):
    args = build_parser().parse_args(
        [
            "doctor",
            "--harness",
            "hermes",
            "--search-dir",
            str(tmp_path / "search"),
        ]
    )

    assert args.search_dir == tmp_path / "search"


def test_removed_search_skills_directory_option_is_not_accepted():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["doctor", "--harness", "hermes", "--search-skills-dir", "/tmp/skills"]
        )


def test_sources_can_be_listed_and_described_without_starting_hermes(
    tmp_path, capsys
):
    search_dir = tmp_path / "search"
    write_search_source(search_dir, "custom_docs", required_modules=[])

    assert main(
        ["sources", "list", "--search-dir", str(search_dir), "--json"]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["provider"] for item in listed] == ["custom_docs"]

    assert main(
        [
            "sources",
            "describe",
            "custom_docs",
            "--search-dir",
            str(search_dir),
            "--json",
        ]
    ) == 0
    described = json.loads(capsys.readouterr().out)
    assert described["source_file"] == "custom_docs.yaml"
    assert described["available"] is True


def test_sources_init_creates_stable_user_configuration(
    tmp_path, capsys, monkeypatch
):
    config_home = tmp_path / "user-search"
    monkeypatch.setenv("DEEPRESEARCH_SEARCH_CONFIG_HOME", str(config_home))

    assert main(["sources", "init", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    destination = config_home / ".env"

    assert value["status"] == "ready"
    assert value["path"] == str(destination)
    assert destination.is_file()


def test_domains_can_be_listed_and_described_without_starting_harness(
    tmp_path, capsys
):
    search_dir = tmp_path / "search"
    write_search_source(search_dir, "custom_docs", required_modules=[])
    domains_dir = search_dir / "domains"
    domains_dir.mkdir()
    (domains_dir / "documentation.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "documentation",
                "description": "Product documentation discovery.",
                "default_operation": "search",
                "operations": {
                    "search": {
                        "description": "Search configured documentation.",
                        "sources": ["custom_docs"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert main(["domains", "list", "--search-dir", str(search_dir), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["domain"] for item in listed] == ["documentation"]

    assert main(
        [
            "domains",
            "describe",
            "documentation",
            "--search-dir",
            str(search_dir),
            "--json",
        ]
    ) == 0
    described = json.loads(capsys.readouterr().out)
    assert described["default_operation"] == "search"
    assert described["operations"][0]["sources"] == ["custom_docs"]


def test_custom_workflow_accepts_an_explicit_node_behavior_mode(tmp_path):
    args = build_parser().parse_args(
        [
            "query",
            "--mode",
            "heavy",
            "--workflow",
            str(tmp_path / "flow.yaml"),
            "--report-format",
            "formal_report",
            "--harness",
            "hermes",
        ]
    )

    assert args.mode == "heavy"
    assert args.workflow == tmp_path / "flow.yaml"


def test_status_reads_new_run_without_starting_a_harness(tmp_path, capsys):
    store, projection = _partial_run(tmp_path)

    code = main(
        ["status", projection.run_id, "--runs-dir", str(store.root), "--output-dir", str(tmp_path / "output"), "--json"]
    )
    value = json.loads(capsys.readouterr().out)

    assert code == 0
    assert value["status"] == "running"
    assert value["completed_steps"] == ["plan"]
    assert value["result"] is None


def test_nodes_can_be_discovered_without_running_a_model(capsys):
    code = main(["nodes", "list", "--json"])
    value = json.loads(capsys.readouterr().out)

    assert code == 0
    assert any(item["id"] == "md-html" and item["kind"] == "agent" for item in value)


def test_plain_output_always_prints_result_location(capsys):
    _emit(
        {
            "run_id": "run-1",
            "status": "completed",
            "workflow": "heavy",
            "output_format": "html",
            "result": {
                "type": "html_report",
                "path": "/tmp/output/run-1/report.html",
                "source_report_path": "/tmp/output/run-1/report.md",
            },
        },
        as_json=False,
    )

    output = capsys.readouterr().out
    assert "result_path: /tmp/output/run-1/report.html" in output
    assert "source_report_path: /tmp/output/run-1/report.md" in output


def test_json_output_preserves_result_location(capsys):
    _emit({"status": "completed", "result": {"type": "report", "path": "/tmp/report.md"}}, as_json=True)
    assert json.loads(capsys.readouterr().out)["result"]["path"] == "/tmp/report.md"


def test_cli_never_exposes_stub_harness():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["doctor", "--harness", "stub"])


@pytest.mark.parametrize("value", ["0", "51"])
def test_cli_rejects_search_provider_limit_outside_supported_range(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["doctor", "--harness", "hermes", "--search-provider-limit", value]
        )


@pytest.mark.parametrize("value", [True, 0, 51, 1.5, "20"])
def test_execution_config_rejects_invalid_search_provider_limit(value):
    with pytest.raises(ValueError, match="between 1 and 50"):
        ExecutionSessionConfig(harness="hermes", search_provider_limit=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_execution_config_rejects_invalid_node_timeout(value):
    with pytest.raises(ValueError, match="finite and positive"):
        ExecutionSessionConfig(harness="hermes", node_timeout_seconds=value)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "2"])
def test_execution_config_rejects_invalid_max_concurrency(value):
    with pytest.raises(ValueError, match="positive integer"):
        ExecutionSessionConfig(harness="hermes", max_concurrency=value)


def test_node_input_syntax_rejects_missing_or_duplicate_ports(tmp_path):
    source = tmp_path / "report.md"
    source.write_text("report", encoding="utf-8")
    from deepresearch_cli.cli import _parse_inputs

    assert _parse_inputs([f"report={source}"]) == {"report": source.resolve()}
    with pytest.raises(ValueError, match="PORT=PATH"):
        _parse_inputs([str(source)])
    with pytest.raises(ValueError, match="duplicate"):
        _parse_inputs([f"report={source}", f"report={source}"])


def test_single_node_execution_imports_external_input_by_port(tmp_path):
    class ServiceHarness(StubHarness):
        async def probe(self):
            return {"model_check": "stub"}

    source = tmp_path / "source.md"
    source.write_text("# Source report\n", encoding="utf-8")
    service = WorkflowService(
        tmp_path / "runs",
        ExecutionSessionConfig(harness="stub"),
        output_dir=tmp_path / "output",
    )
    harness = ServiceHarness()
    service._harness = lambda: harness

    projection = asyncio.run(
        service.run_node(
            "md-html",
            RunRequest("make html", mode="node", output_format="html"),
            inputs={"report": source},
        )
    )

    assert projection.status == "completed"
    assert harness.invocations[0].agent_context["inputs"]["report"][0]["type"] == "report"
    assert (tmp_path / "output" / projection.run_id / "report.html").is_file()
