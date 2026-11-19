# JumpServer Multi-asset Directory-discovery Log MCP

按"只配置目录、运行时发现文件"思路设计的多资产日志查询 MCP。你只需登记每个资产每个模块允许访问的日志目录，不必配置 `info.log`、`error.log` 或每天变化的归档文件名。

## 工作方式

1. `gateway_host.py` 在独立终端运行。启动后不强制连接任何资产；通过 `reconnect_asset` 工具（或 `POST /connect`）按需发起认证，密码/MFA 提示出现在该终端。
2. 每个资产一条独立 SSH 连接，认证后复用；后台心跳任务每 25 分钟（可配置）执行一次 `true`，防止 JumpServer 空闲超时（约 2 小时）强制断连。
3. MCP 查询指定资产/模块时，在服务端配置的 `log_dir` 下执行受限 `find`，获取文件相对路径、大小和修改时间，识别文件名中的常见日期格式与 info/error 类型。
4. 模型不能直接传任意远程路径；`file_id` 会在每次操作时重新对照目录发现结果进行验证。多资产批量查询时，每个资产只匹配属于自己的 `file_id`。
5. `execute_query` 允许提交受限的只读命令组合（grep 正则、zgrep 压缩日志、统计、管道），经服务端静态校验后以 `cwd=log_dir` 执行。

## 配置

`config/log_targets.yaml`：每个资产携带自己的 `ssh_username`，每个模块只需 `log_dir`。

```yaml
jumpserver:
  host: "jumpserver.example.com"
  port: 2222
  bind_host: "127.0.0.1"
  bind_port: 8765
  auto_connect: []                # 可选：启动时自动认证的资产列表
  keepalive_interval_minutes: 25  # 可选：应用层心跳间隔（1-60）

log_timezone: "Asia/Shanghai"

targets:
  server-172-26-73-80:
    description: "Linux asset 172.26.73.80"
    ssh_username: "zhangxiaolong#root#172.26.73.80"
    modules:
      member:
        log_dir: "/data/logs/parking/member"
  server-172-26-73-81:
    description: "Linux asset 172.26.73.81"
    ssh_username: "zhangxiaolong#root#172.26.73.81"
    modules:
      order-api:
        log_dir: "/data/apps/order-api/logs"
```

**从旧版迁移**（BREAKING）：删除 `jumpserver.username` 与 `jumpserver.active_target`，把用户名移到每个 target 的 `ssh_username` 字段。路径必须是目标机器上的绝对路径且位于该资产授权范围内。

## MCP 工具

| 工具 | 用途 |
|---|---|
| `list_log_targets()` | 列出全部资产、模块、日志目录与连接状态 |
| `list_connections()` | 各资产 SSH 连接状态、上次心跳、断开原因 |
| `reconnect_asset(server)` | 触发指定资产重连认证（MFA 在 gateway 终端完成） |
| `discover_log_files(servers, module, day, log_type, max_files)` | 动态发现候选日志文件，返回不透明 `file_id` |
| `search_logs(servers, module, keyword, day, log_type, max_results, file_ids)` | 固定字符串搜索（grep -F），支持多资产 |
| `read_log_tail(servers, module, day, log_type, lines, file_ids)` | 读取文件末尾，支持多资产 |
| `execute_query(servers, module, command)` | 受限自由只读查询（白名单命令 + 管道，限定 log_dir） |
| `execute_system_query(servers, command)` | 系统/Docker/K8s 只读查看（三段式分类，不限定目录） |

`servers` 接受 1–10 个资产名，结果按资产分组，单资产失败不阻断其他资产。`day` 支持 `today`、`yesterday` 或 `YYYY-MM-DD`；`log_type` 支持 `info`、`error`、`both`。

## execute_query 安全模型

命令在下发 SSH 前经过默认拒绝的静态校验（`command_guard.py`）：

- **白名单命令**：grep、egrep、fgrep、zgrep、head、tail、cat、zcat、wc、sort、uniq、awk、sed、cut、tr、tac、ls、find、du、file；管道 `|` 最多 5 段。
- **拒绝**：`;`、`&&`、`||`、反引号、`$(`、换行；输出重定向 `>`/`>>`；路径中的 `$变量`/`~`（shell 展开会绕过路径检查）。awk 内部的 `>` 比较允许。
- **逐命令规则**：sed 禁 `-i` 与 `w`/`W`/`e` 命令；awk 禁 `system()`/`getline`/`print >`/−f；find 禁 `-delete`，`-exec` 允许但嵌套命令须在白名单内；sort 禁 `-o`；uniq 最多一个文件参数。
- **路径限定**：相对路径以 `log_dir` 为基准（服务端强制 `cd log_dir`），绝对路径必须位于 `log_dir` 内，`..` 一律拒绝；含 `/` 的搜索词用 `grep -e 'PATTERN'`。
- **资源上限**：输出 100KB 截断、超时 60s、单次最多 50 个文件参数。远程命令用 `bash -c 'set -o pipefail; … | head -c'` 执行，失败不会被截断管道掩盖（`141` 视为截断）。

