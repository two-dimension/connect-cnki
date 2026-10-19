# 知网文献 MCP

供 Codex 等本地 MCP 客户端调用的学术文献服务。根据 [项目实施规格](docs/学术文献MCP项目实施规格.md) 实现，试点学校为厦门大学。研究问题与综合写作由 Codex 完成，本服务负责获取、保存、筛选、引文和可定位的证据，不额外调用另一个大模型。

已实现 29 个工具、3 个资源入口和 1 个工作流提示：校内网/CARSI 专用会话、受控检索与游标、本地文献库、保守去重及回滚、五学科筛选、RIS/BibTeX/CSL JSON 导入导出、GB/T 7714—2025/2015 引文、PDF 证据与综述草稿检查、任务取消恢复。安装后可立即使用本地导入链路。厦大校内网机构识别已通过真实 MCP 调用，返回 `ready / 厦门大学`。用户已确认非商业个人科研范围的许可，本机已按此配置；**真实文献检索及 CARSI 尚未验收**。详情见 [验收状态](docs/验收状态.md)。

## 安装与启动

Windows 本地环境需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js（只用于 CSL 文本引文）。默认通过本机 Edge 的临时独立上下文打开知网，不读取日常 Edge 的个人配置。也可在配置中指定 `chrome` 或 `chromium`。

在 PowerShell 7 中执行：

```powershell
./scripts/setup.ps1
uv run cnki-mcp --config config.local.json --check
uv run cnki-mcp --config config.local.json
```

最后一条命令运行 stdio 协议服务，等待 MCP 客户端输入；不要向它输入普通聊天文本。标准输出仅用于协议，状态和诊断不会混入 stdout。

若使用 Playwright 自带 Chromium：

```powershell
./scripts/setup.ps1 -BrowserChannel chromium -InstallBrowser
```

已有 `config.local.json` 会原样保留；如需切换浏览器，修改其中 `browser_channel`。浏览器下载默认放在配置数据目录的 `browsers/` 下。此机器的 Chromium 安装遇到 DLL 写入限制，已通过 Edge 完成浏览器夹具测试。

## 接入 Codex

```powershell
./scripts/register-codex.ps1
```

