# 维度视角反馈 Markdown 契约

每个 Perspective 节点只发布一份当前研究维度的汇总文件。文件按 Plan 中的顺序覆盖该维度
全部 lenses；不再为每个 lens 生成独立文件。

它不是正式证据。新的事实线索必须由补研 Research 复核并写入 evidence，才能支撑报告断言。

## 必备章节

Plan 中的 `lenses[]` 不包含 ID。按数组的一基位置派生稳定 ID：第 1 项是 `l1`，第 2 项是 `l2`，
依此类推。每个 Lens 标题必须逐字符使用 `### l{N}: {axis}:{value}`；不得添加 dimension 前缀，
也不得改写成 `lens-1`、`lens_1` 等近义形式。

```markdown
# Perspective Summary: {dimension_id}

## Lens Reviews

### l{N}: {axis}:{value}

#### Lens 定位

#### 写作补充边界（非正文主张）

#### 需要补研后才能使用

#### 探索性搜索线索

## 维度内补研需求

## 写回摘要
```

`lenses: []` 时仍写出全部二级章节，并在 `Lens Reviews` 下明确“当前维度没有已声明 lens”。

## Lens 章节

每个 lens 恰好出现一次，并包含：

- 按 Plan 数组一基位置派生的稳定 ID `l{N}`、精确复制的 `axis:value` 和 rationale；
- 已审阅 evidence 的边界；
- 仅可作为 writing context、表注或限制说明的内容；
- 必须经过补研后才能使用的具体问题；
- 可选的探索性来源线索，且明确标记为非证据。

不得把探索性搜索结果写成已验证事实，也不得在此文件中补写正式 evidence。

## 维度内补研需求

使用以下表格：

| lens | 缺口 | 补研问题 | 建议来源 | 候选线索 | 不补研的影响 |
| --- | --- | --- | --- | --- | --- |

相同缺口可合并，但必须保留涉及的 lens。没有必要补研时原样写：`无必要补研。`

## 写回摘要

提供 3–6 条简短要点，区分补研需求、writing context 边界和无需补研的决定。

## 消费边界

- `supplement-planner` 读取这一份维度汇总与 review、evidence。
- 报告阶段不得把 Perspective 当作正式证据。
- 未解决内容必须先进入补研 evidence 或 evidence.writing_context，才能影响成品。
