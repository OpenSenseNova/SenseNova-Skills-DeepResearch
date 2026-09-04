# Research 多来源搜索 MCP

本文说明 Research 节点如何选择来源、并发检索、读取正文、使用 Camofox 回退、记录指标并处理失败。

## 1. 目标与边界

Search MCP 的职责：

- 向 Agent 暴露稳定的领域和来源目录；
- 并发执行多个 Search Provider；
- 归一化、规范化 URL、去重和缓存候选；
- 按需读取候选正文；
- 在符合条件时执行代码级 Camofox 回退；
- 持久化搜索结果和可观测指标；
- 隔离不同 Research attempt 的权限与结果视图。

Search MCP 不负责：

- 替模型决定研究问题和证据结论；
- 绕过 CAPTCHA、登录、付费墙或其他访问控制；
- 把搜索摘要自动当作正式 Evidence；
- 保存模型供应商凭据。

## 2. 调用链

```text
Research Agent
  → Search MCP
    → Search Coordinator
      → Domain Registry
      → Source Registry
      → Provider Processes
      → Search Store
    → HTTP Fetcher
      → optional Camofox fallback
```

Research attempt 启动时，Harness 为该 attempt 注册专用 Search MCP。多个 Research attempt 可以共享 Run 级 Coordinator、缓存和并发预算，但每个 attempt 使用独立 token 和 namespace。

## 3. Domain 与 Source

Domain 描述面向研究主题的来源组合，例如学术、企业披露、金融市场、软件工程或通用网页。Source 描述具体 Provider 的执行方式。

Research 通常先调用：

```text
list_search_domains
  → start_domain_search
  → get_search_batch
```

Agent 根据 query、计划中的来源要求和研究维度选择 Domain。选择结果不是固定枚举生成，而是模型基于 Domain Registry 做出的研究决策。

Source YAML 可以声明：

- Provider 脚本；
- 支持的领域；
- 必需 Python 模块；
- 可选环境变量；
- Provider 超时；
- 并发限制；
- 默认候选数量。

没有满足配置要求的 Source 会在诊断中显示为不可用，不会阻止其他 Source 工作。

## 4. MCP 工具

### `list_search_domains`

列出领域 ID、描述、来源数量和可用状态。Agent 用它选择与当前 Research Task 相关的领域。

### `start_domain_search`

提交一个或多个领域查询。服务解析 Domain 中的 Source，创建批次并并发执行 Provider。

典型输入：

```json
{
  "requests": [
    {"domain": "academic", "query": "research topic"},
    {"domain": "general_web", "query": "official publication research topic"}
  ]
}
```

### `get_search_batch`

读取批次状态和摘要。结果可能是完成、部分成功、失败或仍在运行。

### `list_search_sources`

列出 Source 级配置和可用性，主要用于诊断或精细选择。

### `batch_search`

直接按 Source 提交查询，主要用于兼容和诊断。普通 Research 优先使用 Domain 工具。

### `search_results`

分页读取当前 attempt 可见的规范化候选。返回稳定的 hit ID，不把全部候选一次塞入模型上下文。

### `get_search_hit`

读取单个候选的完整元数据、Provider 来源和去重信息。

### `fetch_url`

读取公开 URL 正文。执行顺序固定为：

```text
ordinary HTTP
  → classify response
  → optional Camofox retry
  → normalized text result
```

## 5. 并发与缓存

并发由三层约束共同控制：

- Run 级 worker 上限；
- Domain 级并发限制；
- Source 级并发限制。

相同 Source 与规范化 query 的并发请求会合并。首个请求执行 Provider，其他请求复用结果。缓存保存在 Run Search Store 中，因此恢复 Run 时仍能复用已完成查询。

Provider 使用参数数组启动，不经过 shell。每个 Provider 只获得其 Source 声明且用户已配置的环境变量。

## 6. 归一化与去重

Provider 原始条目会转换为统一结构，包括：

- 标题；
- URL；
- 摘要；
- 来源与 Provider；
- 发布时间（如可用）；
-排名和其他可选元数据。

URL 规范化会移除常见跟踪参数、统一主机和默认端口、处理 fragment，并生成稳定去重键。多个 Provider 命中同一目标时保留来源关联，但只产生一个 Unique candidate。

Search Store 区分：

```text
Raw candidate
  → normalized candidate
  → unique candidate
  → fetched content
  → evidence source
```