校验规则可用 `uv run pytest test/` 回归；端到端覆盖测试为 `uv run python test/e2e_check.py`（需要 gateway 与资产认证）。

## execute_system_query 安全模型

`system_guard.py` 三段式分类：安全命令自动执行、危险命令自动拦截、未知命令返回 UNKNOWN 让用户确认。

- **白名单**：OS 查看（ps、df、free、top -b、journalctl 等）、Docker 只读子命令、kubectl 只读子命令。
- **黑名单**：rm/mv/cp/dd、sudo/su、reboot/shutdown、chmod/chown、curl/wget/nc/ssh、bash/python、systemctl start/stop、docker rm/exec/run、kubectl delete/apply/exec 等。
- **额外限制**：grep/head/tail/awk 等只能接在 `|` 之后（不能单独读文件）；`top` 必须带 `-b`；`journalctl` 禁 `-f`；`kubectl get/describe secret(s)` 与 `kubectl config view --raw` 被拒绝（防凭据泄露）；禁止输出重定向、命令串联与 `$(`。

### 自定义命令白名单（网页可维护，实时生效）

内置白名单不可能覆盖所有只读命令（`htop`、`ifconfig`、`docker compose ls`…），遇到 UNKNOWN 时不必改代码：

1. 浏览器打开 `http://<gateway>:8765/dashboard?token=<local.token>`；
2. 在「自定义命令白名单」里填**命令前缀**（如 `docker compose ls`）→ 点「添加 / 更新」；
3. 该命令**下一次请求立即放行**，无需重启 gateway；删除后立即恢复 UNKNOWN。

也可用「试运行校验」输入一条完整命令，先看网关会判定成 `allowed` / `rejected` / `unknown`（不发往任何资产）。

存储与接口：

| 项 | 说明 |
|---|---|
| 存储 | `config/system_allowlist.yaml`（可手工编辑，改完同样实时生效） |
| `GET /dashboard/allowlist` | 列出当前条目、文件路径与解析警告 |
| `POST /dashboard/allowlist` | `{name, denied[], note}` 新增或覆盖一条；`denied` 可填 flag 或子命令 |
| `DELETE /dashboard/allowlist?name=htop` | 删除一条 |
| `POST /dashboard/allowlist/check` | `{command}` 试运行判定，返回 `{verdict, message}` |

匹配规则与边界（重要）：

- **前缀匹配**：按 token 前缀匹配，登记 `docker compose ls` 只放行以这三个 token 开头的命令；`name` 最多 4 个 token，且不允许 `; & | $ \` > < ( )` 等 shell 元字符。
- **黑名单优先**：`rm`、`sudo`、`docker rm`、`kubectl delete` 等黑名单命令**不能**被登记，也不能用更短前缀绕过（例如登记 `docker` 会被拒绝，因为它会放行 `docker rm`）。
- **通用规则始终生效**：禁止输出重定向、命令串联（`; && ||`）、换行与 `$(`/反引号，awk `system()`/`getline`/`print >` 仍被拒绝；管道最多 3 段。
- **每条可再禁参数**：支持两种形态——flag（`-o`、`--output`）与子命令（`VM.set_flag`、`GC.run`）。
  例如登记 `jcmd` 且禁用 `VM.set_flag,GC.run,GC.run_finalization`，则 `jcmd 1234 VM.version` 放行、
  `jcmd 1234 GC.run` 仍被拒绝；登记 `ncdu` 且禁用 `-o`，则 `ncdu` 放行、`ncdu -o out.txt` 仍被拒绝。
- 白名单文件损坏不会拖垮网关：坏条目被跳过，并在接口 `warnings` 与页面底部提示。

## 配套 Skill

`.claude/skills/log-query/SKILL.md` 提供项目级 skill，规范 Claude 的查询流程：连接状态前置检查 → 资产/模块选择（未指明必问、已指明不问）→ 时间与类型澄清 → 意图-工具映射 → 多资产分组汇总 → 只读边界。在本项目目录启动 Claude Code 即自动可用。

## DSH 插件（日志上下文）

`dsh-plugins/log-context/` 是同一套能力的 DSH 插件：右栏点选「环境 → 资产 → 模块 / K8S 命名空间、Pod → 动作模板」，
一键把「查哪里」写进对话输入框，你只需要补一句问题。详见 [dsh-plugins/log-context/README.md](dsh-plugins/log-context/README.md)。

