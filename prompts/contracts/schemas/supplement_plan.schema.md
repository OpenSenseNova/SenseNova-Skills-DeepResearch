# `supplement_plan.json` 补研计划字段契约

`{report_dir}/sub_reports/d{N}.supplement_plan.json` 记录某个现有研究维度的补研决定。

本文档列出补研任务派生与完成状态消费的正式字段。不影响生命周期的扩展字段不阻断工作流。
已退役的 `meta` 会被明确拒绝；输入来源、日期和报告目标由 Runtime Context 持有。

## 顶层对象

```json
{
  "dimension_id": "d1",
  "supplement_items": [...],
  "deferred_items": [...]
}
```

## 维度字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `dimension_id` | 字符串 | 是 | 已存在的维度 ID，例如 `d1` |
| `supplement_items` | 数组 | 是 | `research` 补研模式需要执行的事项；可以为空 |
| `deferred_items` | 数组 | 是 | 不作为补研任务执行的事项；可以为空 |

## `supplement_items[]`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 是 | 稳定 ID，例如 `d1-s1`、`d1-s2` |
| `type` | 枚举 | 是 | `coverage` / `claim_fix` / `both` |
| `gap` | 字符串 | 是 | 简要说明证据缺口或断言质量缺口 |
| `question` | 字符串 | 是 | 交给 `research` 智能体回答的具体问题 |
| `rationale` | 字符串 | 是 | 说明这项补研为什么重要 |
| `suggested_sources` | 字符串数组 | 是 | 建议的来源类别或具体来源类型 |
| `candidate_leads` | 字符串数组 | 是 | 输入文件中已有的候选 URL、来源名称或搜索线索；可以为空。|
| `source_refs` | 字符串数组 | 是 | 提出该事项的审查或视角反馈位置 |
| `review_refs` | 字符串数组 | 是 | 涉及的断言 ID 或审查要点；纯覆盖型事项可以为空 |
| `impact_if_skipped` | 字符串 | 是 | 如果跳过，最终报告应受到什么限制 |
| `status` | 枚举 | 是 | 初始值为 `pending`；`research` 后续更新为 `resolved` / `partial` / `no_data` / `out_of_scope` |
| `resolution_note` | 字符串 | 是 | 初始为空；`research` 执行后填写 |

## 生命周期状态

- `planned`：SupplementPlanner 的新计划。所有 `supplement_items[]` 都是 `pending`，且
  `resolution_note` 为空。
- `completed`：supplement Research 的执行结果。计划中的每个 item id 都必须出现在完成态副本
  中，不能残留 `pending`；每项必须填写非空 `resolution_note`。item 的说明可在执行中作合法纠正，
  机械校验不冻结其全部字段。
- `partial`、`no_data`、`out_of_scope` 必须分别在新 evidence 的 `writing_context[]` 中保留
  `unresolved_gap`、`availability_gap`、`scope_boundary`。完成态计划记录执行审计，正式证据边界
  仍以新 evidence 为准。
- 输入计划和旧 evidence 是只读文件；Research 将完整的新 evidence 与完成态计划分别写到 Runtime
  指定的新输出路径。

## `deferred_items[]`

该数组用于记录不应触发补研的候选事项。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 是 | 稳定 ID，例如 `d1-d1` |
| `reason` | 枚举 | 是 | `writing_context_only` / `low_value` / `not_actionable` / `out_of_scope` / `already_covered` / `unavailable` |
| `item` | 字符串 | 是 | 简要说明被延后的候选事项 |
| `source_refs` | 字符串数组 | 是 | 提出该事项的审查或视角反馈位置 |
| `writing_context_use` | 字符串 | 是 | 说明如何呈现；不适用时填写空字符串 |

## 空计划

如果无需补研，写入一份合法的空计划：

```json
{
  "dimension_id": "d1",
  "supplement_items": [],
  "deferred_items": []
}
```

运行时只在 `supplement_items[]` 非空时自动生成一个 `research-tasks/<dimension_id>.json`；Agent 不写
任务副本。
