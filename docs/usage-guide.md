# SenseNova-Skills-DeepResearch 使用指南

本文说明当前 SenseNova-Skills-DeepResearch 命令、研究模式、节点链路、输入输出、超时和恢复行为。

## 1. 核心选择

| 选项 | 可选值 | 作用 |
| --- | --- | --- |
| `--mode` | `quick`、`normal`、`heavy` | 选择研究深度与工作流 |
| `--report-format` | `brief`、`formal_report` | 选择简报或正式报告 |
| `--output-format` | `markdown`、`html`、`pdf`、`docx` | 选择交付格式 |
| `--harness` | `hermes`、`codex`、`claude-code`、`openclaw` | 选择 ACP Agent 后端 |

四项彼此独立。用户在 query 中明确指定目录、章节、顺序或承载结构时，用户结构优先，内置报告模板只用于补充覆盖检查。

## 2. 启动研究

`research` 子命令可以省略：

```bash
deepresearch "研究问题" \
  --mode normal \
  --report-format formal_report \
  --output-format markdown \
  --harness hermes
```

等价写法：

```bash
deepresearch research "研究问题" \
  --mode normal \
  --report-format formal_report \
  --output-format markdown \
  --harness hermes
```

常用执行参数：

| 参数 | 说明 |
| --- | --- |
| `--workflow PATH` | 使用自定义 Workflow YAML |
| `--language CODE` | 报告语言 |
| `--max-concurrency N` | 并发 Agent 节点实例上限 |
| `--node-timeout-seconds N` | 未在 Workflow 声明超时的 Agent 节点兜底预算 |
| `--no-node-timeout` | 关闭兜底预算，不影响 Workflow 已声明的节点预算 |
| `--progress auto\|tools\|off` | 控制终端进度输出 |
| `--runs-dir PATH` | Run 持久化目录 |
| `--output-dir PATH` | 最终交付目录 |
| `--search-dir PATH` | 自定义 Search 注册表 |
| `--search-provider-python PATH` | Search Provider 使用的 Python |
| `--search-provider-limit N` | 单次 Provider 返回候选上限 |
| `--no-search-mcp` | 禁用内置 Search MCP |
| `--camofox-fallback` | 启用 Camofox 读取回退 |
| `--no-camofox-fallback` | 禁用 Camofox 读取回退 |
| `--harness-command PATH` | 指定 Harness 命令 |
| `--harness-profile NAME` | 指定 Harness Profile |
| `--harness-model NAME` | 指定支持单次模型选择的 Harness 模型 |

完整参数始终可以通过以下命令查看：

```bash
deepresearch research --help
```

## 3. 模式选择

### Quick

适合快速了解主题和短报告：

```text
research → report-writer → render
```

### Normal

适合常规专题研究：

```text
plan → research → report-writer → render
```

### Heavy

适合长篇、多维度或高严谨度研究：

```text
scout
→ plan
→ research
→ review
→ perspective
→ supplement-planner
→ research
→ review
→ perspective
→ report-planner
→ report-writer
→ stitcher
→ final-review-diagnostic
→ final-repair
→ final-review-recheck
→ render
```

Heavy 中的第二轮 Research、Review 和 Perspective 只在补研任务存在时产生实例。FinalRepair 和 Recheck 只在诊断结果要求修复时产生实例。

## 4. 节点职责与合同

| 节点 | 类型 | 主要输入 | 主要输出 | 作用 |
| --- | --- | --- | --- | --- |
| `scout` | Agent | query | `briefing.json` | 扫描范围、术语、来源方向和风险 |
| `plan` | Agent | query、briefing（可选） | `plan.json`、Research Tasks | 拆分研究维度、关键问题和来源要求 |
| `research` | Agent | Research Task、已有 Evidence/补研计划（可选） | `evidence.json` | 搜索、读取、核验并形成结构化证据 |
| `review` | Agent | Evidence、计划 | Review | 检查证据覆盖、冲突和缺口 |
| `perspective` | Agent | Evidence、Review、计划 | Perspective Feedback | 从不同分析视角提出质疑和补充 |
| `supplement-planner` | Agent | Evidence、Review、Perspective | Supplement Plan | 将缺口转换为定向补研任务 |
| `report-planner` | Agent | 全部 Evidence 和审查结果 | Outline、Content Tasks | 决定报告结构并拆分写作单元 |
| `report-writer` | Agent | Evidence、写作任务或完整研究材料 | Draft | 撰写整篇报告或单个章节 |
| `stitcher` | Script | Outline、Drafts、Repairs | Stitched Report | 确定性拼接并检查标题、引用和 render contract |
| `final-review-diagnostic` | Agent | 拼接报告、Evidence | Final Review、Repair Tasks | 定位需要修复的具体内容单元 |
| `final-repair` | Agent | Repair Task、原 Draft、Evidence | Repaired Draft | 定向修复被点名的内容单元 |
| `final-review-recheck` | Agent | 修复后的报告、Evidence | Final Review | 复查修复结果 |
| `render` | Script | Draft/Stitched Report、Evidence | `report.md` | 统一引用、标题和最终 Markdown |
| `md-html` | Agent | `report.md` | `report.html` | 生成结构化 HTML |
| `md-pdf` | Script | `report.md` | `report.pdf` | 调用确定性转换器生成 PDF |
| `md-docx` | Script | `report.md` | `report.docx` | 调用确定性转换器生成 DOCX |