它由两部分组成，都不改动上面的 Python 业务代码：

1. **能力**：bundle patch 插入 `@deepseek-ai/dsh-mcp-client`，连本项目的 `mcp_server.py`，
   模型拿到 `mcp__logmcp__*` 工具；并追加一段系统提示，看到「【日志上下文】」块就直接按块内坐标查询。
2. **交互**：Host 半读 `config/log_targets.yaml` 并提供 `/log-context/api`；Client 半注册右栏「日志上下文」标签页
   与输入框左侧入口按钮。

安装（DSH 会话里执行）：

```
plugin_manager(install_bundle, target="D:\\pythonProject\\log-mcp-starter\\dsh-plugins\\log-context")
```

部署形态：`gateway_host.py` + `mcp_server.py` 跑在服务器 `10.12.0.101`（8000 = MCP，8765 = gateway），
Windows 本机只跑 DSH。插件用的 token 在 `dsh-plugins/log-context/.token`（已被 `.gitignore` 忽略），
内容是该服务器上的 `local.token` —— 注意仓库根目录的 `local.token` 可能是本机旧 token，两者不一定相同。
地址与 token 换机器时改 `dsh-plugins/log-context/cordis.patch.yml` 里的 `url`、`gatewayBaseUrl` 与 `.token`。

## 部署模式

### 远程部署（推荐，gateway 作为 systemd 服务 + MCP 独立进程）

```
┌─ 服务器 (Linux) ─────────────────────────────────────┐
│                                                       │
│  systemd: gateway_host.py     ← SSH 持久 + 心跳        │
│  │  监听 127.0.0.1:8765                               │
│  │  Web 仪表盘 http://server:8765/dashboard           │
│  └────────────┬───────────────────────────────────────│
│               │ HTTP bearer                           │
│               ▼                                        │
│  mcp_server.py (streamable HTTP)                       │
│  监听 0.0.0.0:8000/mcp + bearer 鉴权                  │
└───────────────────────────────────────────────────────┘
```

1. **安装依赖**
```bash
uv sync
uv run python create_token.py
```

2. **启动 Gateway（systemd 永久托管）**
```ini
# /etc/systemd/system/log-mcp-gateway.service
[Unit]
Description=Log MCP Gateway
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/log-mcp
ExecStart=/opt/log-mcp/.venv/bin/python gateway_host.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload && systemctl enable --now log-mcp-gateway
```

3. **Web 仪表盘认证**
   浏览器打开 `http://<服务器IP>:8765/dashboard?token=<local.token 内容>`，在表格中为每个资产填入 JumpServer 密码 + MFA 验证码，点击连接。连接成功后关闭浏览器即可——gateway 由 systemd 托管，SSH 由心跳任务维持。

4. **启动 MCP 远程服务**
```bash
uv run python mcp_server.py
# 监听 0.0.0.0:8000/mcp，bearer token 鉴权
```

5. **Claude Code 远程连接**
   参照 `claude-mcp.example.json`，配置 `type: "http"` + `url` + `Authorization` header。

### 本地开发（双进程 stdio 模式）

```powershell
# 终端 A：启动 gateway
uv run python gateway_host.py
# 终端 B：启动 MCP
uv run python mcp_server.py
```
MCP 以 stdio 与 Claude Code 通信（`claude-mcp.example.json` 方案 B）。

## 安装与运行（本地开发）

## 安全边界及当前限制

- 只允许操作配置文件中登记的资产/模块；模型不能传原始 SSH 命令，`execute_query` 的命令经白名单校验，路径限定在 `log_dir` 内。
- 远程发现范围限制在 `log_dir` 下最多 5 层、最多 2000 个文件；查询最多使用 50 个文件。
- 压缩文件（.gz 等）会出现在发现列表中，`search_logs`/`read_log_tail` 不搜索压缩文件；用 `execute_query` 的 zgrep/zcat 查询。
- 日期识别覆盖常见文件名格式；非常规名称用 `discover_log_files` 让模型读取候选清单后以 `file_ids` 指定。
- 使用 GNU `find -printf`，目标系统应为常见 Linux/GNU find 环境。
- 心跳命令 `true` 每 25 分钟一次（可配置 1–60），会被 JumpServer 审计记录为正常操作。
- SSH 主机密钥验证保持开启；不要设置 `known_hosts=None`。本地 bridge 只监听 127.0.0.1 且需要 bearer token，切勿暴露到局域网或提交 token。
- 这是可供验证的开发工程，并非已完成生产安全审计。MFA 仍由用户正常完成；连接失效时按提示重新认证。
