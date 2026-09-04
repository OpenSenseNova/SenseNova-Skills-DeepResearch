# 报告模板

这个目录同时保存内容组织模板与成品排版模板，两者职责不同：

- `content/templates.yaml` 是正式报告的内容参考模板目录。Report Planner 根据 query
  选择一个模板，并把选择写入 Outline；模板只用于内容覆盖和证据组织检查，不规定固定标题，
  也不得覆盖、重排或替换用户明确指定的报告结构。
- `word/` 与 `pdf/` 是成品视觉模板。Word 和 PDF 都使用规范的 Markdown 报告作为内容源，
  但分别使用独立的排版模板。

排版转换器会自动把第一个一级标题作为封面标题，并生成日期和两级目录：

- `word/reference.docx` 控制 Word 的封面、样式、动态页码目录、宽表格、页边距和
  页眉页脚。目录页码由 Word/WPS 排版引擎计算，更新目录不会破坏封面分页。
- `pdf/report.typ` 控制 Typst PDF 的封面、目录、字体、宽表格和分页。

视觉基线：PDF 采用 [Bergfink](https://github.com/andyburri/pandoc-typst-template)
（Unlicense）的 Typst 长文档排版原则，并针对中文深度研究报告
保留独立封面、两级目录、宽表格和研究品牌色；Word 采用中文 Pandoc DOCX 模板的
正文、列表、代码与图注规则（参考
[pandoc_docx_template](https://github.com/Achuan-2/pandoc_docx_template)），由本仓库脚本
独立生成，继续使用现有封面和目录后处理；没有复制该仓库中未声明许可证的 DOCX 文件。

Node Registry 通过 `asset:` 引用加载这些文件，并将模板内容保存到每个 Run 的
Node Spec 快照中。因此，恢复任务时不依赖这个目录里的当前模板文件。

修改 Word 模板生成脚本后，使用以下命令重新生成并写入仓库的模板文件：

```bash
.venv/bin/python -m pip install -e ".[template]"
.venv/bin/python scripts/build_reference_docx.py
```
