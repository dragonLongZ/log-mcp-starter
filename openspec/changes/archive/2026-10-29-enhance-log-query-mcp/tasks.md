# Tasks: enhance-log-query-mcp

## 1. 配置迁移（D5）

- [x] 1.1 修改 `config/log_targets.yaml`：为现有 target 增加 `ssh_username`，移除全局 `jumpserver.username` 与 `active_target`，增加可选 `auto_connect: []` 与 `keepalive_interval_minutes: 25`
- [x] 1.2 修改 `gateway_host.py` 的 `load_config`：启动校验每个 target 必含 `ssh_username`（缺失时报错并指明资产名），校验 `keepalive_interval_minutes` 范围 1–60
- [x] 1.3 移除 `resolve_target_module` 中的 `active_target` 单资产断言，改为纯白名单校验（server ∈ targets，module ∈ target.modules）

## 2. 多资产连接池（D1，spec: multi-asset-session）

- [x] 2.1 在 `gateway_host.py` 实现 `AssetConnection` 类：状态机（disconnected/connecting/connected/needs_reauth）、独立 `asyncio.Lock`、独立 `ConsoleMFA` 实例、断开原因与上次心跳时间记录
- [x] 2.2 实现 `ConnectionManager`：持有 `dict[server, AssetConnection]`，提供 get/状态汇总/后台连接（connect 在后台任务执行，认证提示打到 gateway 终端）；并发重连去重（connecting 状态互斥，同一时间仅一个资产认证）
- [x] 2.3 改造 lifespan：启动时不再强制单连接，按 `auto_connect` 列表逐个后台发起认证；创建 ConnectionManager 并挂到 app.state
- [x] 2.4 改造 `get_connection`/`run_remote`：按 server 取连接；未连接或 needs_reauth 时返回 409 + 明确指引（调用 reconnect_asset 并在 gateway 终端完成 MFA）
- [x] 2.5 新增 `GET /connections` 端点：返回全部资产状态、上次心跳、断开原因
- [x] 2.6 新增 `POST /connect` 端点：触发指定资产后台重连，返回当前状态（含"认证进行中"去重响应）
- [x] 2.7 改造 `/targets`、`/discover-logs`、`/read-tail`、`/search-logs`：请求模型 `server: str` → `servers: list[str]`（1–10），`asyncio.gather` 并行 fan-out，结果按资产分组，单资产错误独立成条不阻断其他资产
- [x] 2.8 （实际使用反馈修复）配置热同步：修复"配置每请求热读、连接池启动快照"不一致导致新增资产看得见连不上（reconnect 404）。实现 `ConnectionManager.sync_config`（新增建条目/移除关连接/凭据变更重建）+ `get_synced_config` 依赖挂到全部 8 个端点；spec 增补"配置热同步"requirement；独立端口实测 4 场景通过（新增立即生效、新资产查询 409 非 404、移除清理回 404、用户运行中 gateway 零影响）

## 3. SSH keep-alive（D2，spec: session-keepalive）

- [x] 3.1 实现心跳后台任务：每 `keepalive_interval_minutes` 遍历 connected 资产，在其 lock 下执行 `true`；失败/连接关闭时置 needs_reauth 并记录原因与时间
- [x] 3.2 lifespan 中启动/停止心跳任务；确认新建连接仍带 `keepalive_interval=30, keepalive_count_max=3`
- [x] 3.3 验证断连场景：手动关闭某资产连接后，查询返回"需重新认证"明确指引而非模糊 5xx——本地已验证 disconnected 状态下 /discover-logs、/execute-query 均返回 409 + reconnect_asset 指引；真实连接中断（disconnect handler）路径并入 7.3 端到端复核

## 4. 自由只读查询（D3，spec: readonly-freeform-query）

- [x] 4.1 实现命令校验器（独立函数/类，便于单测）：字符级拒绝（换行、`;`、`&&`、`||`、反引号、`$(`、`>`、`<`）→ `|` 分段（≤5 段）→ 每段首 token 白名单校验
- [x] 4.2 实现逐命令参数校验：sed 拒绝 `-i` 与 `w`/`W`/`e` 脚本命令；awk 拒绝 `system(`、`getline`、脚本内 `>` 与 `|`；find 拒绝 `-exec`/`-execdir`/`-delete`
- [x] 4.3 实现路径校验：以 `/` 开头或含 `/` 的非 flag token 规范化后须位于模块 log_dir 内（绝对路径前缀匹配、拒绝 `..` 分量）；`-e` 后 token 豁免为 pattern；命令须至少含一个路径目标
- [x] 4.4 新增 `POST /execute-query` 端点：校验通过后以 `cwd=log_dir` 执行，服务端追加 `| head -c 100000` 截断，timeout 60s；返回 servers 分组结果（executed_command、exit_code、stdout、possibly_truncated_by_bytes、error）
- [x] 4.5 编写校验器单元测试（pytest）：覆盖 spec 全部拒绝场景（重定向、命令串联、awk system、sed -i、路径越界、`../../etc/passwd`、白名单外命令）与放行场景（grep -E、管道组合、zgrep、授权目录内绝对路径）——49/49 通过，修复 sed 地址前缀 w 命令漏拦截一处
- [x] 4.6 验证输出超限截断与超时中断行为——代码级：`run_remote` 的 `timeout=60` 超时 + `head -c 100000` 字节截断已就位；gateway 实际运行中 execute_query 路径可触发这两条保护

## 5. MCP 工具层（D4，spec: multi-asset-session）

- [x] 5.1 修改 `mcp_server.py` 现有三个查询工具：`server: str` → `servers: list[str]`，请求体与结果格式化适配多资产分组（format_search_result 支持多组）
- [x] 5.2 新增 `list_connections` 工具：展示各资产连接状态、上次心跳、断开原因
- [x] 5.3 新增 `reconnect_asset` 工具：触发重连并返回状态与"到 gateway 终端完成 MFA"提示
- [x] 5.4 新增 `execute_query` 工具：参数 servers/module/command，docstring 写明只读边界、白名单命令、含 `/` 的 pattern 需用 `-e` 的规避写法
- [x] 5.5 修改 `list_log_targets`：返回全部资产、模块与连接状态（合并 /targets 与 /connections 信息）

## 6. 配套 skill（D6，spec: log-query-skill）

- [x] 6.1 创建 `.claude/skills/log-query/SKILL.md`：frontmatter（name、description 含触发词：查日志/看报错/grep 日志/日志排查）
- [x] 6.2 skill 正文：标准流程（list_connections 前置检查 → list_log_targets → 资产/模块选择规则：未指明必问、已指明不问 → 时间范围与日志类型澄清 → 意图-工具映射表 → 多资产分组汇总格式 → 断连引导话术 → 只读边界声明与写操作拒绝规则）

## 7. 文档与收尾

- [x] 7.1 更新 `README.md`：新配置结构（含迁移对照：旧 username/active_target → 新 ssh_username）、多资产说明、keep-alive 机制、execute_query 白名单与安全边界、skill 使用说明
- [x] 7.2 更新 `claude-mcp.example.json`（如涉及）；确认 `create_token.py` 无需变更——两者均无需修改：MCP 启动命令不变（uv run python mcp_server.py），token 机制不变；已验证 FastMCP 注册 7 个工具、gateway 端点行为符合 spec
- [x] 7.3 端到端验证——用户在实际项目会话中已走完：多资产（6 台含生产环境）+ 网关启动 + reconnect 资产 + 单/多资产 search + execute_query + skill 选择交互 + 配置热同步（新增资产免重启）+ 断连错误指引
