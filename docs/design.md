# SenseNova-Skills-DeepResearch 配置运行时设计

本文描述当前运行时的稳定设计：配置模型、Workflow 编译、节点调度、Artifact 发布、持久化、ACP Harness、Search MCP 和扩展边界。

## 1. 设计目标

- Workflow 拓扑和节点合同由配置定义，而不是散落在业务代码中。
- Agent 只处理研究与写作判断；拼接、格式检查、转换和持久化由确定性代码负责。
- 每个节点只读取声明的输入并只发布声明的输出。
- 并发、失败、修复、重试和恢复都具有可重复的状态语义。
- 四个生产 Harness 共享同一执行合同。
- Search、浏览器回退和模型执行彼此解耦。

## 2. 配置模型

### 2.1 Workflow

Workflow YAML 定义执行顺序、Agent 节点超时和最终结果类型：

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

约束：

- `steps` 中每个值必须对应已注册 Node ID。
- 同一 Node ID 可以重复出现，编译后得到 `research`、`research-2` 等 Step ID。
- `timeouts` 的 key 是 Node ID，不是重复后的 Step ID。
- 超时只允许配置给 Agent 节点。
- `result` 必须能由编译后的某个主输出满足。

输出格式会在编译阶段补充转换节点：

- `markdown`：使用 Workflow 的 `report` 主输出；
- `html`：追加 `md-html`；
- `pdf`：追加 `md-pdf`；
- `docx`：追加 `md-docx`。

### 2.2 Node

Node YAML 声明：

- `id` 和 `kind`；
- 输入端口与输出端口；
- Agent Prompt Bundle Key 或 Script Command；
- Preparer、Materializer 和 Validator；
- 运行时资源；
- 输出是否为 `state`、`batch` 或普通 Artifact。

Agent 节点示意：

```yaml
version: 1
id: research
kind: agent
prompt_bundle_key: Research
inputs:
  task:
    type: research-task
    media_type: application/json
    mode: each
outputs:
  evidence:
    path: evidence.json
    type: evidence
    media_type: application/json
    mode: state
validator: [python, -m, deepresearch_cli.node_validators.evidence]
```

Script 节点示意：

```yaml
version: 1
id: render
kind: script
command: [python, -m, deepresearch_cli.render_node]
```

### 2.3 Search Source 与 Domain

Source YAML 描述单个搜索能力，包括命令、必需模块、环境变量、超时和并发限制。Domain YAML 将若干 Source 组织成面向主题的来源集合。

配置文件不保存凭据值，只声明所需环境变量名。用户值由独立的 Search 配置文件提供。

## 3. 编译

Compiler 将 Workflow 和 Node Registry 编译为不可歧义的执行计划：

1. 校验所有 Node ID。
2. 为重复节点生成稳定 Step ID。
3. 根据端口类型、媒体类型和 scope 模式绑定输入。
4. 根据输出格式追加转换节点。
5. 校验 Agent 超时配置。
6. 确认最终主输出存在且媒体类型正确。

编译结果只包含运行所需信息，不依赖后续重新读取源 YAML。

## 4. Scope 与并发

输入端口的 mode 决定调度方式：

- `one`：读取当前 scope 或全局的最新 state；
- `all`：读取该类型的全部当前 state；
- `each`：按上游 batch Artifact 的 scope 扇出节点实例。

例如 Plan 发布多个 `research-task` 后，Research 为每个 `dimension-id` 生成独立实例。Driver 通过全局 semaphore 控制并发数量。

每个实例拥有稳定的 `instance_id`，每次执行拥有递增的 `attempt`。同一 Step 的不同 scope 可以并发，但不会共享 attempt workspace。

## 5. Node Context

NodeRunner 为每次 attempt 生成统一 Context：

```json
{
  "run": {},
  "step": {},
  "scope": {},
  "inputs": {},
  "outputs": {},
  "resources": {},
  "prompt": {}
}
```

- `run`：query、模式、语言、报告形式和输出格式；
- `step`：Step ID、Node ID、Instance ID 和 attempt；
- `scope`：研究维度或写作单元标识；
- `inputs`：已经通过引用校验的输入 Artifact；
- `outputs`：允许写入的 staging 路径；
- `resources`：当前节点可见的 schema、模板或其他资源；
- `prompt`：从权威 Context 派生的稳定别名。

Prompt 中附带 Runtime Node Contract。Agent 必须使用 Harness 的原生写文件能力写入声明路径，不得修改受信任 Preparer 产物。

## 6. Attempt 隔离与 ACP Harness

生产 Harness：

- Hermes ACP；
- Codex ACP Bridge；
- Claude Code ACP Adapter；
- OpenClaw ACP Gateway。

它们统一实现：

```text
BackendFactory
  → AttemptRuntime
  → AgentInvocation
  → AgentExecutionResult
```

每个 attempt 创建独立运行时或独立 Session。Harness 配置、模型选择、Search MCP 注册和进程生命周期不进入业务 Artifact。

