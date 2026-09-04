# SenseNova-Skills-DeepResearch 文档

本目录只保存与产品使用和实现约束相关的长期文档，不混入测试批次、运行日志或阶段性待办。

## 用户文档

- [SenseNova-Skills-DeepResearch 使用指南](usage-guide.md)：CLI 命令、研究模式、节点输入输出、超时与恢复。
- [诊断指南](diagnostics.md)：安装、Harness、Search、Camofox、Web、Run 和导出故障定位。
- [Search MCP](search-mcp.md)：多来源检索、正文读取、Camofox 回退、配置和故障语义。
- [运行时设计](design.md)：Workflow 编译、Artifact、持久化、ACP Harness 和扩展边界。
- [自定义工作流示例](../examples/custom-workflow/README.md)：通过 YAML 组合内置节点。
- [npm 安装层](../npm/README.md)：npm 启动器、隔离 Python 运行时和构建方式。
- [更新指南](update-guide.md)：源码安装后的升级、数据保留和验证。

## README

- [中文 README](../README.md)
- [English README](../README_EN.md)

## 文档维护约定

- 命令和可选值以 CLI 实际输出为准。
- 节点链路以 `config/workflows/*.yaml` 为准。
- 节点合同以 `config/nodes/*.yaml` 为准。
- 文档只描述当前行为，不混入历史方案或单次测试结论。
- 示例使用通用路径、通用主题和占位符，不依赖特定运行环境。
- README 只保留产品定位和最短成功路径；详细参数、认证和排障放在对应专题文档。
- 修改安装、认证或命令时，同时检查中文 README、英文 README、Skill 和 npm README，避免重复内容漂移。
