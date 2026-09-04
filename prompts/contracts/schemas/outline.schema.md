# 报告编排字段契约

报告编排阶段的字段契约。最终产物由有序的 `content_units[]` 组成。`report_profile` 保存下游 Writer 必须执行的报告形式和内容模板选择；`paradigm` 表示内容推进方式，`organization_decision` 表示核心信息的承载结构。请求级 `output_format` 仅表示文件容器，由 payload 传入，不复制到 `outline.json`。

本文档列出 Content Task 派生、Writer 和 Render 消费的正式字段。不影响这些消费端的扩展字段不阻断；`evidence_subset` 等 Runtime 自有字段仍禁止 Agent 写入。
`depth_level`、`claim_routing_table`、`supporting_unit_types`，以及 `scan_summary` 中已移除的派生统计字段会被明确拒绝。

## 文件位置

```text
{report_dir}/outline.json
{report_dir}/content_units/{unit_id}.evidence_subset.json
{report_dir}/content_units/{unit_id}.md
```

## 顶层结构

```json
{
  "report_profile": { "format": "formal_report", "template_id": "market_analysis" },
  "paradigm": { "main": "comparison", "secondary": "evaluation" },
  "global_arc": "从用户的选择问题出发，先统一比较口径，再用现有证据呈现差异、冲突和适用边界，最后给出有条件的判断。",
  "organization_decision": { "...": "见下文" },
  "L0_draft": { "...": "见下文；也可以为 null" },
  "style_contract": { "...": "见下文" },
  "content_units": [ "..." ],
  "scan_summary": { "...": "见下文" }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `report_profile` | 对象 | 已确认的报告形式，以及正式报告采用的内容模板 |
| `paradigm` | 对象 | 内容推进范式，不决定产物结构 |
| `global_arc` | 字符串 | 非空的全文方向和证据边界，不设置字符数限制 |
| `organization_decision` | 对象 | 证据完成后的结构决定 |
| `L0_draft` | 对象 / `null` | 是否存在由 `opening_summary` 决定 |
| `style_contract` | 对象 | 体裁、语气、术语和引用约定 |
| `content_units` | 数组 | 至少 1 个有序交付单元，不设机械上限 |
| `scan_summary` | 对象 | 需要进入成品的冲突和证据缺口 |

## `report_profile`

```json
{
  "format": "brief|formal_report",
  "template_id": "market_analysis"
}
```

- `format` 必须与请求级 `report_format` 完全一致，不得根据证据量或 `output_format` 改写。
- `format=formal_report` 时，`template_id` 必须是 `report_templates_path` 所指模板目录中的一个 ID。
- `format=brief` 时，`template_id` 必须为 `null`；简报不套正式报告内容模板。
- 模板的 `required_elements`、`planning_rules` 和 `writing_rules` 是语义覆盖要求，不是必须逐字使用的章节标题。

## `paradigm`

`main` 必须取以下枚举之一；`secondary` 必须为 `null` 或同一枚举中的另一个值：

```text
panorama | comparison | investigation | timeline | evaluation | forecast
```

- `panorama`：建立主题全貌、组成与关系。
- `comparison`：按共同口径比较对象或方案。
- `investigation`：围绕争议、原因、证据链或未决问题推进。
- `timeline`：按时间、阶段或关键转折推进。
- `evaluation`：依据标准形成受证据约束的评价。
- `forecast`：从基线、驱动因素与条件形成前瞻判断。

`main` 必须为非空字符串，与非空 `secondary` 不得相同。推荐使用上述标准值，但合理的扩展值不会阻断下游。
历史演进使用 `timeline`，因果或证据链调查使用 `investigation`。

## 结构示例

```json
{
  "reader_task": "让采购负责人按同一口径比较三个方案，并识别在不同约束下的适用边界",
  "opening_summary": "recommendation",
  "toc": false,
  "numbered_headings": false
}
```

### 内容单元类型

基础枚举：

- `narrative`：连续论述。
- `matrix`：实体乘维度的二维比较。
- `timeline`：按时间或阶段组织的事件链。
- `checklist`：逐项核对状态、要求或完成度。
- `scorecard`：按标准给出等级、分数或判断。
- `qa`：独立问题与回答。
- `callout`：关键事实、冲突、缺口或限制。
- `diagram`：流程、因果、关系或系统结构。
- `custom`：用户定义的其他结构；必须通过 `render_contract.instructions` 说明。

`opening_summary` 取 `none|findings|recommendation`。`toc` 与 `numbered_headings` 必须由已确认形式决定，不能使用报告式默认值。Supporting 类型直接从 `content_units[role=supporting].type` 得到，不重复声明。

## `L0_draft`（L0 草稿）

- `opening_summary=none` 时，`L0_draft` 必须为 `null`。
- `opening_summary=findings|recommendation` 时，`L0_draft` 必须存在。

```json
{
  "headline": "三个方案的最优选择取决于规模门槛与部署约束",
  "key_findings": [
    "方案甲在大规模负载下成本最低，但前期部署与迁移要求最高",
    "方案乙在中等规模下保持成本和交付速度的平衡，证据覆盖最完整",
    "方案丙适合快速启动，但长期成本与扩展性数据仍存在明显缺口"
  ],
  "abstract_visual": {
    "form": "comparison-table",
    "data_refs": ["d1.c1", "d2.c1", "d3.c1"]
  }
}
```

约束：`headline` 必须非空；`key_findings` 是非空字符串数组，不设机械数量限制；`abstract_visual` 的事实型 `data_refs` 必须是有效的断言 ID，并进入至少一个 element 的 `evidence_refs`。

`abstract_visual.form` 只能取以下枚举之一：

```text
bar-chart | distribution-chart | comparison-table | metric-strip |
timeline | flowchart | quadrant-chart | key-fact-callout |
evidence-conflict-callout | evidence-gap-callout | entity-profile-card |
concept-illustration | source-image
```

不要自造近义值；例如分层架构图应使用 `flowchart` 或
`concept-illustration`，不能写 `layered-architecture-diagram`。除
`concept-illustration` 外，事实型 visual 的 `data_refs` 至少包含一个有效 claim ID。

## `style_contract`（样式契约）

```json
{
  "register": "executive_memo",
  "voice": "declarative_executive",
  "terminology": {
    "preferred": {
      "总拥有成本": ["TCO", "全周期成本"]
    }
  }
}
```

枚举：

- `register`：`research_brief|academic|executive_memo|industry_report|policy_analysis`
- `voice`：`neutral_analytical|hedged_scholarly|declarative_executive|opinionated_supported`
引用样式由确定性的 Render 阶段统一采用脚注，不在 Outline 中重复声明。

## `content_unit`（内容单元）

```json
{
  "id": "u1",
  "type": "matrix",
  "role": "primary",
  "title": "三个方案的核心指标与适用边界",
  "reader_task": "按一致口径比较成本、交付、扩展性与主要风险",
  "lead": "三个方案没有脱离场景的统一最优解；规模门槛和交付约束会改变排序。",
  "render_contract": {
    "mode": "markdown_table",
    "show_heading": true,
    "schema": ["方案", "成本", "交付周期", "扩展性", "适用边界"],
    "instructions": "用一张主矩阵承载所有同口径结果；每格只写结论和必要引用，口径差异放表注。",
    "citation_policy": {
      "scope": "element",
      "require_each_claim": true,
      "required_fields": ["成本", "交付周期"]
    },
    "secondary_structure": {
      "allowed": true,
      "required": true,
      "heading_level": 3
    }
  },
  "elements": [
    {
      "id": "e1",
      "label": "方案甲",
      "purpose": "呈现方案甲在统一指标下的结果与限制",
      "evidence_refs": [
        { "claim_id": "d1.c1", "role": "primary_support" },
        { "claim_id": "d1.c2", "role": "counter" }
      ],
      "writing_context_refs": ["d1.w1"]
    }
  ]
}
```

### 通用字段

| 字段 | 约束 | 说明 |
|---|---|---|
| `id` | `^u\d+$` | 内容单元的唯一 ID |
| `type` | 内容单元枚举 | 信息语义，不强制具体 Markdown 渲染方式 |
| `role` | `primary|supporting` | 主体或补充结构 |
| `title` | 非空字符串 | 可展示标题；是否显示由渲染契约决定。`numbered_headings=true` 且显示标题时，标题本身必须带稳定序号 |
| `reader_task` | 非空字符串 | 读者使用该内容单元完成什么任务，不要求写成问句 |
| `lead` | `null` 或非空字符串 | 需要先给结论时使用；结构件不需要开场时为 `null` |
| `render_contract` | 对象 | Markdown 形态和字段契约 |
| `elements` | 数组 | 内容单元内的行、问题、事件、检查项、论点或其他可执行元素；可为空 |

### 渲染契约

```json
{
  "mode": "prose|markdown_table|ordered_list|checklist|qa|callout|mermaid|mixed|custom",
  "show_heading": true,
  "schema": ["字段或列名"],
  "instructions": "非空的具体渲染约束",
  "citation_policy": {
    "scope": "unit|element",
    "require_each_claim": true,
    "required_fields": ["仅 markdown_table 可用的列名"]
  },
  "secondary_structure": {
    "allowed": true,
    "required": true,
    "heading_level": 3
  }
}
```

- `mode` 与 `type` 不做硬编码映射。`timeline` 可渲染为表格、列表或 Mermaid；`investigation` 也可以使用 `diagram` 或 `narrative`。
- `schema` 是去重字段名数组，不设机械数量上限。矩阵可以填写列名，`timeline` 可以填写事件字段，`narrative` 可以留空。
- `instructions` 必须说明本内容单元如何承载主要信息，不能只写“按要求输出”。
- `citation_policy.scope` 决定 claim 的合法引用检查范围。默认业务选择 `element`；只有不可机械分割的整体结构才使用 `unit`。
- `require_each_claim=true` 时，每条 routed claim 的至少一个 evidence source 必须在检查范围内以 `[^source_id]` 出现。
- `required_fields` 只用于 `markdown_table`，必须是 `schema` 的子集；命中的每个元素行中，这些列必须有行内引用。无此要求时使用空数组。
- `secondary_structure.allowed=false` 时禁止 H3/H4，且 `required=false`、`heading_level=null`。
- `secondary_structure.allowed=true` 时只允许与 element label 完全一致的 H3，`heading_level=3`；`required=true` 还要求所有 element 各出现一次且顺序一致。

### 元素与证据边界

每个元素：

- `id`：内容单元内唯一，匹配 `^e\d+$`。
- `label`：非空字符串。
- `purpose`：非空字符串。
- `evidence_refs`：0–10 条，每条包含合法的 `claim_id` 和叙事角色。为空时，`writing_context_refs` 必须非空，并且只能表达有记录支撑的证据缺口。
- `writing_context_refs`：可选的 `dN.wM` 数组，不设机械数量上限。

`evidence_refs[].role` 沿用：`primary_support|supporting_context|quantifier|counter|reference_only`。

边界是硬约束：

1. 断言与写作上下文可为空；Writer 仍可依据单元指令撰写过渡性内容。
2. Report Planner 输出中不得出现派生证据子集；materializer 根据引用生成独立任务文件。
3. 单个内容单元派生任务中的断言，必须与所有 `elements[].evidence_refs[].claim_id` 的去重并集完全相同，不设机械数量上限。
4. 写作智能体只能读取和引用自己的证据子集，不得从其他内容单元或完整证据中补充材料。

## `scan_summary`（扫描摘要）

```json
{
  "conflicts": [
    {
      "issue": "两组证据对同一问题给出不同结论",
      "severity": "medium",
      "claim_ids": ["d1.c1", "d2.c1"],
      "surface_in": "u1"
    }
  ],
  "gaps": []
}
```

`conflicts[]` 与 `gaps[]` 中每一项都必须严格包含 `issue`、`severity`、
`claim_ids`、`surface_in`；`severity=low|medium|high`。不影响下游读取的扩展字段不阻断。

只保存下游 Final Review 实际使用的 `conflicts[]` 与 `gaps[]`。数量、来源比例、主题簇、实体和时间密度可从 Evidence 计算，不复制到 Outline。

## `evidence_subset.json`（证据子集）

```json
{
  "claims": [
    {
      "id": "d1.c1",
      "text": "...",
      "kind": "factual",
      "polarity": "neutral",
      "topic_tag": "cost",
      "narrative_role": "primary_support",
      "evidence": ["..."]
    }
  ],
  "writing_context": [],
  "sources": []
}
```

输出规则：

- 内容单元身份由文件名 `{unit_id}.evidence_subset.json` 与运行时 scope 唯一确定。
- `claims`、`writing_context` 与 `sources` 中的对象必须具备合法字段；传入原始证据时，断言和写作上下文 ID 必须存在。
- `claims` 与 `writing_context` 必须精确等于本内容单元元素实际引用的对象，不允许额外对象。
- `sources` 必须精确等于这些对象引用的来源 ID，不允许额外来源。
- 该文件由运行时生成，Report Planner 不得手工创建。

## 最小示例

```json
{
  "report_profile": { "format": "brief", "template_id": null },
  "paradigm": { "main": "evaluation", "secondary": null },
  "global_arc": "围绕用户需要作出的选择，按统一标准核对关键证据、相反信息和适用边界，给出受证据强度约束的判断。",
  "organization_decision": {
    "reader_task": "快速核对方案是否满足关键条件，并看到每项判断的证据边界",
    "opening_summary": "none",
    "toc": false,
    "numbered_headings": false
  },
  "L0_draft": null,
  "style_contract": {
    "register": "research_brief",
    "voice": "neutral_analytical",
    "terminology": { "preferred": {} }
  },
  "content_units": [
    {
      "id": "u1",
      "type": "checklist",
      "role": "primary",
      "title": "关键条件核对",
      "reader_task": "逐项确认关键要求是否满足以及证据是否充分",
      "lead": null,
      "render_contract": {
        "mode": "checklist",
        "show_heading": true,
        "schema": ["条件", "状态", "依据", "限制"],
        "instructions": "每项只给满足、不满足或证据不足三种状态，并在同一项内附引用和限制。",
        "citation_policy": {
          "scope": "element",
          "require_each_claim": true,
          "required_fields": []
        },
        "secondary_structure": {
          "allowed": false,
          "required": false,
          "heading_level": null
        }
      },
      "elements": [
        {
          "id": "e1",
          "label": "条件甲",
          "purpose": "核对条件甲是否满足并呈现证据限制",
          "evidence_refs": [
            { "claim_id": "d1.c1", "role": "primary_support" }
          ],
          "writing_context_refs": []
        }
      ]
    }
  ],
  "scan_summary": {
    "conflicts": [],
    "gaps": []
  }
}
```
