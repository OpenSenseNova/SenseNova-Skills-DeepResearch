# 自定义 Workflow E2E 示例

这个目录展示一个新用户如何只通过配置扩展完整研究流程：

- `nodes/expand-research-plan.yaml` 注册一个自定义 Script Node；
- 该 Node 读取内置 `plan` 的阶段产物，把研究任务规范化为 8 个并行任务；
- `custom-heavy-8.yaml` 把该 Node 插入完整 Heavy 流程；
- Script 和 Validator 都会被写进 Run 的 Node Spec 快照，后续 resume 不依赖本目录仍然存在。
- 本例用 `validators` 先直接执行唯一的正式 Plan Validator，再执行只负责“必须扩展为 8 个任务”的节点级检查；后者不重复 Plan 字段规则。

先检查自定义 Node：

```bash
deepresearch nodes describe expand-research-plan \
  --nodes-dir ./examples/custom-workflow/nodes \
  --json
```

再运行完整流程：

```bash
deepresearch "研究一个需要多角度交叉验证的问题" \
  --workflow ./examples/custom-workflow/custom-heavy-8.yaml \
  --nodes-dir ./examples/custom-workflow/nodes \
  --mode heavy \
  --harness hermes \
  --max-concurrency 8 \
  --no-node-timeout
```

`expand-research-plan` 只演示配置能力和高并发 fan-out，不建议把“固定 8 个任务”当成所有研究问题的默认策略。
