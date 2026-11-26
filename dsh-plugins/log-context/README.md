# 日志上下文（log-context）— DSH 插件

把本仓库已有的 JumpServer 日志查询能力做成 DSH 插件：右栏点选「资产 / 模块」或「K8S 命名空间 / Pod」，
一键把这次要查的坐标写进输入框，于是你只需要补一句「什么问题」，不必每次口述资产和目录。

## 给别人用（多用户共享一个网关）

插件已做到**装好即可用、不用改任何文件**：连接信息在右栏「连接设置」里填，存在
`$DSH_HOME/log-context/settings.json`，不依赖本仓库路径（`cordis.patch.yml` 里没有硬编码路径）。

对方需要拿到三样东西（都由你提供）：

| 项 | 值 |
|---|---|
| 网关地址 | `http://10.12.0.101:8765` |
| MCP 地址 | `http://10.12.0.101:8000/mcp` |
| 访问令牌 | 服务器上 `local.token` 的内容 |

他安装插件 → 打开右栏「连接设置」→ 填三项 → 点「试连」（同时校验网关与 MCP）→ 保存。

### ⚠️ 当前的安全边界（务必先看）

**token 是全局共享的一把，没有按用户 / 资产做授权。** 拿到 token 的人：

- 能查询**全部资产**的日志与系统信息；
- 能通过 `/dashboard/targets` **新增 / 修改 / 删除资产**（等于能让你的服务器去 SSH 任意地址）；
- 能改自定义命令白名单。

给出 token 之前至少要做：

1. **关掉网页写权限**（`config/settings.yaml`）：
   ```yaml
   dashboard:
     write_enabled: false
   ```
2. **确认网关只在内网 / VPN 可达**，不要暴露公网。
3. 想清楚是否愿意让所有人看所有资产——若不愿意，需要「多 token + 资产级授权」，当前版本**没有**这个能力。

### 分发方式

