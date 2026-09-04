# SenseNova-Skills-DeepResearch 诊断指南

需要定位当前安装所携带的本文件时，执行：

```bash
deepresearch diagnostics --json
```

本文供 sn-deepresearch-cli 和维护者在安装、预检、启动、搜索、浏览器回退、运行恢复或导出失败时使用。按故障范围执行最少的只读检查；不要为了诊断重置配置、删除 Run 或启动重复任务。

## 1. 诊断原则

1. 先保留原始错误、命令参数、`run_id` 和失败节点。
2. 先做只读检查，再提出修改或安装操作。
3. 只读取完成当前判断所需的诊断文件，避免输出大段模型响应或网页正文。
4. 不显示环境变量值、token、Cookie、代理凭据、npm 凭据或 Harness Profile 秘密。
5. 不把 Search Provider 失败等同于模型失败，也不把 Web 展示问题等同于 Run 失败。
6. 不删除 `runs/<run-id>/`；恢复依赖其中的 Manifest、Journal、Artifact 和 attempt 诊断。
7. 需要安装依赖、修改用户配置、启动服务或访问网络时，先遵循当前环境的授权要求。

## 2. 最小诊断入口

先根据现象选择命令，不要无条件全部执行：

```bash
deepresearch --help
deepresearch doctor --harness <harness> --json
deepresearch sources list --json
deepresearch domains list --json
deepresearch browser status --json
deepresearch status <run-id> --json
deepresearch resume --help
deepresearch web --help
```

诊断结果至少记录：

- 执行的命令；
- 退出状态；
- 失败范围；
- 可安全重试与否；
- 建议的下一步；
- 仍需用户提供的非敏感信息。

## 3. CLI 不存在或无法启动

### 现象

- `deepresearch: command not found`；
- npm 安装完成但命令不可执行；
- Python 版本不满足要求；
- 安装后的隔离运行时不完整。

### 检查

```bash
command -v node
command -v npm
command -v python3
command -v deepresearch
node --version
npm --version
python3 --version
npm list -g --depth=0
```

判断顺序：

1. Node.js 必须满足 CLI 安装要求。
2. Python 必须满足运行要求；自动发现失败时才使用 `DEEPRESEARCH_PYTHON` 指定解释器。
3. npm 全局包存在但命令不在 `PATH` 时，报告 npm 全局可执行目录问题，不重复安装。
4. npm 包不存在时，从项目 GitHub Release 的 `.tgz` 安装；不要静默改用其他同名包。
5. 安装失败时保留 npm 输出，区分下载失败、Python 发现失败和 wheel 依赖安装失败。

安装或升级属于写操作。取得授权后再执行：

```bash
npm install -g <github-release-tgz-url>
deepresearch --help
```

不要使用 uv 作为用户安装前提，不要向系统 Python 全局安装 DeepResearch 依赖。

## 4. Harness 或 ACP 预检失败

统一入口：

```bash
deepresearch doctor --harness <hermes|codex|claude-code|openclaw> --json
```

### Hermes

检查错误是否属于：

- `hermes` 命令不存在；
- ACP 检查失败；
- Profile 不存在；
- 模型或登录未配置；
- Provider 认证或额度失败。

不要替用户创建或覆盖 Hermes Profile。Profile 名由用户确认后通过 `--harness-profile` 传入。

### Codex

检查错误是否属于：

- `codex` 命令不存在；
- `codex login status` 失败；
- Codex App Server 不可用；
- 指定模型不可用。

登录应由用户完成。不要读取或输出 Codex 凭据。

### Claude Code

检查 `claude-agent-acp` 是否存在，以及 Adapter 认证是否完成。Adapter 缺失时，在授权后安装：

```bash
npm install -g @agentclientprotocol/claude-agent-acp
deepresearch doctor --harness claude-code --json
# 仅当 doctor 确认没有可用认证时执行：
# claude-agent-acp --cli auth login
```

CLI 内含 Claude Code 适配代码，但不内置外部 ACP Adapter。

### OpenClaw

检查：

