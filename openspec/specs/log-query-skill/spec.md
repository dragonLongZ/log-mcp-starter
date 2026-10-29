# log-query-skill Specification

## Purpose
TBD - created by archiving change enhance-log-query-mcp. Update Purpose after archive.
## Requirements
### Requirement: skill 定义标准查询流程
项目 SHALL 提供一个 `log-query` skill（Markdown 指令文件，供 Claude Code 加载），定义日志查询的标准流程：列出可用资产与模块 → 确认/让用户选择目标 → 澄清时间范围与日志类型 → 选择合适 MCP 工具执行查询 → 汇总呈现结果。

#### Scenario: skill 可被发现与加载
- **WHEN** skill 文件按 Claude Code skill 规范放置（含 name 与 description 的 frontmatter）
- **THEN** Claude Code 会话中该 skill 出现在可用列表并可被触发

### Requirement: 资产与模块选择交互
skill MUST 规定：资产选择先于一切连接与查询动作。当用户请求未明确指定资产时（即使只有一个模块名线索、或多个资产都含匹配模块），Claude 须先调用 `list_log_targets` 获取清单，并通过提问让用户选择资产（可多选）与模块（可多选），拿到选择结果后才允许发起认证或查询；当用户已明确指定资产时不重复询问。

#### Scenario: 未指定资产时询问
- **WHEN** 用户说"帮我查下昨天的报错日志"且未指明资产/模块
- **THEN** Claude 先列出资产与模块清单，请用户选择后再查询

#### Scenario: 多资产含同名模块时仍须询问
- **WHEN** 用户说"查云车场昨天的入场记录"且两个及以上资产都配置了匹配模块
- **THEN** Claude 用提问让用户勾选目标资产（可多选），不默认全选

#### Scenario: 已明确指定时直接查询
- **WHEN** 用户说"查 server-172-26-73-80 上 member 模块今天的 error 日志"
- **THEN** Claude 不再询问选择，直接执行查询

### Requirement: 最小操作范围（只动选定的资产）
skill MUST 规定硬性禁令：(1) 禁止对用户未选定的资产发起 `reconnect_asset`——认证只针对用户选定且需要查询的资产；(2) 禁止把"全部资产"或"全部已连接资产"作为查询的 `servers` 默认值——每次查询的 `servers` 必须且只能是用户明确指定或明确勾选的资产集合；(3) 资产连接状态（connected/disconnected）MUST NOT 作为选择依据、默认值或候选过滤理由——选定只看用户意图，"恰好一个资产匹配"的自动选定规则仅按业务归属判断；(4) 用户未完成选择（拒绝提问、未作答、回复不含资产名的"继续"类指令）时 MUST NOT 推进认证或查询。

#### Scenario: 只认证选定资产
- **WHEN** 用户选定资产 A（另有资产 B 也处于断连状态）
- **THEN** Claude 只对 A 发起 reconnect_asset，不触碰 B

#### Scenario: 只查询选定资产
- **WHEN** 用户选定资产 A 后发起查询（A、B 均已连接）
- **THEN** 查询的 servers 仅含 A；B 的连接状态不影响查询范围

#### Scenario: 用户明确要求多资产时才多选
- **WHEN** 用户说"两台都查一下"或在提问中勾选多个资产
- **THEN** Claude 才把多个资产放入 servers 并对未连接的选定资产发起认证

#### Scenario: 禁止默认选中已连接资产
- **WHEN** 用户未指明资产，多个资产都含匹配模块，其中恰好只有一台处于 connected
- **THEN** Claude 仍必须提问让用户勾选资产，不得以"只有它可用"为由直接选定该 connected 资产

#### Scenario: 用户未完成选择时不得推进
- **WHEN** 用户拒绝资产选择提问，或回复"继续"等不含资产名的指令
- **THEN** Claude 重新给出选择提问或停下等待，不得自行选定资产推进查询；用户明确不选择时终止查询并说明无法确定目标资产

### Requirement: 查询意图到工具的映射
skill SHALL 定义意图与 MCP 工具的映射规则：浏览最新日志 → `read_log_tail`；固定字符串/关键词检索 → `search_logs`；正则、压缩文件、统计计数、多命令组合等复杂需求 → `execute_query`（自由只读查询）；文件名或日期不确定时 → 先 `discover_log_files` 再用 file_ids 精确查询。

#### Scenario: 正则需求映射到自由查询
- **WHEN** 用户要求"找出所有以 ERROR 开头且包含 timeout 的行"
- **THEN** Claude 按 skill 指引构造 `grep -E` 只读命令并通过 `execute_query` 执行

#### Scenario: 文件名不确定时先发现
- **WHEN** 自动文件选择失败或用户指定的日期文件名不规则
- **THEN** Claude 先调用 `discover_log_files` 查看候选，再用返回的 file_ids 查询

### Requirement: 连接状态前置检查与引导
skill SHALL 规定：查询前若遇"需重新认证"或"bridge 未运行"类错误，Claude 须向用户转达明确的操作指引（启动 gateway_host.py / 调用 reconnect_asset 并在 gateway 终端完成 MFA），不得反复盲目重试。

#### Scenario: 断连错误引导用户
- **WHEN** 查询返回资产"需重新认证"
- **THEN** Claude 告知用户到 gateway 终端完成认证或触发重连，而非重试相同查询

### Requirement: 多资产结果汇总格式
skill SHALL 规定多资产查询结果的呈现格式：按资产分组、每组标注模块/文件/匹配数，失败的资产单独列出原因；汇总时对相同错误模式可跨资产归并说明。

#### Scenario: 多资产结果呈现
- **WHEN** 对两个资产执行同一关键词搜索且均有结果
- **THEN** Claude 按资产分组呈现匹配内容，并给出跨资产的简要结论

### Requirement: 只读边界声明
skill MUST 声明查询边界：仅执行只读命令，任何涉及写入、修改、删除远程文件的请求都应被拒绝并向用户说明本 MCP 的安全边界。

#### Scenario: 拒绝写操作请求
- **WHEN** 用户要求"把日志里的错误行删掉"或"清空日志文件"
- **THEN** Claude 依据 skill 拒绝执行并说明本工具仅支持只读查询

