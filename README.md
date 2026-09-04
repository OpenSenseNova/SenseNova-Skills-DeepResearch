<div align="center">

# SenseNova-Skills-DeepResearch

[English](README_EN.md) · [中文](README.md)

[![CI](https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch/actions/workflows/npm-package.yml/badge.svg)](https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch/actions/workflows/npm-package.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Node.js](https://img.shields.io/badge/Node.js-%3E%3D22-339933.svg)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-3776AB.svg)](https://www.python.org/)

</div>

> 本地运行、证据可追溯的深度研究 CLI。

SenseNova-Skills-DeepResearch 用来完成从研究问题到成品报告的完整流程。它会自动拆解问题、并发搜索、整理证据、
撰写和检查报告，并可导出 Markdown、HTML、PDF 或 DOCX。整个过程在本地运行，可查看进度、保留记录，
也能在中断后继续，同时复用用户已有的 Hermes、Codex、Claude Code 或 OpenClaw 模型环境。

### 功能介绍

- 把规划、搜索、证据和写作组织成可恢复的完整流程，而不是一次性聊天回复；
- 每个结论都保留来源和中间证据，便于复核；
- 同一套命令可切换不同 Harness，并输出可交付的多种报告格式。

[使用指南](docs/usage-guide.md) ·
[搜索说明](docs/search-mcp.md)

主要能力：

- `quick`、`normal`、`heavy` 三种研究深度；
- 多领域、多来源并发搜索，自动去重并保留出处；
- 简报和正式报告两种写作形式；
- Markdown、HTML、PDF、DOCX 四种交付格式；
- 终端进度和本地 Web 控制台；
- 普通抓取失败时按需使用 Camofox。

## 环境要求

- Node.js >=22（包含 npm）
- Python >=3.10
- 运行研究前，需安装并配置至少一种 Agent：Hermes、Codex、Claude Code ACP 或 OpenClaw
- 导出 DOCX 需要 Pandoc；导出 PDF 需要 Typst

## 安装

### GitHub 源码安装（推荐）

安装时会在
`~/.deepresearch-cli/npm-runtime/<version>/` 创建独立环境，不会修改项目目录或系统 Python：

```bash
git clone https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch.git
cd SenseNova-Skills-DeepResearch
bash scripts/install.sh
deepresearch --help
```

如果安装程序没有找到合适的 Python，可以提前指定：

```bash
export DEEPRESEARCH_PYTHON=/path/to/python3
```

源码更新后，在仓库根目录执行 `bash scripts/update.sh`。脚本只重建并重新安装当前检出的代码，
不会自动拉取 Git、删除运行记录或改写 `~/.deepresearch-cli/` 配置；工作区有未提交修改时会停止。

### 从源码构建

```bash
git clone https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch.git
cd SenseNova-Skills-DeepResearch
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' build
.venv/bin/python scripts/build_npm_package.py
```

构建产物位于 `dist/`。npm 安装层的更多说明见 [npm/README.md](npm/README.md)。

## 快速开始

先初始化搜索配置，再检查所选 Agent 是否可用：

```bash
deepresearch sources init
# 按命令输出编辑 ~/.deepresearch-cli/search/.env

deepresearch doctor --harness hermes --json
```

没有 API 凭据时，公开搜索来源仍可使用；需要更多来源时再填写 `.env` 中对应的 token、cookie
或 User-Agent。

运行一次正式报告研究：

```bash
deepresearch "对比主要国际组织对全球经济增长率的预测，并分析差异原因" \
  --mode normal \
  --report-format formal_report \
  --output-format markdown \
  --harness hermes
```

核心参数：

| 参数 | 可选值 | 作用 |
| --- | --- | --- |
| `--mode` | `quick`、`normal`、`heavy` | 选择研究深度 |
| `--report-format` | `brief`、`formal_report` | 选择简报或正式报告 |
| `--output-format` | `markdown`、`html`、`pdf`、`docx` | 选择最终文件格式 |
| `--harness` | `hermes`、`codex`、`claude-code`、`openclaw` | 选择实际执行的 Agent |

如果 query 已明确要求报告目录或章节顺序，SenseNova-Skills-DeepResearch 会优先遵循用户结构，内置模板只作为补充参考。

最终报告位于 `output/<run-id>/`，运行记录位于 `runs/<run-id>/`。查看或恢复运行：

```bash
deepresearch status <run-id> --json
deepresearch resume <run-id> --harness hermes
```

<details>
<summary>研究模式详情</summary>

## 研究模式

| 模式 | 适合场景 | 流程特点 |
| --- | --- | --- |
| Quick | 快速了解主题、短报告 | 直接研究、写作和导出 |
| Normal | 常规专题研究 | 先规划，再并发研究、写作和导出 |
| Heavy | 高要求、长篇或多维度研究 | 增加多轮审查、补研、分章节写作和最终修复 |

三种模式均可调整并发数、搜索来源和输出格式。各模式的完整节点说明见
[使用指南](docs/usage-guide.md)。

</details>

## Web 进度页面

启动本地控制台：

```bash
deepresearch web
```

访问 <http://127.0.0.1:8765>，即可创建研究任务并查看总体进度、当前阶段、来源数量、章节状态和活动记录。
页面刷新后仍会读取已保存的进度。

![Web 研究进度页面](assets/web-demo.png)

也可以直接启动任务：

```bash
deepresearch web "分析企业级 AI Agent 平台的竞争格局" \
  --mode heavy \
  --report-format formal_report \
  --output-format pdf \
  --harness hermes
```

默认只监听回环地址。需要修改端口或保存目录时：

```bash
deepresearch web \
  --port 9000 \
  --runs-dir ./runs \
  --output-dir ./output
```

如果原运行进程已经结束，可使用 `deepresearch resume <run-id> --harness <harness>` 继续。

<details>
<summary>Agent、搜索与导出配置</summary>

## 选择 Agent

项目提供 Hermes、Codex、Claude Code ACP 和 OpenClaw 四种 Agent 适配器。适配器已通过本地单元测试和模拟运行验证；实际运行是否可用，取决于对应 Agent 在当前环境中的安装、登录、模型和权限配置。

| Harness | 使用前准备 |
| --- | --- |
| `hermes` | 完成 Hermes 模型配置或登录 |
| `codex` | 安装 Codex CLI 并执行 `codex login` |
| `claude-code` | 安装 Claude Code ACP Adapter 并完成登录 |
| `openclaw` | 配置并启动 OpenClaw Gateway |

检查任一 Harness：

```bash
deepresearch doctor --harness hermes --json
deepresearch doctor --harness codex --json
deepresearch doctor --harness claude-code --json
deepresearch doctor --harness openclaw --json
```

Claude Code 需要额外安装 ACP Adapter：

```bash
npm install -g @agentclientprotocol/claude-agent-acp
deepresearch doctor --harness claude-code --json
# 仅在 doctor 确认没有可用认证时执行：
# claude-agent-acp --cli auth login
```

OpenClaw 使用其 Gateway 中已经配置的模型，不支持通过单次 DeepResearch 命令临时切换模型。

## 搜索配置

初始化并查看可用来源：

```bash
deepresearch sources init
deepresearch sources list --json
deepresearch domains list --json
```

用户配置保存在 `~/.deepresearch-cli/search/.env`。如需使用自定义搜索注册表，可以传入：

```bash
deepresearch "研究问题" \
  --mode normal \
  --report-format formal_report \
  --harness hermes \
  --search-dir /path/to/search
```

CLI 会根据研究主题选择相关领域和来源，并行执行搜索，对结果进行去重，再读取选中的原文。
完整来源、领域和抓取规则见 [Search MCP 说明](docs/search-mcp.md)。

## Camofox 回退

Camofox 用于处理普通 HTTP 无法读取的公开网页。SenseNova-Skills-DeepResearch 会先尝试普通抓取，只有遇到访问拒绝、
反自动化页面或 JavaScript 空壳时才进行一次浏览器回退。

首次使用前安装并启动：

```bash
deepresearch browser setup
deepresearch browser start
deepresearch browser status
```

浏览器文件默认保存在 `~/.deepresearch-cli/camofox`，不会打进基础安装包。停止服务：

```bash
deepresearch browser stop
```

Camofox 不会绕过 CAPTCHA、登录、付费墙或其他访问控制。未安装或不可用时，Research 会切换来源，
不会因此阻塞整个报告。完全禁用可使用 `--no-camofox-fallback`。

## 自定义工作流

内置工作流位于 `config/workflows/`。也可以通过一份简单 YAML 调整节点顺序和 Agent 节点超时：

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
deepresearch "研究问题" \
  --workflow ./my-workflow.yaml \
  --mode normal \
  --report-format formal_report \
  --harness hermes
```

`steps` 表示执行顺序，`timeouts` 的单位是秒。Agent 节点首次超时会自动使用全新 Session
重试当前节点一次；连续两次超时才终止运行。并发节点只重试超时的 scope，已完成结果会保留。
更多节点配置、自定义脚本和输入输出说明见
[自定义工作流示例](examples/custom-workflow/README.md) 和 [设计文档](docs/design.md)。

## 导出 Word 和 PDF

使用 DOCX 前安装 Pandoc，使用 PDF 前安装 Typst。请通过对应项目的官方安装方式准备命令行工具，
并确保 `pandoc` 和 `typst` 可以从 `PATH` 调用。

研究时直接选择输出格式：

```bash
deepresearch "研究问题" \
  --mode normal \
  --report-format formal_report \
  --output-format docx \
  --harness hermes
```

也可以转换已有的 Markdown：

```bash
deepresearch node run md-docx --input report=./report.md
deepresearch node run md-pdf --input report=./report.md
```

</details>

## 开发与验证

```bash
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -e ".[dev]" build
.venv/bin/python -m pytest
.venv/bin/python -m build
```

仓库保留核心回归测试；这些测试不会调用真实模型或产生外部服务费用。

## 更多文档

- [文档索引](docs/README.md)
- [诊断指南](docs/diagnostics.md)
- [SenseNova-Skills-DeepResearch 使用指南](docs/usage-guide.md)
- [Search MCP 架构与工具说明](docs/search-mcp.md)
- [运行时设计与扩展方式](docs/design.md)
- [自定义工作流示例](examples/custom-workflow/README.md)
- [npm 安装层说明](npm/README.md)
