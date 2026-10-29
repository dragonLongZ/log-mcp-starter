# Spec Delta: session-keepalive

## ADDED Requirements

### Requirement: 周期性心跳保活
gateway SHALL 对每条已建立的 SSH 连接按可配置间隔（默认 25 分钟，MUST 显著小于 JumpServer 空闲超时约 2 小时）发送轻量心跳命令（如 `true` 或 `echo`），以防止会话被空闲强制关闭。

#### Scenario: 空闲期间心跳维持连接
- **WHEN** 某资产连接建立后无任何查询超过心跳间隔
- **THEN** gateway 自动发送心跳命令，连接保持活动，JumpServer 空闲计时被重置

#### Scenario: 心跳间隔可配置
- **WHEN** 配置文件中设置 `keepalive_interval_minutes` 为其他值（1–60）
- **THEN** 心跳按配置间隔发送；未配置时使用默认 25 分钟

### Requirement: 连接健康检测与状态标记
心跳失败、连接关闭或网络错误时，gateway SHALL 将该资产标记为"需重新认证"，记录断开时间与原因；后续查询该资产 MUST 返回包含明确操作指引的错误（重启/重连 gateway 并完成 MFA），而非模糊的 5xx 报错。

#### Scenario: 心跳失败标记状态
- **WHEN** 某资产连续心跳失败或连接被服务端关闭
- **THEN** 该资产状态变为"需重新认证"，`list_connections` 中可见断开原因与时间

#### Scenario: 断连后查询返回明确指引
- **WHEN** 对状态为"需重新认证"的资产发起查询
- **THEN** 返回错误信息明确说明该资产 SSH 会话已断开，并指引用户在 gateway 终端重新认证

### Requirement: 重新认证入口
gateway SHALL 提供触发指定资产重新建立连接的入口（HTTP 端点及对应 MCP 工具 `reconnect_asset`），触发后在 gateway 终端进行交互式认证；认证期间该资产状态为"认证中"，并发重连请求 MUST 被去重。

#### Scenario: 主动重连
- **WHEN** 调用 `reconnect_asset(server)` 且该资产当前断开
- **THEN** gateway 终端提示输入密码/MFA，认证成功后资产状态变为"已连接"

#### Scenario: 并发重连去重
- **WHEN** 同一资产的重连请求在认证完成前再次到达
- **THEN** 返回"认证进行中"提示，不产生第二个认证提示

### Requirement: 传输层 keepalive 参数保留
SSH 连接建立时 SHALL 继续启用 asyncssh 传输层 keepalive（`keepalive_interval`），与应用层心跳命令互补；两者任一生效即可维持会话。

#### Scenario: 传输层参数生效
- **WHEN** 建立任意资产的 SSH 连接
- **THEN** 连接参数包含 `keepalive_interval=30` 与 `keepalive_count_max=3`
