# Agent Prompt 资源包

本目录保存内置研究节点的角色 Prompt，以及现有 Claims/Evidence 相关 schema、validator 和引用渲染脚本。

配置运行时把这里作为内置 Prompt Bundle：Node Registry 在创建 Run 前读取 Prompt，再将具体文本放入 Node Spec 快照。resume 使用 Run 内快照，不重新读取本目录。

Prompt 只描述节点的研究职责。每次 Agent 调用由 NodeRunner 追加唯一、统一的 Runtime Node Context：

```text
run / step / scope / inputs / outputs
```

其中 `inputs` 按 Node Spec 端口提供已提交 Artifact 的绝对只读路径，`outputs` 提供当前 staging 的准确写入路径或 batch pattern。Prompt 中若有描述性文件名，最终以该 Context 为准。

本目录不定义流程顺序、跳转、fan-out 或恢复策略：

- 工作流顺序位于根目录 `config/workflows/*.yaml`；
- 输入输出合同位于根目录 `config/nodes/*.yaml`；
- scope、attempt、发布和恢复由统一 Driver/NodeRunner 处理；
- Validator 在 Agent 返回后由 NodeRunner 对封存 candidate 只读执行。

内置 Prompt 仍可引用较完整的业务 schema 来提升研究质量，但运行时的基础 Artifact 传递只要求 Node Spec 声明、路径安全、文件非空和 hash 完整。新增能力不必采用本目录的 Claims/Evidence schema；它可以通过自己的 Node YAML 提供最小 Validator。
