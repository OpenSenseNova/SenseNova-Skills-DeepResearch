# SenseNova-Skills-DeepResearch for npm

这个 npm 格式的安装包是 SenseNova-Skills-DeepResearch 的安装和命令转发层。包内自带版本一致的 Python
wheel；安装时在用户目录创建独立虚拟环境，不修改系统 Python，也不把 Python 依赖安装进项目目录。
当前安装方式是从 [GitHub 仓库](https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch)源码构建本地包。

## 环境要求

- Node.js 22 或更高版本；
- Python 3.10 或更高版本；
- 安装 Python wheel 依赖时可以访问 Python Package Index；
- 使用 Hermes、Codex、Claude Code 或 OpenClaw 时，相应 Harness 已安装并配置。

如果 Python 不在标准 PATH 中，可以在安装前指定。源码仓库根目录优先使用
`bash scripts/install.sh`；更新已检出的干净工作区使用 `bash scripts/update.sh`。
手动构建流程如下：

```bash
export DEEPRESEARCH_PYTHON=/path/to/python3
git clone https://github.com/OpenSenseNova/SenseNova-Skills-DeepResearch.git
cd SenseNova-Skills-DeepResearch
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python scripts/build_npm_package.py
npm install -g ./dist/*.tgz
```

安装脚本会依次探测 `python3.14`、`python3.13`、`python3.12`、`python3.11`、
`python3.10`、`python3` 和 `python`；Windows 会先探测对应的 `py -3.x` launcher，
并选择第一个 Python 3.10+；
`DEEPRESEARCH_PYTHON` 始终拥有最高优先级。

Windows PowerShell：

```powershell
$env:DEEPRESEARCH_PYTHON = "C:\\Python311\\python.exe"
npm install -g .\\dist\\*.tgz
```

## 使用

```bash
deepresearch --help
deepresearch doctor --harness hermes --json
deepresearch sources init
deepresearch web
```

`deepresearch sources init` 会创建 `~/.deepresearch-cli/search/.env`，这是 npm 用户配置
Search token、cookie、User-Agent 和代理的统一入口。按命令输出编辑文件后，可运行
`deepresearch sources list --json` 查看各 Source 是否可用。也可通过
`DEEPRESEARCH_SEARCH_CONFIG_HOME` 修改用户配置目录。

Python 运行时默认位于 `~/.deepresearch-cli/npm-runtime/<version>/`。可在安装和运行时使用
`DEEPRESEARCH_NPM_RUNTIME_HOME` 指定其他位置。

Camofox 浏览器内核不包含在 npm 包中，避免让基础安装包增加数百 MB。需要反扒回退时单独执行：

```bash
deepresearch browser setup
deepresearch browser start
```

## 本地构建

在源码仓库根目录执行：

```bash
PYTHON_BIN="${DEEPRESEARCH_PYTHON:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python >=3.10 required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python scripts/build_npm_package.py
```

脚本会构建 Python wheel、复制到 npm 包的 `vendor/`，运行 Node 测试，并在根目录 `dist/`
生成 `.tgz`。仓库和 npm 包当前使用 MIT License。

构建后可以在临时 npm prefix 或测试机安装本地包：

```bash
npm install -g ./dist/*.tgz
deepresearch --help
```

如果后续确认 npm scope 属于当前发布账号，可以再发布 scoped package：

```bash
npm login
npm publish ./dist/*.tgz --access public
```