## 7. 为什么必须读取正文

搜索结果只用于发现候选，摘要不能自动升级为 Evidence。Research 应选择高价值候选调用 `fetch_url`，核对原文中的事实、口径、发布日期和上下文，再把来源写入 `evidence.json`。

Evidence Validator 负责检查结构和引用关系，但事实判断仍由 Research Agent 基于已读取材料完成。

## 8. HTTP 与 Camofox

普通 HTTP 是默认读取方式。只有响应满足代码规则时才触发一次 Camofox：

- 明确访问拒绝；
- 反自动化中间页；
- 需要 JavaScript 渲染但正文为空；
- 普通响应可识别为浏览器可恢复失败。

以下情况不使用 Camofox：

- HTTP 429；
- CAPTCHA；
- 登录要求；
- 付费墙；
- 带凭据 URL；
- 指向回环、私网、链路本地或保留地址的 URL；
- 不允许的协议；
- 已经取得有效正文。

Camofox 未安装、未启动或读取失败时，`fetch_url` 返回明确错误，Research 应切换来源，不应阻塞整个 Workflow。

管理命令：

```bash
deepresearch browser setup
deepresearch browser start
deepresearch browser status
deepresearch browser stop
```

## 9. 配置

初始化用户 Search 配置：

```bash
deepresearch sources init
```

查看来源和领域：

```bash
deepresearch sources list --json
deepresearch domains list --json
```

默认目录结构：

```text
search/
├── .env
├── sources/
│   └── *.yaml
├── domains/
│   └── *.yaml
└── providers/
    └── *.py
```

`.env` 保存可选 token、cookie、User-Agent 和代理值，并应保持在版本控制之外。Source YAML 只声明变量名称。

为单次运行指定自定义注册表：

```bash
deepresearch "研究问题" \
  --mode normal \
  --report-format formal_report \
  --harness hermes \
  --search-dir /path/to/search \
  --search-provider-python /path/to/python
```

`sources list` 检查声明、模块和配置项是否满足，不向外部 API 发送真实业务查询。

## 10. 持久化与 attempt 隔离

Coordinator 为每个 Research invocation 创建：

- 独立 namespace；
- 独立短期 token；
- 独立 Search MCP Server 名称；
- 受 attempt lease 控制的执行权限。

一个 attempt 的 token 不能读取另一个 attempt 的 namespace。attempt 结束或超时后 lease 失效，未完成的 Provider 工作会被取消。

Run 级数据库用于缓存、候选和指标，不包含 Harness 模型凭据。

## 11. 可观测性

Web 和 benchmark 可以读取以下指标：

- API/Provider 调用次数；
- Raw candidate 数；
- Unique candidate 数；
- Fetched 数；
- 进入 Evidence 的来源数；
- Cache reuse 数；
- Domain 和 Source 的完成、失败、超时状态。

终端工具事件与 Search Store 的统计口径不同：工具事件表示 Agent 发起和完成了什么调用；Search Store 表示 Provider 实际执行、去重和缓存后的结果。Web 应使用持久化 Search 指标展示检索漏斗。

## 12. 故障语义

| 故障 | 行为 |
| --- | --- |
| 单个 Provider 失败 | 保留其他 Provider 结果，批次可部分成功 |
| Provider 超时 | 终止对应进程并记录超时，其他查询继续 |
| 批次超时 | 返回已完成结果并标记未完成项 |
| Source 缺少配置 | Source 不可用，其他 Source 继续 |
| HTTP 读取失败 | 按规则尝试 Camofox 或切换来源 |
| Camofox 不可用 | 返回明确失败，Research 切换来源 |
| Research 节点超时 | attempt 终止，Driver 使用全新 Session 重试一次 |
| 认证或额度错误 | 不按节点超时重试，返回明确错误 |

Search Provider 自身的失败不会自动使整个 Run 失败；Research 是否能形成合法 Evidence 由节点输出和 Validator 决定。

## 13. 安全边界

- 只允许 HTTP 和 HTTPS 公网目标。
- 拒绝带凭据 URL 和受保护地址范围。
- 重定向后重新执行目标校验。
- Provider 以无 shell 子进程启动。
- 只透传 Source 声明的环境变量。
- MCP token 按 attempt 隔离。
- Search 结果和网页正文不获得执行权限。
- Camofox 不自动点击、登录、导入 Cookie 或解决 CAPTCHA。
