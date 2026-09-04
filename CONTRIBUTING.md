# Contributing to SenseNova-Skills-DeepResearch

感谢参与 SenseNova-Skills-DeepResearch。提交 Issue 或 Pull Request 前，请先阅读本文件和项目 [README](README.md)。

## 开发环境

- Node.js 22+（包含 npm）
- Python 3.10+
- Git

```bash
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -e ".[dev]" build
```

提交前运行：

```bash
.venv/bin/python -m pytest
node --test npm/tests/*.test.js
git diff --check
```

保持变更聚焦，补充相应测试和文档，不提交 token、Cookie、个人路径、运行记录或构建产物。Pull Request 应说明目的、主要改动和验证方式。
