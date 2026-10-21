# 第三方组件与样式

本项目没有复制其他知网爬虫项目代码。Python 依赖的精确版本与下载哈希保存在 `uv.lock`。发布时应随分发产物保留各依赖原始许可。

| 直接组件 | 用途 | 上游许可 |
| --- | --- | --- |
| modelcontextprotocol/python-sdk (`mcp`, 1.x 维护分支) | MCP stdio 协议 | MIT |
| AnyIO | MCP 取消作用域内的子进程与暂存文件清理保护 | MIT |
| Playwright Python | 专用临时浏览器上下文 | Apache-2.0 |
| Pydantic | 输入和配置校验 | MIT |
| Beautiful Soup | 脱敏页面题录解析 | MIT |
| pypdf | PDF 结构检查与文本解析 | BSD-3-Clause |
| bibtexparser | BibTeX 导入导出 | LGPL-3.0-or-later |
| rispy | RIS 导入导出 | MIT |
| citeproc-js (`citeproc` npm 包) | CSL 渲染 | CPAL-1.0 或 AGPL-3.0 双重许可，详见安装包原始 LICENSE |

格式化文本由 Frank Bennett 编写的 [citeproc-js](https://github.com/Juris-M/citeproc-js) 生成。npm 精确版本及完整性哈希保存在 `src/cnki_mcp/js/package-lock.json`。该组件通过本地 Node.js 子进程运行，不访问网络、不调用模型。应保留上游 LICENSE、CPAL/AGPL 及作者署名；对外分发或服务化时按所选许可履行义务。

`src/cnki_mcp/styles/` 包含从 [CSL Styles](https://github.com/citation-style-language/styles) 原样取得的两个独立样式，均保留原作者、样式标识、更新时间及许可声明：

- `gbt-7714-2025.csl`：作者 Zeping Lee；样式更新时间 2026-05-10。
- `gbt-7714-2015.csl`：作者 牛耕田；贡献者 Zeping Lee；样式更新时间 2024-01-22。

样式许可为 [Creative Commons Attribution-ShareAlike 3.0](https://creativecommons.org/licenses/by-sa/3.0/)，该许可适用于样式本身。下载日期、原始来源及文件 SHA-256 见同目录 `SOURCES.json`。当前小型回归样例不替代规格要求的 50 个独立引文黄金样例及学校格式复核。

OCR/MinerU、Jasminum、GROBID、Zotero 翻译器、外部模型 SDK 不随本项目安装。P1 组件启用前需分别核验其许可。
