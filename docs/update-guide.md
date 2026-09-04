# 更新指南

源码安装的 CLI 使用当前工作区构建。先自行切换到目标分支或执行 `git pull`，确认工作区干净后运行：

```bash
bash scripts/update.sh
```

脚本会重建 Python wheel 和 npm 包并替换当前版本的用户级运行时；不会修改 Git 历史、删除
`runs/` 或 `output/`，也不会改写 `~/.deepresearch-cli/` 中的 Search 配置。工作区有未提交修改时，
脚本会拒绝执行，避免覆盖本地开发内容。

升级后验证：

```bash
deepresearch --help
deepresearch diagnostics --json
deepresearch doctor --harness <harness> --json
```