- `openclaw` 命令是否存在；
- Gateway 是否可用；
- Gateway 是否已经配置模型；
- ACP Bridge 是否可建立连接。

OpenClaw 模型由 Gateway 配置选择，不要通过单次研究命令强行覆盖。

### 判断边界

- `doctor` 失败发生在研究开始前，不应创建 Run。
- 认证、额度、模型不存在不是节点超时，不进入自动超时重试。
- ACP 启动失败和模型调用失败要分别报告。
- 不要因为一个 Harness 不可用就擅自切换到另一个 Harness。

## 5. Search 来源不可用或结果过少

### 检查

```bash
deepresearch sources list --json
deepresearch domains list --json
deepresearch sources describe <source-name> --json
deepresearch domains describe <domain-name> --json
```

必要时先初始化配置：

```bash
deepresearch sources init
```

判断顺序：

1. 确认 Research 启用了 Search MCP，没有传入 `--no-search-mcp`。
2. 确认 Domain 存在且至少包含一个运行时可用 Source。
3. 查看 Source 缺少的是模块还是环境变量名。
4. 只报告缺少的变量名，不读取或回显变量值。
5. 单个 Source 失败时检查批次是否仍为部分成功；不要立刻判定整个 Research 失败。
6. `Raw > 0` 但 `Fetched = 0` 表示候选已发现但正文读取尚未完成或未被选择，不代表搜索没有结果。
7. `Fetched > 0` 但 `Evidence = 0` 表示尚未发布合法 Evidence，需要检查 Research 输出和 Validator。

常见分类：

| 现象 | 优先判断 |
| --- | --- |
| 所有 Source 不可用 | Search 配置、Provider Python 或必需模块 |
| 只有公开来源可用 | 可选凭据未配置，CLI 仍可继续 |
| 单个 Provider 超时 | Provider 局部故障，其他来源继续 |
| HTTP 429 | 限流；不使用 Camofox 重试 |
| 403 或 JavaScript 空壳 | 符合规则时可尝试 Camofox |
| 候选重复很多 | 查看 Unique 去重结果，不按 Raw 数判断覆盖度 |

Search 配置默认由 `sources init` 创建。自定义注册表必须在研究、`doctor` 和诊断命令中使用相同的 `--search-dir` 与 `--search-provider-python`。

## 6. Camofox 不可用

### 检查

```bash
deepresearch browser status --json
```

仅在用户选择启用浏览器回退且允许下载资源后执行：

```bash
deepresearch browser setup
deepresearch browser start
deepresearch browser status --json
```

判断顺序：

1. 未安装：提示需要额外下载浏览器资源。
2. 已安装但未启动：启动后再次检查健康状态。
3. 启动失败：读取命令返回的日志路径，只查看必要的错误尾部。
4. 健康但单个网页失败：按 URL 安全规则和访问控制边界判断，不反复启动服务。
5. Camofox 不可用时，Research 应切换来源，不应因此阻塞整个 Workflow。

Camofox 不用于 CAPTCHA、登录、付费墙、带凭据 URL、私网目标或其他访问控制。

## 7. Web 页面打不开或看不到进度

### 检查启动方式

Normal 和 Heavy 通常通过以下入口启动：

```bash
deepresearch web "<query>" \
  --mode <normal|heavy> \
  --report-format <brief|formal_report> \
  --output-format <markdown|html|pdf|docx> \
  --harness <harness> \
  --host 127.0.0.1 \
  --port 8765 \
  --progress tools
```

确认终端已经输出监听成功，再提供 <http://127.0.0.1:8765>。

### 常见现象

| 现象 | 检查 |
| --- | --- |
| 首页无法连接 | Web 进程是否仍运行、监听地址和端口是否一致 |
| 端口已占用 | 确认占用者；使用其他端口，不启动重复研究 |
| 首页可开但没有 Run | Run 是否仍处于 starting、是否使用了相同 `runs-dir` |
| 直接 Run URL 可开而首页不更新 | 检查浏览器缓存、SSE 和首页当前 Run 选择逻辑 |
| Run API 返回 404 | `run_id`、`runs-dir` 或 Manifest 是否存在 |
| 页面显示持久化进度但终端已结束 | Web 只展示状态；使用 `status` 判断 Run，不假设执行器仍存活 |

