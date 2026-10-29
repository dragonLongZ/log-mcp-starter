# Proposal: enhance-log-query-mcp

## Why

当前日志查询 MCP 只能连接单一 JumpServer 资产（`active_target` 硬编码），无法支持多资产配合排查问题；SSH 会话在 JumpServer 约 2 小时不操作后被强制关闭，且无 keep-alive 机制，导致查询中断需重新 MFA；查询能力仅限 `tail` 和固定字符串 `grep -F`，无法满足实际日志排查中 grep 正则、head、zgrep、wc、awk 等命令组合的需要。同时缺少一个配套的 skill 来规范 Claude 查询日志的流程（何时让用户选择资产/模块、如何根据意图选择合适的查询方式）。

## What Changes

- **多资产支持**：移除 `active_target` 单资产限制；配置文件中每个 target 携带自己的 SSH 用户名（JumpServer 格式 `user#root#ip`）；gateway 维护多 SSH 连接池，按需建立、复用连接；MCP 工具新增多资产批量查询能力（一次查询多个资产的同一模块/关键词）。**BREAKING**：配置文件 `jumpserver.username` / `jumpserver.active_target` 结构变更为每 target 独立 `ssh_username`。
- **SSH 会话 keep-alive**：每个已建立的 SSH 连接由后台任务定期（默认每 25 分钟）发送轻量心跳命令，防止 JumpServer 空闲超时（约 2 小时）断连；心跳失败或连接关闭时标记该资产为"需重新认证"，MCP 查询返回明确提示而非模糊报错。
- **自由只读查询**：新增 `execute_query` 类工具，允许在配置的 `log_dir` 范围内执行只读命令组合（grep/zgrep/head/tail/cat/zcat/wc/sort/uniq/awk 只读用法/管道组合）；服务端做命令白名单 + 危险模式黑名单（写操作、重定向、命令注入符号）双重校验；路径必须限定在已登记资产的 `log_dir` 之下。
- **配套 skill**：新增 `log-query` skill（Claude Code skill），规范日志查询流程：先列出可用资产/模块 → 需要时让用户选择资产与模块 → 澄清时间范围与日志类型 → 根据用户意图选择 MCP 工具（发现文件 / 关键词搜索 / 自由查询 / tail）→ 多资产结果汇总呈现。

## Capabilities

### New Capabilities

- `multi-asset-session`: 多资产 SSH 连接管理——每资产独立配置与认证、连接池按需建立与复用、多资产状态查询、单资产故障不影响其他资产查询。
- `session-keepalive`: SSH 会话保活——周期性心跳防 JumpServer 空闲断连、连接健康检测、断开后的状态标记与重新认证引导。
- `readonly-freeform-query`: 受限自由只读查询——命令白名单、危险模式拦截（写/删/改/重定向/注入）、路径限定在授权 `log_dir` 内、输出量与超时限制。
- `log-query-skill`: 配套查询 skill——规范资产/模块选择交互、查询意图到工具的映射、多资产结果汇总格式。

### Modified Capabilities

（无——`openspec/specs/` 为空，本项目首次建立规格。）

## Impact

- **配置**：`config/log_targets.yaml` 结构变更（每 target 增加 `ssh_username`，移除全局 `username`/`active_target`）；旧配置需迁移。
- **代码**：
  - `gateway_host.py`：单连接改为连接池（`dict[server, SSHClientConnection]`）、新增心跳后台任务、新增 `/connections`（认证管理）、`/execute-query`（自由查询）端点、命令安全校验模块。
  - `mcp_server.py`：所有工具增加多资产语义（`server` 可为列表或保留单资产 + 新增多资产工具）、新增 `list_connections`/`connect_asset`、`execute_query` 工具。
  - 新增 `.claude/skills/log-query/SKILL.md`（或用户指定位置的 skill 文件）。
- **依赖**：无新增第三方依赖（asyncssh/fastapi/httpx 已具备）。
- **安全边界**：自由查询扩大了远程命令面，安全校验（白名单+黑名单+路径限定）是本变更的核心风险点，需在 spec 中明确拒绝场景。