**已发布到 npm：[`@zxlll/dsh-log-context`](https://www.npmjs.com/package/@zxlll/dsh-log-context)。**

推荐让对方用 `dsh` 命令装（在终端，不在 DSH 会话里）：

```bash
dsh plugin --profile web add @zxlll/dsh-log-context
```

一条命令完成：pnpm 装包 → 写入 profile 依赖 → 追加进 `dsh.profile.bundles` 组合包列表。
装完重启 DSH（或等热重载），右栏就有「日志上下文」标签页。卸载用
`dsh plugin --profile web remove @zxlll/dsh-log-context`。

也可以让 DSH 会话里的 agent 代劳：

```
plugin_manager(install_bundle, target="@zxlll/dsh-log-context")
```

- **直接给目录**（改源码时）：`dsh-plugins/log-context/`，对方用
  `plugin_manager(install_bundle, target="<该路径>")`，或 `dsh plugin --profile web add ./dsh-plugins/log-context`。
  **不要把 `.token` 一起发出去**，让他用自己的 `settings.json`。
- **自己发新版本**：改完 `package.json` 的 `version` → `npm publish`。**不要**把 `name` 改回 `@local/...`：
  `client.js` 的模块 id 必须与 `package.json` 的 `name` 一致，否则客户端模块登记不上、右栏标签页不出现。
  同理 `cordis.patch.yml` 的插件行用相对路径 `./index.js`，对 npm 安装和本地 link 都成立。


```
┌ 右栏：日志上下文 ──────────────┐
│ ● 日志上下文        [刷新]      │
│ 环境  [全部][生产][测试][研发]  │
│ 🔍 搜索资产 / IP / 模块         │
│ ● server-172.26.73.42-2.0-prod │
│ ● server-172.26.73.47-2.0-prod │
│ 模块  ◉ member  会员服务        │
│ 动作  [查今天 error][最近200行] │
│ 预览  【日志上下文】…            │
│ [插入到输入框] [直接发送]       │
└─────────────────────────────────┘
```

插入后输入框里是：

```
【日志上下文】
资产：server-172.26.73.42-2.0-prod（2.0云平台生产环境…）
模块：member（会员服务）
日志目录：/data/logs/parking/member
时间范围：今天（today）
日志类型：error 优先
动作：搜索今天 error 日志中的异常…
问题：
```

光标停在「问题：」后面，补一句就能发。模型看到这个块，会直接调用 `mcp__logmcp__*` 工具，
不再反问「查哪台机器 / 哪个模块」。

## 组成

| 部分 | 作用 |
|---|---|
| `cordis.patch.yml` → `log-context-mcp` | 让 DSH 通过 `@deepseek-ai/dsh-mcp-client` 连上本项目已有的 `mcp_server.py`，工具名 `mcp__logmcp__*` |
| `cordis.patch.yml` → `log-context` | 本插件的 Host + Client 两半 |
| `index.js`（Host 半） | 读 `config/log_targets.yaml`、提供 `/log-context/api`、代理重连 / kubectl 只读查询、注册系统提示 |
| `client.js`（Client 半） | 右栏「日志上下文」标签页 + 输入框左侧入口按钮 |

Host 半**不改动** Python 侧业务逻辑，只读取配置并调用 gateway 的既有接口。

## 部署形态

本项目按 README 的「远程部署」使用：`gateway_host.py` 和 `mcp_server.py` 都跑在服务器上（示例为 **10.12.0.101**），

| 服务 | 地址（示例） | 用途 |
|---|---|---|
| MCP | `http://10.12.0.101:8000/mcp` | 模型侧 `mcp__logmcp__*` 工具 |
| gateway | `http://10.12.0.101:8765` | 资产配置、连接状态、kubectl 只读查询 |

实际地址在右栏「连接设置」里填，不用改文件。

Windows 本机只跑 DSH。地址与 token 都**不用改文件**：在右栏「日志上下文」→「连接设置」里填。

## 前置条件

1. **远程** 10.12.0.101 上 `gateway_host.py` 与 `mcp_server.py` 都在运行，且 gateway 里已完成 JumpServer 认证
   （未认证的资产在面板里显示「未连接」，点「重连」会触发对应资产重新认证，密码 / MFA 在 gateway 终端完成）。
2. 手上有该服务器上的 `local.token` 内容（网关与 MCP 共用同一把）。
3. profile 里安装了本 bundle（见下）。

右栏顶部圆点表示 gateway 状态；资产行左侧圆点表示该资产的 SSH 连接状态，未连接时可点「重连」。

## 安装

**方式一：npm 包（推荐）** —— 在终端执行，`web` 换成你的 profile 名：

```bash
dsh plugin --profile web add @zxlll/dsh-log-context
```

`dsh plugin` 把参数转发给 profile 目录里的 pnpm，并在装完后自动把该包追加到
`dsh.profile.bundles`，重启 DSH 后生效。需要本机有 pnpm。

**方式二：本地目录**（改源码时）——在 DSH 会话里让 agent 执行：

```
plugin_manager(install_bundle, target="D:\\pythonProject\\log-mcp-starter\\dsh-plugins\\log-context")
```

或终端 `dsh plugin --profile web add ./dsh-plugins/log-context`。

装好后右栏即可看到标签页，输入框左侧也会出现「日志上下文」按钮。

卸载：

```bash
dsh plugin --profile web remove @zxlll/dsh-log-context
```

### 升级

```bash
dsh plugin --profile web add @zxlll/dsh-log-context@0.1.1   # 指定新版本
dsh plugin --profile web update                             # 或按 package.json 范围升级
```

升级后刷新页面（客户端包要重新下发）；Host 半改动需重启 DSH。

## 配置

### 在右栏配（推荐）

打开右栏「日志上下文」→ 点「连接设置」→「配置」，填三项后 **保存**：

| 字段 | 说明 | 示例 |
|---|---|---|
| 网关地址 | 资产目录、连接状态、kubectl 只读查询 | `http://10.12.0.101:8765` |
| MCP 地址 | 模型侧 `mcp__logmcp__*` 工具 | `http://10.12.0.101:8000/mcp` |
| 访问令牌 | `local.token` 的内容 | `_TKs0MDWIb...` |

- 「试连」只发一次 `GET /health`，**不落盘**，用于先验证再保存。
- 保存后**立即生效**：Host 半每次请求都重新读设置；MCP 行会自动重启一次，让新地址与新 token 生效。
- token 输入框**留空表示保持不变**；界面上只显示掩码（如 `tok-***3456`），永远不回明文。
- 保存位置：`$DSH_HOME/log-context/settings.json`（未设 `DSH_HOME` 时为 `~/.dsh/log-context/settings.json`）。
  放在 DSH home 而不是仓库目录，因为里面有 token，不能进 git；这也让**别人 clone 仓库后不用改任何文件**就能连自己的网关。

### 文件里配（可选，作为默认值）

`cordis.patch.yml` 里的两行仍然可以用，但只作为**出厂默认值**：`settings.json` 里没有对应字段时才生效。

```yaml
- id: log-context-mcp
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: logmcp
    transport: streamable-http
    url: !!js '…读 settings.json 的 mcpUrl，读不到用 http://127.0.0.1:8000/mcp…'
    headers:
      Authorization: !!js '…读 settings.json 的 token，读不到回退 .token 文件…'
    failOnStartupError: false
    reconnect:
      enabled: true
      maxAttempts: 120      # 开机时 VPN / 网络还没就绪也能撑住，不会很快烧完重连预算
      maxDelayMs: 30000

- id: log-context
  name: './index.js'                                 # 相对本 patch 文件；npm 包与本地目录都适用
  config:
    projectDir: 'D:/pythonProject/log-mcp-starter'   # 仅用于本地 YAML 兜底与默认 token 文件位置
    gatewayBaseUrl: 'http://127.0.0.1:8765'          # 出厂默认值，会被 settings.json 覆盖
```

> `!!js` 表达式由 Loader 在插件上下文中求值，因此能动态算出绝对路径，不受工作目录影响。
> 改动这段仍然需要重装/重启 bundle；**改地址/token 则不需要**——走右栏保存即可。

### log_targets.yaml 的可选字段

全部向后兼容，不写也不影响现有 gateway：

```yaml
targets:
  server-172.26.73.83-2.0-dev:
    description: "2.0平台研发环境 K8s 资产节点"
    ssh_username: "zhangxiaolong#root#172.26.73.83"
    kind: k8s                    # 可选：k8s | linux；缺省按 description 里是否含 K8s 推断
    env: "研发"                   # 可选：环境名；缺省从 description 推断（生产/测试/研发/网关/演示）
    k8s:
      context: "2.0-dev"         # 可选：仅用于展示
      namespaces: ["parking"]    # 可选：常用命名空间；不写就点「拉取」动态查
```

## 面板里能做什么

- **环境筛选 / 搜索**：按描述推断的环境分组，搜索资产名、IP 或模块名。
- **重连（资产）**：对未连接的资产触发 gateway 重新认证（密码 / MFA 仍在 gateway 终端完成）。
- **重连 MCP**：面板标题栏的 `MCP` 按钮重启 DSH 里那条 MCP 连接（`fiber.restart`，不写配置）。
  MCP 工具未就绪时，面板顶部会显示黄色提示条并给出「重连 MCP」按钮。
- **Linux 资产**：选模块 → 选动作模板（查今天 error、最近 200 行、搜关键字、系统状态、只给上下文）。
- **K8S 资产**：选命名空间（可「拉取」）→ 填或选 Pod → 选动作模板（Pod 日志 / 事件 / 状态 / 工作负载）。
- **时间范围**：今天 / 昨天 / 指定日期（`YYYY-MM-DD`）。
- **插入到输入框**：保留输入框里已有的内容，在光标处插入；编辑器拒绝时回退为整段替换。
- **直接发送**：写入后立刻提交。

最近一次选择（资产、模块、模板、时间范围、命名空间）存在浏览器 localStorage，
刷新页面或重开会话后仍在。

## 模型侧

- 工具：`mcp__logmcp__list_log_targets`、`list_connections`、`reconnect_asset`、
  `discover_log_files`、`search_logs`、`read_log_tail`、`execute_query`、`execute_system_query`。
- `log_context_mcp_restart`：插件自带的手动重连工具——`mcp__logmcp__*` 工具被移除后，
  模型仍可用它把工具救回来（和面板里的「重连 MCP」是同一段逻辑）。
- 系统提示（`index.js` 中的 `SYSTEM_PROMPT_SECTION`）说明：看到「【日志上下文】」块就直接按块内坐标查询，
  不要反问资产、模块；K8S 场景用 `execute_system_query` 执行只读 kubectl。

## 故障排查

| 现象 | 处理 |
|---|---|
| 右栏没有「日志上下文」 | 确认 bundle 已安装且行状态为 enabled；刷新页面（不是新开标签，是重载当前页面） |
| 面板顶部圆点是红色 | gateway 不可达或 token 不匹配：先在「连接设置」点「试连」看具体报错，再 `Test-NetConnection <你配的主机> -Port 8765` |
| 面板里资产显示「未连接」 | 该资产在 gateway 侧还没认证：点资产行的「重连」，到 gateway 终端完成密码 / MFA |
| 模型没有 `mcp__logmcp__*` 工具 | 先点面板标题栏的 **重连 MCP**（或让模型调用 `log_context_mcp_restart`）；仍不行再确认「连接设置」里的 MCP 地址与 token |
| 工具一开始有、重启后消失 | 启动时 VPN / 网络未就绪，MCP 客户端重连预算耗尽后会移除工具并停止重连：点 **重连 MCP** 即可恢复（本 patch 也把 `maxAttempts` 调到了 120 兜底） |
| 改了右栏「连接设置」没生效 | Host 半立即生效；MCP 行已自动重启。若仍不生效，点标题栏 **重连 MCP** |
| 改完 `cordis.patch.yml` 没生效 | 那是**默认值**配置，行配置只在重新挂载时读取：`remove_bundle` + `install_bundle`，或重启 DSH |
| K8S「拉取」报 `UNKNOWN` / 被拒绝 | 该资产未连接，或 `kubectl` 只读命令被 `system_guard.py` 拦截；先把资产连上再拉 |
| 提示「未知方法」 | Client / Host 版本不一致，重新安装 bundle |

## 开发期自检

```powershell
cd D:\pythonProject\log-mcp-starter
node dsh-plugins/log-context/check-client.mjs    # Client 半：语法 + 注册 + 渲染 + 提示词（无网络）
node dsh-plugins/log-context/check-host.mjs      # Host 半：假 gateway 验证 catalog/状态合并/离线降级（无网络）
node dsh-plugins/log-context/check-settings.mjs  # 连接设置：保存/试连/掩码/损坏降级（无网络）
.venv\Scripts\python dsh-plugins/log-context/check-patch.py   # patch 里的 !!js 表达式能解析且求值正确
node dsh-plugins/log-context/check-live.mjs      # 联调：真实访问 gateway，只读（可设 LOG_CONTEXT_GATEWAY 覆盖地址）
```

改了 `client.js` 之后需要刷新页面（本 profile 没有跑 `dev:web` 时 HMR 不会自动重建客户端包）；
改了 `index.js` / `cordis.patch.yml` 之后 `remove_bundle` + `install_bundle` 一次（或重启 DSH）。

## 边界

- 右栏标签页只在有会话时存在；空会话页用输入框左侧按钮进入。
- 只读：面板只调用 gateway 的 `/targets`、`/connections`、`/connect`、`/execute-system-query`，
  不提供任何写操作；`/log-context/api` 走 `ctx.connection.requestRejection` 做同源校验。
- 一次查询仍是「单资产 + 单模块」（与现有 MCP 工具一致）；多资产批量留待后续。