Web 不负责从页面恢复已结束的执行进程。确认原进程已经结束后使用 CLI `resume`。

只有用户明确需要其他主机访问时才改变监听地址，并提醒进度和报告可能对网络中的其他用户可见。

## 8. Run 失败、停止更新或需要恢复

先读取持久化状态：

```bash
deepresearch status <run-id> --json
```

从状态中确认：

- Run 是 `running`、`completed` 还是 `failed`；
- 当前或失败的 Step、Node、scope 和 attempt；
- 错误是 timeout、Harness、Validator、Script 还是输入缺失；
- 最终结果 Artifact 是否已经发布。

attempt 诊断位于：

```text
runs/<run-id>/attempts/<instance-id>/attempt-<n>/
```

按需查看：

- `invocation.json`：Node ID、超时和声明的 Context；
- `stderr.log`：Harness 或进程错误；
- `raw-response.txt`：模型最后响应，仅在必要时读取；
- `validator*.stderr.log`：硬校验失败；
- `acp-events.jsonl`：经过裁剪的工具活动。

不要把整个 attempt 目录或可能含敏感内容的文件原样贴给用户。优先报告结构化错误和必要的短摘要。

### 超时

Agent 节点首次超时会标记为 `retryable`，Driver 使用全新 Session 自动执行 attempt-2。并发节点只重跑超时 scope。连续第二次超时才使 Run 失败。

如果 Run 已经因为连续超时失败，不要无限重试。先判断节点预算、输入规模、Provider 状态或任务范围是否需要调整，再取得用户同意后创建新 Run。

### Broken pipe、EOF 和连接重置

这类错误通常表示 Harness、ACP 或模型 Provider 的传输连接中断，不足以单独证明上下文过长或模型能力不足。应结合失败节点、stderr、Provider 状态和是否已有自动重试判断。

### Validator 或 Script 失败

- Validator 错误应包含规则和具体 scope/unit；不要把它当作网络瞬态错误。
- `repairable` 会在新 attempt 中注入定向修复信息。
- Script 错误是确定性失败，不使用模型或超时重试掩盖。

### 恢复

确认原执行进程已经结束后：

```bash
deepresearch resume <run-id> \
  --harness <harness> \
  --progress tools
```

若原 Run 使用了非默认目录、Harness Profile、模型或 Search 注册表，恢复时保持一致。已成功节点不会重跑。

## 9. 报告未生成或格式转换失败

先确认 Run 是否完成以及 Markdown 主报告是否存在：

```bash
deepresearch status <run-id> --json
```

判断：

- Run 未完成：先诊断失败节点，不单独重跑转换器。
- Markdown 已生成但 HTML 失败：检查 `md-html` Agent 和 Validator。
- DOCX 失败：确认 `pandoc` 可从 `PATH` 调用。
- PDF 失败：确认 `typst` 可从 `PATH` 调用。

只转换已有 Markdown：

```bash
deepresearch node run md-docx --input report=./report.md
deepresearch node run md-pdf --input report=./report.md
```

不要把中间 Draft、搜索候选或未通过 Validator 的文件当作最终报告。

## 10. 停止条件与诊断报告

遇到以下情况应停止自动操作并请求用户处理或授权：

- 需要登录、填写 token、Cookie 或付款信息；
- 需要修改 Harness 全局模型或 Profile；
- 需要安装或下载额外组件；
- 需要改变监听范围；
- 原研究进程是否仍存活无法确认；
- 连续超时后是否扩大预算或缩小任务范围需要用户选择。

向用户报告时使用以下结构：

```text
诊断结论：<一句话>
失败范围：<CLI / Harness / Search / Browser / Web / Node / Converter>
证据：<命令、状态和必要错误摘要>
已保留：<run_id、成功节点或 Artifact>
下一步：<一个最小操作；需要授权时明确说明>
```

禁止为了“试试看”执行删除 Run、清空配置、覆盖 Profile、关闭证书校验、绕过访问控制或并行启动同一研究。
