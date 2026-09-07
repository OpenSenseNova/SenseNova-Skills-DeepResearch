# 更新指南

普通用户通过 npm 更新已构建的 CLI：

```bash
npm install --global sensenova-skills-deepresearch@latest
```

npm 包会替换当前版本的用户级运行时；不会修改 Git 历史、删除 `runs/` 或 `output/`，也不会改写
`~/.deepresearch-cli/` 中的 Search 配置。

升级后验证：

```bash
deepresearch --help
deepresearch diagnostics --json
deepresearch doctor --harness <harness> --json
```
