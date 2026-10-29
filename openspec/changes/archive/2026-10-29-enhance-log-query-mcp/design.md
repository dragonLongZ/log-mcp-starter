# Design: enhance-log-query-mcp

## Context

现状：`gateway_host.py`（FastAPI + asyncssh）在独立终端维持**单条** SSH 连接到 JumpServer（用户名内嵌资产路由 `user#root#ip`），`mcp_server.py`（FastMCP stdio）通过 bearer token 调用本地 bridge，提供 discover / search / tail 三类只读查询。约束：

- JumpServer 空闲约 2 小时强制断连，重连需人工 MFA（只能在 gateway 终端交互）。
- 每个资产对应一个 JumpServer 用户名，认证互相独立。
- 现有安全模型：路径全部来自服务端配置（log_dir 白名单），模型只能传不透明 file_id，从不传原始路径——自由查询工具将首次打破这一假设，是本次设计的核心风险点。
- 项目为个人/团队内部工具，本地 stdio 部署形态不变，无需考虑分发打包。

## Goals / Non-Goals

**Goals:**

- 多资产：配置、连接、查询、状态全链路支持 N 个资产，单资产故障隔离。
- 保活：应用层心跳 + 传输层 keepalive 双保险，断连可感知、可引导重连。
- 自由只读查询：在 log_dir 边界内允许白名单命令 + 管道的组合查询，默认拒绝一切写/执行/逃逸。
- 配套 skill：将"选资产 → 选模块 → 澄清意图 → 选工具 → 汇总"流程固化为 Claude Code skill。

**Non-Goals:**

- 不做远程多资产关联计算（如跨资产 join）；汇总由模型在本地完成。
- 不做压缩文件的流式解压下载，只支持远端 zgrep/zcat。
- 不改部署形态（仍是本地 stdio MCP + 本地 bridge），不做 MCPB/远程化。
- 不提供任何写远程文件的能力，无论用户如何要求。
- 不做 Web UI；资产选择交互由 Claude 通过 skill 完成。

## Decisions

### D1. 连接管理：`AssetConnection` + 连接池，认证与查询解耦

每个资产一个 `AssetConnection`（状态机：`disconnected → connecting → connected → needs_reauth`），由 `ConnectionManager` 持有 `dict[server, AssetConnection]`，每连接独立 `asyncio.Lock`（替代现有全局锁，粒度更细且天然隔离）。

**认证与查询解耦**：查询未连接资产时**不**阻塞等待 MFA，而是立即返回"需重新认证"错误 + 操作指引；用户调用 `reconnect_asset(server)` 后，gateway 在后台任务发起连接（状态 `connecting`），MFA 在 gateway 终端完成，随后状态变 `connected`，模型重试查询。

- 备选方案 A：查询触发同步认证并阻塞 HTTP 请求——否决：MFA 时长不可控，httpx 75s 超时会先炸，且并发查询会打出多个认证提示。
- 备选方案 B：启动时串行连接所有资产——保留为可选配置 `jumpserver.auto_connect: [server...]`，默认空；避免每次重启 gateway 都为用不到的资产做 MFA。

每资产独立 `ConsoleMFA` 实例（密码缓存按资产隔离），并发重连用 per-asset 锁去重。

### D2. Keep-alive：单个后台心跳任务 + 传输层参数双保险

lifespan 中启动一个 `asyncio` 后台任务，每 `keepalive_interval_minutes`（默认 25，配置范围 1–60）遍历所有 `connected` 资产，在该连接的 lock 下执行 `true` 作为心跳。失败/连接关闭 → 状态置 `needs_reauth`，记录时间与原因。asyncssh 的 `keepalive_interval=30, keepalive_count_max=3` 继续保留（现状已有），两层互补：传输层保 SSH 协议会话，应用层保 JumpServer 的"操作空闲"计时（JumpServer 以命令活动为准，仅传输层 keepalive 可能不被视为操作）。

- 备选：每连接一个心跳任务——否决：一个任务轮询即可，减少生命周期管理复杂度。

### D3. 自由查询：字符串命令 + 默认拒绝的静态校验，`cwd=log_dir` 执行

新增 `/execute-query`（gateway）与 `execute_query`（MCP 工具）。模型提交**完整命令字符串**与 `servers`/`module`，gateway 校验后在对应资产上以 `cwd=log_dir` 执行。校验管线（全部通过才下发远程）：

