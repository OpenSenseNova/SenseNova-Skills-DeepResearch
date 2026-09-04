<div align="center">

# SenseNova-Skills-DeepResearch

English · [中文](README.md)

[![CI](https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch/actions/workflows/npm-package.yml/badge.svg)](https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch/actions/workflows/npm-package.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Node.js](https://img.shields.io/badge/Node.js-%3E%3D22-339933.svg)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-3776AB.svg)](https://www.python.org/)

</div>

> A local, evidence-traceable deep research CLI.

SenseNova-Skills-DeepResearch turns a research question into a finished, evidence-backed report. It plans the
work, searches multiple source domains concurrently, collects evidence, writes and validates the
report, and exports Markdown, HTML, PDF, or DOCX. Runs are persisted, visible through terminal and
Web progress views, and resumable after interruption.

### Why use it

- Turns planning, search, evidence, and writing into a resumable workflow instead of a one-shot chat reply;
- preserves sources and intermediate evidence so conclusions can be reviewed;
- uses one command surface across Harnesses and exports delivery-ready report formats.

The CLI works with an existing Hermes, Codex, Claude Code, or OpenClaw model environment through a
common ACP-based harness interface.

[Documentation](docs/README.md) ·
[User guide](docs/usage-guide.md) ·
[Search architecture](docs/search-mcp.md)

Key capabilities:

- Quick, Normal, and Heavy research modes;
- concurrent multi-domain and multi-source search;
- source normalization, deduplication, retrieval, and evidence tracking;
- brief and formal report styles;
- Markdown, HTML, PDF, and DOCX output;
- terminal progress and a Web dashboard;
- persistent runs and node-level resume;
- optional Camofox fallback for public pages that ordinary HTTP cannot read.

## Requirements

- Node.js >=22 (with npm);
- Python >=3.10;
- at least one configured agent environment to run research: Hermes, Codex, Claude Code ACP, or OpenClaw;
- Pandoc for DOCX output;
- Typst for PDF output.

## Install

### Install from GitHub source

Installation creates an isolated runtime under `~/.deepresearch-cli/npm-runtime/<version>/` and does
not modify the project directory or the system Python environment.

```bash
git clone https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch.git
cd SenseNova-Skills-DeepResearch
bash scripts/install.sh
deepresearch --help
```

If Python cannot be discovered automatically, set it before installation:

```bash
export DEEPRESEARCH_PYTHON=/path/to/python3
```

After pulling or switching to a clean checkout, run `bash scripts/update.sh` to rebuild and reinstall.
It does not modify Git history, delete runs or outputs, or rewrite `~/.deepresearch-cli/` configuration.

### Build from source

```bash
git clone https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch.git
cd SenseNova-Skills-DeepResearch
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' build
.venv/bin/python scripts/build_npm_package.py
```

Build artifacts are written to `dist/`. See [npm/README.md](npm/README.md) for details about the npm
launcher and isolated Python runtime.

## Quick start

Initialize the search configuration and verify a harness:

```bash
deepresearch sources init
deepresearch doctor --harness hermes --json
```

Public search sources remain available without API credentials. Add optional tokens, cookies, user
agents, or proxy settings to the configuration file reported by `sources init` when additional
providers are needed.

Run a formal report:

```bash
deepresearch "Compare major institutions' global growth forecasts and explain their differences" \
  --mode normal \
  --report-format formal_report \
  --output-format markdown \
  --harness hermes
```

Core options:

| Option | Values | Purpose |
| --- | --- | --- |
| `--mode` | `quick`, `normal`, `heavy` | Select research depth and workflow |
| `--report-format` | `brief`, `formal_report` | Select the writing style |
| `--output-format` | `markdown`, `html`, `pdf`, `docx` | Select the delivery format |
| `--harness` | `hermes`, `codex`, `claude-code`, `openclaw` | Select the agent backend |

When the query specifies a report structure or section order, that structure takes precedence over
the built-in report template.

Reports are exported to `output/<run-id>/`; persistent run state is stored in `runs/<run-id>/`.

```bash
deepresearch status <run-id> --json
deepresearch resume <run-id> --harness hermes
```

<details>
<summary>Research mode details</summary>

## Research modes

| Mode | Best for | Workflow behavior |
| --- | --- | --- |
| Quick | Fast topic exploration and short reports | Research, write, validate, and render |
| Normal | General-purpose research | Plan, research in parallel, write, validate, and render |
| Heavy | Long-form or high-rigor research | Multi-pass review, supplementary research, section writing, and targeted final repair |

All modes support configurable concurrency, search sources, report styles, and output formats. See
the [user guide](docs/usage-guide.md) for the exact nodes.

</details>

## Web dashboard

Start the dashboard:

```bash
deepresearch web
```

Open <http://127.0.0.1:8765> to create runs and view workflow progress, active stages, source counts,
writing status, and activity. The page rebuilds its state from persisted run data after refresh.

![Web research progress dashboard](assets/web-demo.jpeg)

Start the dashboard and a research run together:

```bash
deepresearch web "Analyze the enterprise AI agent platform landscape" \
  --mode heavy \
  --report-format formal_report \
  --output-format pdf \
  --harness hermes
```

Change the listener or storage directories when needed:

```bash
deepresearch web \
  --host 127.0.0.1 \
  --port 9000 \
  --runs-dir ./runs \
  --output-dir ./output
```

If the original execution process has stopped, resume it with
`deepresearch resume <run-id> --harness <harness>`.

<details>
<summary>Harness, search, and export configuration</summary>

## Harness setup

The project provides adapters for Hermes, Codex, Claude Code ACP, and OpenClaw. The adapters are covered by local unit tests and simulated runs; actual availability depends on the corresponding agent's installation, authentication, model, and permission configuration. Public CI does not sign in to or invoke external agent accounts on a user's behalf.

| Harness | Preparation |
| --- | --- |
| `hermes` | Configure or sign in to Hermes |
| `codex` | Install Codex CLI and run `codex login` |
| `claude-code` | Install the Claude Code ACP adapter and authenticate |
| `openclaw` | Configure and start the OpenClaw Gateway |

Run diagnostics for any harness:

```bash
deepresearch doctor --harness hermes --json
deepresearch doctor --harness codex --json
deepresearch doctor --harness claude-code --json
deepresearch doctor --harness openclaw --json
```

Claude Code requires its ACP adapter:

```bash
npm install -g @agentclientprotocol/claude-agent-acp
deepresearch doctor --harness claude-code --json
# Only if doctor confirms that no usable authentication is available:
# claude-agent-acp --cli auth login
```

OpenClaw uses the model configured in its Gateway; a single DeepResearch command does not override
that model.

## Search configuration

```bash
deepresearch sources init
deepresearch sources list --json
deepresearch domains list --json
```

The default user configuration is stored under `~/.deepresearch-cli/search/`. A custom search
registry can be selected per run:

```bash
deepresearch "Research question" \
  --mode normal \
  --report-format formal_report \
  --harness hermes \
  --search-dir /path/to/search
```

Research selects relevant domains and sources, runs searches concurrently, normalizes and
deduplicates candidates, and retrieves selected source content. See
[Search MCP](docs/search-mcp.md) for the tool contracts and failure behavior.

## Camofox fallback

Camofox is an optional fallback for public pages that return access-denied responses,
anti-automation shells, or empty JavaScript content to ordinary HTTP retrieval. HTTP is always tried
first; browser fallback is used only when the response qualifies.

```bash
deepresearch browser setup
deepresearch browser start
deepresearch browser status
```

Browser files are stored separately under `~/.deepresearch-cli/camofox` and are not included in the
base npm package.

```bash
deepresearch browser stop
```

Camofox does not bypass CAPTCHA, login, paywalls, or other access controls. If it is unavailable,
Research switches sources instead of blocking the workflow. Disable it explicitly with
`--no-camofox-fallback`.

## Custom workflows and timeouts

Built-in workflows live in `config/workflows/`. A custom YAML file can change node order and agent
node budgets:

```yaml
version: 1
name: custom
steps:
  - scout
  - plan
  - research
  - report-writer
  - render
timeouts:
  research: 1800
  report-writer: 600
result: report
```

```bash
deepresearch "Research question" \
  --workflow ./my-workflow.yaml \
  --mode normal \
  --report-format formal_report \
  --harness hermes
```

Timeout values are seconds and apply only to agent nodes. A timed-out agent node is retried once in
a fresh session. A second consecutive timeout fails the run. For fan-out nodes, only the timed-out
scope is retried; completed scopes are preserved. Deterministic script nodes do not use workflow
timeouts.

See the [custom workflow example](examples/custom-workflow/README.md) and
[runtime design](docs/design.md) for the configuration contract.

## DOCX and PDF output

Install Pandoc for DOCX and Typst for PDF using their official installation instructions, and make
sure the commands are available through `PATH`.

```bash
deepresearch "Research question" \
  --mode normal \
  --report-format formal_report \
  --output-format docx \
  --harness hermes
```

Convert an existing Markdown report directly:

```bash
deepresearch node run md-docx --input report=./report.md
deepresearch node run md-pdf --input report=./report.md
```

</details>

## Development

```bash
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -e ".[dev]" build
.venv/bin/python -m pytest
.venv/bin/python -m build
```

The repository keeps only the core regression tests required for a release. They do not invoke live
models or incur external service charges.

## Documentation

- [Documentation index](docs/README.md)
- [Diagnostics guide](docs/diagnostics.md)
- [SenseNova-Skills-DeepResearch user guide](docs/usage-guide.md)
- [Search MCP architecture](docs/search-mcp.md)
- [Runtime design and extension model](docs/design.md)
- [Custom workflow example](examples/custom-workflow/README.md)
- [npm launcher and installation](npm/README.md)