`AgentExecutionResult` 统一承载状态、响应、工具事件、usage、stderr、原生 Session 标识和结构化失败类型。NodeRunner 不需要了解具体 Agent 的协议细节。

## 7. 超时、重试与修复

Agent 节点的有效超时按以下优先级确定：

1. Workflow `timeouts` 中的 Node ID；
2. `--node-timeout-seconds` 兜底；
3. 未配置且关闭兜底时不设置节点超时。

Script 节点不接受 Workflow 超时。

节点结果状态：

| 状态 | 含义 | 后续行为 |
| --- | --- | --- |
| `succeeded` | 已发布合法 Artifact | 继续下游 |
| `repairable` | Agent 产物缺失或未通过允许修复的校验 | 新 attempt 注入定向修复信息 |
| `retryable` | 首次节点超时 | 新 attempt 使用干净 Context 和全新 Session |
| `interrupted` | 执行进程中断，attempt 未完成 | 恢复时新建 attempt |
| `failed` | 不可恢复错误或连续超时 | 结束 Run |

超时重试总计最多两个 attempt。并发节点中，成功 scope 的 Artifact 保留，只重跑 `retryable` scope。`retryable` 不注入内容修复 Prompt。

## 8. Candidate、Validator 与发布

节点执行完成后不会直接把 staging 当作正式结果：

```text
staging
  → freeze candidate
  → seal candidate
  → match declared outputs
  → run validators
  → verify candidate unchanged
  → publish artifacts
```

Validator 只能读取 candidate，不能修改它。发布时记录路径、SHA-256、Artifact 类型、媒体类型、scope、Step ID 和 Instance ID。

Preparer 用于生成受信任输入或预拼接内容；Materializer 用于把 Agent 结果转换为确定性结构。两者都在 Node 合同中显式声明。

## 9. Artifact 与状态

Artifact 是节点之间唯一的正式业务数据接口。`state` Artifact 以“类型 + scope”为键，后发布的合法 Artifact 代表当前状态；历史 Artifact 仍保留用于审计。

典型链路：

```text
research-task → evidence → review → supplement-plan
report-outline → content-task → report-draft
report-draft → stitched-report → report
```

Driver 只从已发布 Artifact 绑定下游输入，不读取其他 attempt 的 staging 临时文件。

## 10. 持久化与恢复

Run 包含：

- Manifest：请求、编译后的 Workflow、Node Spec 快照和定义指纹；
- Journal：追加写入的 Step started/finished 与 Run finished 事件；
- Artifacts：通过校验的正式产物；
- Attempts：按 Instance 和 attempt 隔离的诊断文件；
- Search：Run 级搜索数据库和协调器状态。

Journal 通过严格序列验证重建 `RunProjection`。恢复时只使用 Manifest 中的快照，不重新编译当前配置，因此配置变化不会改变已有 Run 的语义。

已经成功的实例不会重跑。未完成、`repairable` 或 `retryable` 实例从新的 attempt 继续。

## 11. Heavy 的确定性收口

Heavy 写作完成后：

1. Stitcher 按 Outline 确定性拼接 Content Units。
2. 代码检查标题、引用和 render contract。
3. FinalReviewDiagnostic 输出明确的 Unit 级问题。
4. 只有被点名的 Unit 执行 FinalRepair。
5. 受信任 Preparer 用修复稿替换对应 Unit 并重新拼接。
6. FinalReviewRecheck 只复查一次。
7. Render 生成最终 Markdown。

模型不负责自由拼接全文，也不能凭感觉替代硬校验。

## 12. Search 边界

Research attempt 可以获得专用 Search MCP。Search Coordinator 在 Run 内共享缓存和 Provider 并发预算，但每个 attempt 使用独立凭据和 namespace。

模型负责选择领域、构造查询和决定读取哪些候选；代码负责执行 Provider、去重、缓存、正文读取、安全校验和指标统计。详细合同见 [Search MCP](search-mcp.md)。

## 13. Web 进度

Web 不从模型文本猜测进度，而是读取持久化状态：

- Workflow 和实例完成情况来自 Manifest 与 Journal；
- 来源、读取和 Evidence 数量来自 Search 数据与 Artifact；
- 写作单元数量来自 ReportPlanner 产物；
- 活动流来自经过裁剪的工具事件。

SSE 只负责通知更新；页面刷新后仍可从持久化数据重建。

## 14. 扩展规则

新增能力时优先遵循以下顺序：

1. 定义 Artifact 合同。
2. 增加 Node YAML。
3. 为结构化输出增加 Validator。
4. 将可确定执行的逻辑放入 Script、Preparer 或 Materializer。
5. 在 Workflow 中组合节点并配置 Agent 预算。
6. 增加编译、调度、失败和恢复测试。

避免：

- 在 Driver 中按业务节点名称硬编码内容逻辑；
- 让 Agent 自行发现未声明路径；
- 让 Validator 修改候选产物；
- 在 Artifact 中保存 Harness 凭据或进程状态；
- 通过无限重试掩盖确定性错误。
