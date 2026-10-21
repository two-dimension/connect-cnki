# 知网文献 MCP

供 Codex 等本地 MCP 客户端调用的学术文献服务。根据 [项目实施规格](docs/学术文献MCP项目实施规格.md) 实现，试点学校为厦门大学。研究问题与综合写作由 Codex 完成，本服务负责获取、保存、筛选、引文和可定位的证据，不额外调用另一个大模型。

2026-10-21 已完成“期权市场微观结构与投资者行为”的真实全流程筛选对照：158 篇候选、两组各八篇材料和两份主题综述。新增机制未达采用门槛，本轮不接入；共同下载／PDF 解析修复保留。结果、边界和复核方式见 [试验报告](experiments/options_screening/RESULTS.md)。

已实现 39 个工具、3 个资源入口和 3 个工作流提示：校内网/CARSI 专用会话、受控检索与游标、本地文献库、保守去重及回滚、五学科筛选、RIS/BibTeX/CSL JSON 导入导出、GB/T 7714—2025/2015 引文、PDF 证据与综述草稿检查、任务取消恢复。安装后可立即使用本地导入链路。厦大校内网机构识别已通过真实 MCP 调用，返回 `ready / 厦门大学`。用户已确认非商业个人科研范围的许可，本机已按此配置；真实文字查询、题录入库、缓存游标和单篇摘要已实测；相关度切换与两页 40 条真实翻页已验证；CARSI 人工登录返回后的机构核验也已通过；题名、关键词、作者、来源字段及三种排序已实测；年份/类型的显式候选筛选已实测；真实单篇三页 PDF 的正常下载、兼容解析与页码证据已实测，返回需结构复核的部分完成状态；来源端条件与独立质量验收集仍待完成。详情见 [验收状态](docs/验收状态.md)。

## 可选综述 V2

0.2.0 已增加项目开关、研究计划、统一语料快照、持久化文献卡、主题比较、多来源论断、分节草稿、逐句核查、影响分析和 Markdown／JSON 导出。增强功能按项目默认关闭；用户明确选择后调用 `set_review_assistance` 启用，基础文献功能继续独立使用。写作由接入的 Codex 完成，服务不需要额外模型或 API Key。

工具参数、修订与恢复示例见 [V2 综述使用指南](docs/V2综述使用指南.md)。升级后重新连接 MCP；数据库自动备份并迁移至 schema 3。`export_project` 默认只导出基础产物，综述成果需显式 `include_review=true`。八篇已有材料的隔离回归已经跑通；同一真实课题的人工质量验收仍待完成。

分阶段提交和逐项工程验收映射见 [V2 交付清单](docs/V2交付清单.md)。可通过 `scripts/validate_review_protocol.py` 重跑独立 stdio 冒烟，通过 `scripts/validate_review_v2.py` 重跑八篇材料的隔离回归。

## 哪些判断最值得由人确定

最能改善检索和写作效果的人工投入，依次是**课题边界与纳排口径、关键论断的原文和方法核查、章节要论证的主线**。Codex 可以先拟计划、找候选、整理证据和写草稿；研究者、导师或熟悉该领域的合作者适合处理以下判断。

| 环节 | 建议由人确定或复核的内容 | 项目如何保留结果与限制 |
| --- | --- | --- |
| 课题启动 | 要回答的具体问题、章节用途、时间与市场范围；哪些论文属于核心依据，哪些仅作方法或制度背景。提供少量已知相关与不相关的例子，有助于校准关键词和边界。 | 增强综述启用后，`save_review_plan` 保存范围、纳排标准、种子文献、同义词及章节要求；计划按版本修订。只有一个宽泛题目时，Codex 应先提出具体方案并说明暂定假设。 |
| 检索覆盖与停止 | 核心作者、术语和已知必读文献是否遗漏；来源、语言及年份是否覆盖研究问题；预算耗尽后是否补检索。 | 查询计划与实际检索记录分别保存。预算截断、本地候选筛选和未命中均有边界，不能据此断言已穷尽文献。新增论文或范围变化后，重新检查覆盖。 |
| 筛选规则与争议条目 | 学科模板、年份／类型约束、经典文献例外、严格程度与阈值是否适合课题；重点看待复核项，以及可能误排的重要论文。 | `save_screening_policy` 保存规则，`review_decision` 追加明确的覆盖结果和理由。当前 `screen_papers` 的相关性维度标为 `not_assessed`，`recommended` 仅是当前规则结果，不能直接作为选题相关或最终纳入结论。缺信息保留未知。 |
| 重复研究与发表版本 | 学位论文和期刊论文是否报告同一研究；是否共享样本、属于扩展、勘误或不同发表版本。 | `resolve_duplicate` 保存关系，必要时 `rollback_merge` 回滚。引用版本和文件身份保留；共享数据的多个报告需核对独立性，避免重复计算支持。 |
| 关键证据与方法 | 对照 PDF 检查关键数字、表格、公式、研究对象和样本条件；核对相关／预测与因果、作者解释与直接观测的区别，以及反证和不显著结果。 | 文献卡分别保存原文陈述、作者解释、Agent 综合和用户判断。页码为 PDF 页序，印刷页码需核对；扫描、排版和缺页提示继续保留。解析成功、返回片段或引语匹配都不等于语义支持成立。 |
| 章节主线与最终论断 | 本章要证明什么、分类是否帮助回答问题、比较条件是否一致、是否遗漏相反证据，研究缺口是否超出当前检索范围。 | `save_synthesis`、`save_review` 保存比较和正文；事实／结构／语言意见通过 `save_review_check` 分轮记录。`get_review_progress` 展示未判断句子；学术结论和最终采用稿仍需研究者判断。 |
| 题录与引用格式 | 作者、年份、卷期页码及实际引用版本；学校或期刊要求采用 2025 还是 2015 版，是否另有字体、缩进等版式要求。 | 当前默认采用 GB/T 7714—2025 中文顺序编码制，也支持 2015 版。CSL 统一编号和书目，学校专用版式需另行核对。题录与 PDF 冲突时保留两者及更正依据，不把自动排版视为题录已正确。本次试验的一处年份冲突见[报告](experiments/options_screening/RESULTS.md)。 |
| 新材料与持续修订 | 是否采用新发表版本、新解析或新快照；材料变化后，原判断和章节是否仍成立。 | `create_corpus_snapshot` 显式选择依据，`get_review_impact` 提供影响范围；依赖变化会使相关核查过期，旧稿与旧证据可重放。重新解析不会自动替换旧引用，修订需携带父版本。 |
| 效果验收与机制采用 | 由熟悉领域且尽量独立于写稿过程的人核查代表性纳排决定和关键论断，记录错误排除、过度推广与真实工作量。 | 同一 Agent 写作后再核查仍属模型意见，不能充当独立人工金标准。当前缺少独立专家验收；本轮新增筛选机制未达门槛，未接入。工程测试通过不代表综述质量达标。 |