脚本在本项目生成 `.codex/config.toml`，使用虚拟环境 Python 的绝对路径启动服务，不修改全局 Codex 设置，不覆盖已有同名条目。在受信任项目中重启 MCP 连接或新开项目聊天后，检查 `cnki_literature` 的工具是否可见。Codex 支持的本地 stdio 与配置字段见 [官方 MCP 文档](https://developers.openai.com/codex/mcp/)。

也可以在客户端的 MCP 设置中直接填写：命令为本项目 `.venv/Scripts/python.exe` 的绝对路径，参数为 `-m cnki_mcp --config <config.local.json绝对路径>`，工作目录为项目目录。服务无需 OpenAI API Key；Codex 负责模型调用。

## 校内网使用

学校档案是内部配置，不是登录表，也不存账号密码。厦大名称、官方 CARSI 地址及主机名单已由项目维护。电脑已在校内网时，直接选择 `institution_network`，无需先登录 VPN 或 CARSI。校内入口先打开知网官网加载机构组件，实际检索仍使用 KNS 入口。

典型工具顺序：

1. `list_institutions` 查看档案，`create_project` 创建 `institution_id="xmu"` 的项目。
2. `start_access_session(institution_id="xmu", mode="institution_network")` 打开专用浏览器。
3. 用户按页面需要完成验证码等交互后调用 `verify_access_session`；只在精确机构标签匹配时返回 `ready`。
4. `search_papers` 提交主题、题名等受控查询；下一页复用完全相同的项目、会话和查询，并传入 `next_cursor`。
5. `get_paper` 默认读本地；显式 `refresh=true` 和 `session_id` 才联网刷新摘要。
6. `screen_papers` 筛选，`export_bibliography` 导出选定发表版本。

厦大档案已固定实页确认的可见机构组件 `.ecp_header_unitName`，精确匹配“厦门大学”；隐藏登录面板不会阻止识别，隐藏机构名也不能作为认证依据。查询条件或排序控件未核验时返回 `UNSUPPORTED_*`，不会悄悄丢弃条件。入口页面变化返回 `SOURCE_LAYOUT_CHANGED`；频繁访问提示会暂停会话。

如需在接入 Codex 前确认真实浏览器状态，可以运行：

```powershell
uv run cnki-mcp --config config.local.json --access-check xmu
```

它打开可见专用窗口，等待用户亲自处理网站安全验证；在终端输入 `verify` 核验机构、`inspect` 查看脱敏诊断、`reload` 重载本地配置、`reopen` 重开已配置入口、`close` 结束。`reload / reopen` 保留同一专用浏览器上下文，并要求重新核验机构；修改数据目录或浏览器类型仍需重启。诊断使用独立数据子目录，不影响已运行 MCP 的任务，不提交文献检索或下载；只列出主机和预期学校名的结构候选，不输出完整 URL、登录 DOM、密码或身份断言。候选节点必须由维护者确认其机构组件含义，不能自动作为认证成功依据。CARSI 可使用 `--mode carsi`。

额外的静态资源主机使用 `resource_hosts` 单独配置，目前包含实际页面请求的 `piccache.cnki.net` 及官网加载的腾讯验证 SDK 主机。它只允许脚本、样式、图片、字体和媒体子资源；页面导航、XHR 和 fetch 仍须符合 `allowed_hosts`。官网的机构组件由 `login.cnki.net` 提供，作为精确认证主机单独放行。专用上下文禁用 service worker，避免缓存工作线程绕过路由检查。

## 无需在线接入的本地流程

将合法取得的 RIS、BibTeX、CSL JSON、JSON 题录或 PDF 放入 `imports/`（该目录不进 Git）。可在本地配置 `import_roots` 加入其他明确选择的目录。路径解析会阻止目录穿越和指向允许目录外的链接。

1. `create_project(question="研究问题")`，不绑定学校也可使用本地导入。
2. `import_records(project_id=..., file_path=..., format="ris")`；也可直接传入 `records` 数组。
3. `save_screening_policy(rules=...)` 后调用 `screen_papers`。缺失值进入待复核，人工覆盖通过 `review_decision` 追加记录。
4. `import_fulltext(version_id=..., file_path=..., rights=...)` 校验文件，再 `parse_fulltext(asset_id=...)` 获取任务标识。
5. `get_job` 查询解析结果；`read_paper`、`search_evidence` 获取页码与片段；`prepare_review` 准备矩阵。
6. Codex 写草稿，通过 `save_review` 保存正文与论断映射，最后 `export_bibliography` 和 `export_project`。

`examples/records.json` 是**虚构开发样例**，不能作为论文参考文献。

## 内容输出与操作许可

`config.local.json` 将接收方标记为 `external / openai`，初始只返回题录与状态。要向 Codex 返回摘要或全文，需分别满足客户端 `allowed_content` 和对应来源的处理许可。摘要、正文、全文评分解释及综述均执行同一出站检查；工具、资源和项目导出都不会绕过它。

- 对学校来源，五项操作分别为 `automated_search`、`metadata_collect`、`fulltext_download`、`local_parse_index`、`external_model_use`。状态分为 `allowed / denied / unknown`。
- 维护者根据真实依据填写 `source`、`verified_by`、含时区的 `verified_at / expires_at` 和 `resource_types`；外部模型处理还需 `providers: ["openai"]`。不从“在校内网”自动推导上述权限。
- 用户明确确认的非商业个人科研许可可设 `basis_type="user_confirmation"`、`permitted_purpose="personal_noncommercial_research"`，并将顶层 `usage_purpose` 设为同值。用户未指定到期日时 `expires_at` 保持空值，不编造日期；修改用途为商业或未说明时，该确认不适用。其他学校的示例配置仍保留未知状态，文档型许可继续要求有效期。
- 对合法导入文件，`ImportRights` 明确 `basis`、`allow_local_parse` 和 `allowed_recipients`，例如 `local` 或确有授权的 `openai`。未明确授权时可以保存题录，内容保持屏蔽。
- 不保存账号、密码、cookie、SAML 断言、登录截图或日常浏览器配置。会话不跨重启持久化；重启后须重新建立会话。

配置文件和运行数据不进 Git。依赖精确版本由 `uv.lock` 和 npm lock 锁定；CSL 样式及语言文件来源、许可与 SHA-256 均保存于仓库，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

数据库升级前会自动在数据目录的 `backups/` 保存一致快照和 SHA-256 清单；迁移失败会回滚，保留原有文件及目录使用权。工具与任务审计只记状态、耗时和标识。恢复步骤见 [架构与工具契约](docs/架构与工具契约.md)。

## 验证与限制

```powershell
$env:CNKI_TEST_BROWSER_CHANNEL = 'msedge'
uv run pytest -q
uv run ruff check src tests
```

浏览器测试只使用明确标注的人工页面夹具，不访问真实知网，不计入实校验收。若未安装浏览器，可运行 `uv run pytest -q -m 'not browser'`。

文本 PDF 采用 pypdf；扫描件返回 OCR 所需状态，公式、表格、复杂版面均提示复核。CAJ 只人工导入并记录文件签名，未声称可解析。OCR/MinerU、语义检索、外部元数据增强及 Zotero 同步为尚未启用的 P1 能力。引用身份与原文引语检查无法证明论断语义受支持，综述始终保存为待复核草稿。

实现分工见 [架构与工具契约](docs/架构与工具契约.md)，实测与未验收项见 [验收状态](docs/验收状态.md)。