1. **字符级拒绝**：命令含换行、`;`、`&&`、`||`、反引号、`$(`、`>`、`<` 一律 400（管道 `|` 是唯一允许的控制符）。
2. **分段白名单**：按 `|` 用 `shlex.split` 分段，每段首 token ∈ 只读白名单 `{grep, egrep, fgrep, zgrep, head, tail, cat, zcat, wc, sort, uniq, awk, sed, cut, tr, tac, ls, find, du, file}`；段数 ≤ 5。
3. **逐命令参数校验**：`sed` 拒绝 `-i` 及脚本中 `w`/`W`/`e` 命令；`awk` 脚本拒绝 `system(`、`getline`、输出重定向 `>`、管道 `|`；`find` 拒绝 `-exec`/`-delete`/`-execdir`；`ls`/`find`/`du`/`file` 仅允许列出信息类输出。
4. **路径校验**：每段中非 flag token，若以 `/` 开头或含 `/`，规范化后 MUST 位于该模块 log_dir 内（绝对路径前缀匹配 + 拒绝 `..` 分量）；`-e` 后紧跟的 token 视为 pattern 豁免路径校验（支持 `grep -e '/api/v1'` 这类含斜杠的搜索词）。相对路径因 `cwd=log_dir` 天然受限，但仍拒绝含 `..` 的 token。
5. **资源上限**：输出统一经 `| head -c 100000` 截断（由服务端追加，非模型可见部分），timeout 60s；命令中引用的可校验文件数 ≤ 50。

返回体包含 `executed_command`、`exit_code`、`stdout`、截断标志，满足审计要求。

- 备选：结构化管道 DSL（模型传 `[{cmd, args}]` 数组）——否决：对模型更啰嗦、日志排查场景命令形态多变，字符串 + 严格校验的实现成本和可用性更平衡；校验逻辑本身即文档化白名单，可测试。
- 已知取舍：含 `/` 的 grep pattern 未用 `-e` 时会被误判为路径而拒绝——接受，skill 与工具 docstring 中说明规避写法。

### D4. MCP 工具面：`server` → `servers` 列表，批量 fan-out

**BREAKING**：`discover_log_files` / `search_logs` / `read_log_tail` / `execute_query` 的 `server: str` 改为 `servers: list[str]`（1–10 个）。gateway 端点同步改为 `servers` 列表，内部 `asyncio.gather` 并行 fan-out，每资产结果独立成组：`{"results": [{server, found, ...} | {server, error}]}`。单资产异常（断连/超时/校验失败）只影响其分组。`list_log_targets` 合并返回连接状态；新增 `list_connections` 与 `reconnect_asset` 工具。

### D5. 配置结构迁移

```yaml
jumpserver:
  host: ...
  port: 2222
  bind_host: 127.0.0.1
  bind_port: 8765
  auto_connect: []                # 可选：gateway 启动时自动认证的资产
  keepalive_interval_minutes: 25  # 可选：应用层心跳间隔
targets:
  server-172-26-73-80:
    description: ...
    ssh_username: "zhangxiaolong#root#172.26.73.80"   # 每资产独立
    modules: {...}
```

移除 `jumpserver.username` / `jumpserver.active_target`。启动时校验：每个 target 必须有 `ssh_username`，缺失则启动报错并指明资产名。README 同步更新迁移说明。

### D6. Skill 位置与形态

`​.claude/skills/log-query/SKILL.md`（项目级 skill，随仓库走）。frontmatter 含 name/description（description 写明触发场景：查日志、看报错、grep 日志等）。正文按 spec 的六条要求组织：流程图、选择交互规则（何时问/何时不问）、意图→工具映射表、断连引导话术、多资产汇总格式、只读边界声明。

## Risks / Trade-offs

- [黑名单绕过导致远程写入] → 默认拒绝架构：白名单外命令、任何 shell 控制符（除 `|`）、任何写原语在下发前静态拒绝；校验逻辑配单元测试覆盖 spec 中全部拒绝场景；`cwd=log_dir` + 路径前缀校验双保险。
- [awk/sed 图灵完备，静态校验存在理论绕过面] → 收紧到已知危险原语（system/getline/重定向/-i/w 命令）；若后续发现绕过，降级方案是从白名单移除 awk/sed（映射表已提供 grep/head/tail 替代路径）。
- [多资产并发 MFA 造成 gateway 终端提示交错] → `connecting` 状态互斥：同一时间只允许一个资产处于认证中，其余重连请求排队或提示稍后再试。
- [JumpServer 对单用户并发会话数有上限] → 连接按需建立 + auto_connect 默认空；文档提示用户只连接需要的资产。
- [心跳命令本身被 JumpServer 审计记录为高频操作] → `true` 是零输出命令，25 分钟一次频率低，影响可忽略；间隔可配置。
- [BREAKING 配置变更导致旧 gateway 启动失败] → 启动时明确报错并打印迁移示例；README 提供旧→新对照。

## Migration Plan

1. 更新 `config/log_targets.yaml`（按 D5 结构，为现有资产补 `ssh_username`）。
2. 部署新 `gateway_host.py` / `mcp_server.py`，重启 gateway 终端会话。
3. 用 `reconnect_asset` 或 `auto_connect` 完成各资产认证。
4. 回滚策略：git revert 本次 change 的提交并还原旧配置即可，无数据迁移。

## Open Questions

- JumpServer 空闲判定是否以"任意命令活动"为准（决定应用层心跳是否必要充分）？实现后以 25 分钟间隔实测验证；若仍断连，缩短间隔或改用 `exec_command` 级 ping。
- `find`/`ls`/`du` 是否保留在白名单（发现能力已由 `discover_log_files` 覆盖）？初版保留但仅允许信息类参数，若测试中误报多可移除。
