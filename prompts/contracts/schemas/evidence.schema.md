# 证据字段契约

`evidence.json` 的字段与引用完整性契约。每个研究维度产出一份文件。

本文档列出下游消费的正式字段、引用和状态关系。不影响下游读取的额外元数据不阻断工作流。
已退役的 `claims[].answers_key_question` 会被明确拒绝。

## 文件位置

```
{report_dir}/sub_reports/{dimension_id}.evidence.json
```

例如 `d1.evidence.json`、`d3.evidence.json`。

## 顶层结构

```json
{
  "dimension_id": "d1",
  "headline": "2024 年中国半导体设备国产替代率及成熟、先进制程分项数据",
  "key_findings": [ ... ],
  "claims": [ ... ],
  "sources": [ ... ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `dimension_id` | 字符串 | 形如 `d1`、`d2`。必须与 `plan.json` 中的维度 ID 对应。 |
| `headline` | 字符串 | 非空。中性概括本维度包含的主要研究对象与信息类型，不写中心论点或总评。 |
| `key_findings` | 数组 | 作为核心资料索引，每项通过 `claim_ids` 指向对应记录；不设机械数量限制。 |
| `claims` | 数组 | 至少 1 条资料记录。研究取得的全部可核验信息都在这里。 |
| `sources` | 数组 | 至少 1 条来源。本维度引用的全部来源。 |
| `writing_context` | 可选数组 | 可选。保存口径、方法、范围或可得性边界等辅助研究结果表述的信息。 |

## `key_findings`（资料索引层）

`key_findings` 是 `claims[]` 的维度级导航。每条 finding 选择最能直接填充当前 KQ 信息槽位的记录；
定义、成员、数据、关系、来源观点、反例或边界的选择与顺序由 KQ 决定，不使用跨任务固定模板。
finding 不能在已有 claims 之外追加因果、价值判断或结论性尾句。

```json
{
  "finding": "2024 年成熟制程设备国产化率超过 70%，14nm 以下先进制程不足 5%",
  "claim_ids": ["d1.c1", "d1.c2"]
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `finding` | 非空字符串 | 完整的信息句，说明对象是什么、包含什么、数值/变化是什么，或哪一来源提出了什么观点。 |
| `claim_ids` | 非空数组 | 对应的资料记录 ID 列表。每个 ID **必须存在于本文件 `claims[]`**。 |

## 资料记录（`claims[]`）

每条 `claim` 都是一项可被验证和复用的资料记录。它应在证据能核验的信息处停止：对象定义、组成、
数值等 factual 内容后，不追加由 Research 自行得出的“因此/这说明/可见”等裁决。KQ 确实需要解释或
预测时，将有来源支持的观点拆成独立记录并注明提出者、依据和条件。

```json
{
  "id": "d1.c1",
  "text": "中国 2024 年半导体设备国产替代率约 12%",
  "kind": "factual",
  "polarity": "neutral",
  "topic_tag": "domestic_substitution_rate",
  "evidence": [
    {
      "source_id": "d1_s1",
      "snippet": "2024 年中国半导体设备国产化率达到 11.7%，较 2023 年提升 2.3 个百分点",
      "quote_type": "direct"
    }
  ]
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `id` | `^d\d+\.c\d+$` | 形如 `d1.c1`，必须属于当前维度，且在本文件内唯一。 |
| `text` | 非空字符串 | 资料记录本身。**应是一个完整可验证的信息句**，不是段落标题，也不把事实与 Research 自己的结论拼接在一起。 |
| `kind` | `factual` / `interpretive` / `projective` | 见下表。 |
| `polarity` | `support` / `refute` / `neutral` | 立场。用于跨维度矛盾检测。 |
| `topic_tag` | `^[a-z][a-z0-9_]*$` | 主题标签。 |
| `evidence` | 数组 | 至少 1 条。来源等级与多源覆盖的质量目标见下方；未达到时记录诊断，但产物仍可进入下游。 |

### 断言类型三态

| 类型 | 定义 | 示例 | 引用要求 |
|---|---|---|---|
| `factual` | 可被独立验证的事实（数字、事件、状态） | “Tesla 第四季度营收 257 亿美元” | 至少 1 条 evidence；质量目标是至少引用 1 个 primary 或 secondary source |
| `interpretive` | 来源提出的解释、分析、归因，或 KQ 明确要求且有多源材料支撑的关系 | “研究 A 与研究 B 均将 Tesla 利润率下降与同期降价联系起来” | 至少 1 条 evidence；质量目标是引用至少 2 个不同的 `source_id` |
| `projective` | 有明确提出者和前提的未来预测或外推 | “机构 A 在投资持续增长假设下预测中国 7nm 产能于 2027 年扩大” | 至少 1 条 evidence，且 claim text 必须说明提出者与前提 |

`V040`（factual 缺 primary/secondary）和 `V041`（interpretive 缺第二来源）是
**非阻断质量诊断**：Validator 以 `warnings[]` 返回，CLI 记录并展示，但继续发布 Artifact 和
执行 Graph。JSON 结构、ID、枚举、source 引用及必填字段错误仍属于阻断错误。

### 立场三态

| 立场 | 使用场景 |
|---|---|
| `support` | 该断言支持关键问题的某个肯定方向（“X 是可行的，因为……”） |
| `refute` | 该断言反驳常见假设或支持否定方向（“X 不可行，因为……”） |
| `neutral` | 描述性陈述，无明确立场（大多数事实型断言属于此类） |

## 证据项（`evidence[]`）

每条 `evidence` 都是某条断言的一个证据点。

```json
{
  "source_id": "d1_s1",
  "snippet": "2024 年中国半导体设备国产化率达到 11.7%...",
  "quote_type": "direct"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `source_id` | 字符串 | 必须引用本文件 `sources[]` 中对应的 `d{N}_s{M}`。 |
| `snippet` | 非空字符串 | 支撑断言的证据文本。`direct` 表示连续原文，`paraphrase` 表示忠于原意的改写或组合摘要，`numeric` 表示带口径的数据点。**不允许凭印象编造**。 |
| `quote_type` | `direct` / `paraphrase` / `numeric` | 只有逐字复制连续原文时使用 `direct`；组合多个位置的信息必须使用 `paraphrase`；抽取数字及其必要口径时使用 `numeric`。 |

## 来源（`sources[]`）

```json
{
  "id": "d1_s1",
  "url": "https://www.semi.org.cn/...",
  "title": "中国半导体行业 2024 年度报告",
  "quality": "primary",
  "published_at": "2024-12"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `id` | 小写稳定标识 | 必须在当前 dimension 命名空间内唯一；不再要求按数组位置连续编号。 |
| `source_type` | 可选字符串 | 网页来源可省略；本地知识库来源必须为 `local_knowledge_base`。 |
| `url` | http(s) 或绝对 `file://` | 普通网页来源必须是合法的完整 HTTP(S) URL；本地知识库来源必须是与下述 root/ref 对应的绝对本机文件 URL。 |
| `title` | 非空字符串 | 来源标题。 |
| `quality` | `primary` / `secondary` / `tertiary` | 见下表。 |
| `published_at` | `YYYY` / `YYYY-MM` / `YYYY-MM-DD` 或省略 | 原文发表时间。 |
| `knowledge_base_path` | 条件必填绝对目录 | `source_type=local_knowledge_base` 时必填；必须是存在的绝对目录，且不得包含 `..`。 |
| `document_ref` | 条件必填安全相对路径 | `source_type=local_knowledge_base` 时必填；相对于 `knowledge_base_path` 的规范 POSIX 路径，禁止绝对路径、反斜杠、空段、`.`、`..` 和符号链接逃逸。 |

### 本地知识库来源

本地知识库材料进入与网页来源相同的 `claim → evidence → source` 链路，不得作为“无来源背景知识”使用：

```json
{
  "id": "d1_s1",
  "source_type": "local_knowledge_base",
  "url": "file:///Users/example/knowledge/product/notes.md",
  "knowledge_base_path": "/Users/example/knowledge",
  "document_ref": "product/notes.md",
  "title": "Product research notes",
  "quality": "primary"
}
```

本地来源必须同时满足：

- `knowledge_base_path` 是当前运行时存在的绝对目录。
- `document_ref` 是规范的安全相对路径，且指向该目录内存在的普通文件。
- 对 `file://` URL 解码并解析符号链接后，它必须与 `knowledge_base_path/document_ref` 是同一个文件；任何 `..`、绝对 `document_ref`、目录逃逸、符号链接逃逸或 URL/root/ref 不一致都会被 validator 拒绝。
- `title` 与 `quality` 仍然必填。`quality` 按材料本身的来源属性判断；“来自本地”不自动等于 `primary`。
- 最终引用渲染保留 `source_type`、`knowledge_base_path` 与 `document_ref`，参考文献中的标题链接到可点击的 `file://` URL。

### 来源质量三档

| 质量 | 定义 | 示例 |
|---|---|---|
| `primary` | 一手材料：原始数据、官方公告、SEC 申报、政府统计、原始论文 | 财报、白皮书、政府数据库、arXiv 原文 |
| `secondary` | 二手报道/分析：基于一手材料的报道或专业分析 | Reuters / Bloomberg / FT、行业分析师报告 |
| `tertiary` | 三手综合：综述、维基、二次转载、聚合内容 | 维基百科、Substack 综述、聚合新闻 |

## `writing_context`（写作上下文）

`writing_context[]` 保存不属于断言的口径、方法、范围和可得性边界。每项结构：

```json
{
  "id": "d1.w1",
  "kind": "availability_gap",
  "text": "公开资料未披露 2024 年按地区拆分的数据，当前无法确认该口径。",
  "source_ids": ["d1_s1"],
  "applies_to": ["kq2"],
  "use": "在对应检查项中标为证据不足，不推断地区差异。"
}
```

- `id` 匹配 `^d\d+\.w\d+$`，属于当前维度，且在本文件内唯一。
- `kind` 取 `source_profile|methodology|scope_boundary|availability_gap|unresolved_gap`。
- `text` 是非空的实际边界，`use` 是非空的成品使用约束；两者均不设置字符数限制。
- `source_ids` 是 `sources[]` ID 的去重子集，可为空；`applies_to` 是去重 `kqN` 数组，可为空。
- 只写 `{id}` 的空对象不合格。

## 完整示例

```json
{
  "dimension_id": "d1",
  "headline": "2024 年中国半导体设备整体及不同制程的国产化率数据",
  "key_findings": [
    {
      "finding": "2024 年中国半导体设备国产化率为 11.7%，较 2023 年提升 2.3 个百分点",
      "claim_ids": ["d1.c1"]
    },
    {
      "finding": "成熟制程设备国产化率超过 70%，14nm 以下先进制程不足 5%",
      "claim_ids": ["d1.c2"]
    }
  ],
  "claims": [
    {
      "id": "d1.c1",
      "text": "中国 2024 年半导体设备国产替代率约 11.7%，较 2023 年提升 2.3 个百分点",
      "kind": "factual",
      "polarity": "neutral",
      "topic_tag": "domestic_substitution_rate",
      "evidence": [
        {
          "source_id": "d1_s1",
          "snippet": "2024 年中国半导体设备国产化率达到 11.7%，较 2023 年提升 2.3 个百分点",
          "quote_type": "direct"
        }
      ]
    },
    {
      "id": "d1.c2",
      "text": "成熟制程（28nm 以上）设备国产化率超过 70%，14nm 以下先进制程国产化率不足 5%",
      "kind": "factual",
      "polarity": "neutral",
      "topic_tag": "advanced_node_substitution",
      "evidence": [
        {
          "source_id": "d1_s1",
          "snippet": "成熟制程国产化率超过 70%，14nm 以下不足 5%",
          "quote_type": "direct"
        },
        {
          "source_id": "d1_s2",
          "snippet": "China's mature node fabs are domestically supplied, but advanced nodes remain dependent on foreign equipment",
          "quote_type": "paraphrase"
        }
      ]
    }
  ],
  "sources": [
    {
      "id": "d1_s1",
      "url": "https://www.semi.org.cn/report/2024",
      "title": "中国半导体行业 2024 年度报告",
      "quality": "primary",
      "published_at": "2024-12"
    },
    {
      "id": "d1_s2",
      "url": "https://www.ft.com/content/china-chip-2024",
      "title": "China's chip industry: domestic substitution drive",
      "quality": "secondary",
      "published_at": "2024-11"
    }
  ]
}
```