可按三个节点集中沟通，降低操作负担：开始时确认研究计划；首轮候选出来后校准少量纳入、排除和待复核样例；草稿形成后核对论证主线、关键事实和书目。已有明确要求继续沿用，常规检索、记录和排版可在已授权范围内连续进行。重要未知项由 Codex 列出依据和建议，供人作有针对性的判断。

登录、扫码和网站安全验证由用户亲自在专用窗口完成；首次使用或用途、接收方发生变化时，需明确相应许可范围，已有确认沿用。具体操作见[校内网使用](#校内网使用)及[内容输出与操作许可](#内容输出与操作许可)。

人工意见应保留具体对象、依据和修改原因。`review_decision` 的覆盖记录和增强综述中的用户角色均由调用方提交；增强综述的 `actor_role="user"`、`human_checked`、`verified_by_user` 只应在用户实际给出判断后填写。这些字段不具备独立身份认证，Codex 的自行判断按 Agent 意见保存。增强综述接口的参数与示例见 [V2 综述使用指南](docs/V2综述使用指南.md)。

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

真实 MCP 检索联调可运行：

```powershell
uv run python scripts/pilot_mcp.py --query 大语言模型 --limit 3 --budget 20
```

此入口使用官方 MCP 客户端启动 stdio 服务并保留同一会话。输入 `verify` 核验，`search` 提交检索，`next` 继续游标，`paper` 读取首条本地题录，`detail` 显式刷新其摘要，`inspect` 查看脱敏布局诊断，`close` 正常关闭。出现安全验证时在同一窗口亲自完成后再核验；可传 `--project-id` 复用已有联调项目。`download / job / parse / read` 可依次验证首条文献的正常下载、任务状态、PDF 解析与第一页证据（`read` 在此联调脚本只打印统计信息）。MCP 本身也支持 `get_access_status(inspect_page=true)`，默认状态查询仍不读取页面。

同一联调窗口可输入 `search {"query":{"text":"大语言模型","field":"title"},"sort":"relevance","limit":3}` 调整查询；只接受 `query / filters / filter_mode / sort / limit`，随后 `next` 保持该组条件。实际支持范围由档案和页面核验决定。预算截断来源页时会显式标记，并在缓存用完后停止，不跨过未采集的页尾。

年份和类型可显式指定 `filter_mode="local_candidates"`，例如 `filters={"year_from":2025,"year_to":2025,"paper_types":["journal"]}`。此模式先取得预算内候选，在本地筛选；`limit` 计候选批次，`matched_count / filtered_out_count` 记录累计数量，排除原因和题录保留，游标可继续。默认 `source` 模式只接受已核验的来源控件，未配置时明确拒绝。类型受限的采集许可禁止用本地模式替代来源范围约束。

新版知网使用带签名的文章详情链接。此类链接仅保留在对应会话内存中，题录以稳定来源标识入库，`detail_url` 可为空；重启后须在新会话重新检索以恢复详情入口。摘要标记 `source_text / partial / unknown`，折叠内容未能展开时返回 `partial` 和提示。

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
uv run ruff check src tests scripts
```

浏览器测试只使用明确标注的人工页面夹具，不访问真实知网，不计入实校验收。若未安装浏览器，可运行 `uv run pytest -q -m 'not browser'`。

文本 PDF 采用 pypdf；重复字典键可在结束结构和内容流检查通过后兼容读取，原文件不变，并返回 `structure_review_required / partially_succeeded`。扫描件返回 OCR 所需状态，公式、表格、复杂版面均提示复核。CAJ 只人工导入并记录文件签名，未声称可解析。OCR/MinerU、语义检索、外部元数据增强及 Zotero 同步为尚未启用的 P1 能力。引用身份与原文引语检查无法证明论断语义受支持，综述始终保存为待复核草稿。

实现分工见 [架构与工具契约](docs/架构与工具契约.md)，实测与未验收项见 [验收状态](docs/验收状态.md)。