Agent 节点只能读取声明的输入并向 attempt staging 中写声明的输出。Validator 通过后 Artifact 才会发布。Script 节点执行受信任代码，不调用模型。

## 5. 内置节点超时

超时单位为秒，按模式和 Node ID 生效。重复节点使用相同预算；并发 fan-out 的每个 scope 独立计时。

### Quick

| 节点 | 超时 |
| --- | ---: |
| `research` | 1800 |
| `report-writer` | 300 |
| `md-html` | 900 |

### Normal

| 节点 | 超时 |
| --- | ---: |
| `plan` | 600 |
| `research` | 1800 |
| `report-writer` | 420 |
| `md-html` | 1200 |

### Heavy

| 节点 | 超时 |
| --- | ---: |
| `scout` | 600 |
| `plan` | 600 |
| `research` | 1800 |
| `review` | 600 |
| `perspective` | 600 |
| `supplement-planner` | 600 |
| `report-planner` | 600 |
| `report-writer` | 600 |
| `final-review-diagnostic` | 420 |
| `final-repair` | 420 |
| `final-review-recheck` | 420 |
| `md-html` | 1800 |

`render`、`stitcher`、`md-pdf` 和 `md-docx` 是确定性 Script，不接受 Workflow 超时。

Agent 节点首次超时会记录为 `retryable`，并在全新 Session 和全新 attempt 中重跑当前节点一次。连续第二次超时才使 Run 失败。并发节点只重跑超时 scope，已发布的成功 Artifact 保留。普通失败、认证错误、Validator 错误和 Script 错误不按节点超时重试。

## 6. 状态与恢复

```bash
deepresearch status <run-id> --json
deepresearch resume <run-id> --harness hermes
```

恢复时会读取 Run 创建时保存的 Workflow 和 Node Spec 快照。已经成功的节点不会重新执行；`repairable`、`retryable` 或执行中断的节点从新的 attempt 继续。

限制单次恢复执行的节点实例数：

```bash
deepresearch resume <run-id> \
  --harness hermes \
  --max-steps 1
```

## 7. Web 控制台

只启动控制台：

```bash
deepresearch web \
  --host 127.0.0.1 \
  --port 8765
```

启动控制台并立即创建研究：

```bash
deepresearch web "研究问题" \
  --mode normal \
  --report-format formal_report \
  --output-format markdown \
  --harness hermes
```

访问 <http://127.0.0.1:8765>。页面从 Manifest、Journal 和 Artifact 重建进度，通过 SSE 接收更新。Web 展示持久化状态，不代替已结束的执行进程；需要继续执行时使用 `resume`。

## 8. Harness 检查

```bash
deepresearch doctor --harness hermes --json
deepresearch doctor --harness codex --json
deepresearch doctor --harness claude-code --json
deepresearch doctor --harness openclaw --json
```

四个生产 Harness 都通过统一的 ACP 运行边界执行节点。每个 attempt 使用独立进程或独立 Session，避免上下文和工具状态跨 attempt 泄漏。

## 9. Search 与浏览器

```bash
deepresearch sources init
deepresearch sources list --json
deepresearch domains list --json

deepresearch browser setup
deepresearch browser start
deepresearch browser status
deepresearch browser stop
```

Search MCP 默认启用。Research 先使用搜索来源发现候选，再读取原文。普通 HTTP 读取符合回退条件时才尝试 Camofox；Camofox 不处理 CAPTCHA、登录或付费墙。

## 10. 节点发现与单节点执行

```bash
deepresearch nodes list --json
deepresearch nodes describe research --json
deepresearch node run md-html \
  --input report=./report.md \
  --harness hermes
```

自定义节点目录可以重复传入：

```bash
deepresearch nodes list \
  --nodes-dir ./nodes \
  --json
```

## 11. 输出

| 输出格式 | 主文件 | 额外要求 |
| --- | --- | --- |
| Markdown | `report.md` | 无 |
| HTML | `report.html` | 由 `md-html` Agent 生成 |
| PDF | `report.pdf` | Typst |
| DOCX | `report.docx` | Pandoc |

最终文件写入 `output/<run-id>/`。Run 的中间 Artifact 和诊断记录保存在 `runs/<run-id>/`，不应把两者混为同一目录。

## 12. 自定义 Workflow

```yaml
version: 1
name: custom
steps:
  - plan
  - research
  - report-writer
  - render
timeouts:
  plan: 600
  research: 1800
  report-writer: 600
result: report
```

```bash
deepresearch "研究问题" \
  --workflow ./workflow.yaml \
  --mode normal \
  --report-format formal_report \
  --harness hermes
```

更多配置约束见[运行时设计](design.md)和[自定义工作流示例](../examples/custom-workflow/README.md)。
