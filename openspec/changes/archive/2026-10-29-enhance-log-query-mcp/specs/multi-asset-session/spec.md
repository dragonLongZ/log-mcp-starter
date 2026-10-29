# Spec Delta: multi-asset-session

## ADDED Requirements

### Requirement: 多资产独立配置
配置文件 SHALL 支持登记多个资产（target），每个资产携带自己的 JumpServer SSH 用户名（如 `user#root#ip` 格式）与模块目录列表；每个模块 MAY 携带 `description` 业务描述字段，`list_log_targets` SHALL 将其透出，作为模型判断模块业务归属、匹配用户话语的主要依据。系统 MUST 只接受配置中已登记的资产与模块，未登记的一律拒绝。

#### Scenario: 配置多个资产
- **WHEN** 配置文件中登记了两个及以上 target，各自含 `ssh_username` 与 `modules`
- **THEN** `list_log_targets` 返回全部资产及其模块列表，不再返回单一 `active_target`

#### Scenario: 模块描述透出
- **WHEN** 模块配置了 `description` 字段
- **THEN** `/targets` 端点与 `list_log_targets` 的模块条目中包含该描述；未配置时描述为空且不影响任何查询

#### Scenario: 请求未登记资产
- **WHEN** 查询请求中的 server 不在配置 targets 中
- **THEN** 返回 404 错误，提示该资产不在白名单内

#### Scenario: 旧配置字段不再被依赖
- **WHEN** 配置中不存在全局 `jumpserver.username` 与 `jumpserver.active_target`
- **THEN** gateway 正常启动，按各 target 的 `ssh_username` 建立连接

### Requirement: 配置热同步
gateway SHALL 在每次 API 请求时将连接池与磁盘上的最新配置同步：新登记的资产立即出现在连接清单并可发起认证；被移除的资产条目连同其活动连接一并清理；`ssh_username`/主机/端口变更的资产重建条目（旧连接关闭、需重新认证）。修改配置文件 MUST NOT 要求重启 gateway 进程。

#### Scenario: 新增资产免重启生效
- **WHEN** gateway 运行中，用户在配置文件中新增一个资产，随后调用 list_log_targets / list_connections
- **THEN** 新资产立即出现且状态为 disconnected，可对其调用 reconnect_asset 完成认证

#### Scenario: 移除资产清理连接
- **WHEN** 配置中删除某已连接资产后发起任意请求
- **THEN** 该资产从连接清单消失，其 SSH 连接被关闭

#### Scenario: 凭据变更重建条目
- **WHEN** 某资产的 ssh_username 在配置中被修改
- **THEN** 旧连接关闭，条目重置为 disconnected，需以新凭据重新认证

### Requirement: SSH 连接按需建立与复用
gateway SHALL 为每个资产维护至多一条 SSH 连接；首次查询某资产时按需触发交互式认证（密码/MFA 在 gateway 终端完成），认证通过后连接被后续查询复用，直至关闭。

#### Scenario: 首次查询触发认证
- **WHEN** 对某资产的查询到达且该资产无活动连接
- **THEN** gateway 在自身终端提示输入密码/MFA，认证成功后执行查询；MCP 调用方收到"等待 gateway 终端完成认证"的明确提示或阻塞至认证完成

#### Scenario: 连接复用
- **WHEN** 某资产已有活动连接，再次查询该资产
- **THEN** 直接复用连接执行命令，不重复认证

### Requirement: 资产间故障隔离
单个资产的 SSH 连接断开、认证失败或命令出错 MUST NOT 影响其他资产的查询能力。

#### Scenario: 一个资产断连不影响其他资产
- **WHEN** 资产 A 的连接已关闭，对资产 B 发起查询
- **THEN** 资产 B 的查询正常执行并返回结果

### Requirement: 连接状态可查询
gateway SHALL 提供所有已配置资产的连接状态（未连接 / 已连接 / 需重新认证 / 认证中），并通过 MCP 工具 `list_connections` 暴露给模型。

#### Scenario: 查看连接状态
- **WHEN** 调用 `list_connections`
- **THEN** 返回每个资产的状态、上次心跳时间与断开原因（如有）

### Requirement: 多资产批量查询
MCP 查询工具（discover/search/read-tail/freeform）SHALL 支持一次传入多个 server，对每个资产独立执行同一查询并返回按资产分组的结果；单个资产失败 MUST 以该资产的错误条目呈现，不阻断其他资产结果。

#### Scenario: 双资产关键词搜索
- **WHEN** `search_logs` 传入 servers=[A, B]、相同 module 与 keyword
- **THEN** 返回结果按资产分组，各自包含匹配文本或错误信息

#### Scenario: 其中一个资产不可用
- **WHEN** 批量查询中资产 A 需重新认证、资产 B 正常
- **THEN** 资产 B 的结果正常返回，资产 A 条目包含"需重新认证"的明确提示
