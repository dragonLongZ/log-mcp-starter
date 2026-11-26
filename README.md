# JumpServer Multi-asset Directory-discovery Log MCP

按"只配置目录、运行时发现文件"思路设计的多资产日志查询 MCP。你只需登记每个资产每个模块允许访问的日志目录，不必配置 `info.log`、`error.log` 或每天变化的归档文件名。

## 工作方式

1. `gateway_host.py` 在独立终端运行。启动后不强制连接任何资产；通过 `reconnect_asset` 工具（或 `POST /connect`）按需发起认证，密码/MFA 提示出现在该终端。
2. 每个资产一条独立 SSH 连接，认证后复用；后台心跳任务每 25 分钟（可配置）执行一次 `true`，防止 JumpServer 空闲超时（约 2 小时）强制断连。
3. MCP 查询指定资产/模块时，在服务端配置的 `log_dir` 下执行受限 `find`，获取文件相对路径、大小和修改时间，识别文件名中的常见日期格式与 info/error 类型。
4. 模型不能直接传任意远程路径；`file_id` 会在每次操作时重新对照目录发现结果进行验证。多资产批量查询时，每个资产只匹配属于自己的 `file_id`。
5. `execute_query` 允许提交受限的只读命令组合（grep 正则、zgrep 压缩日志、统计、管道），经服务端静态校验后以 `cwd=log_dir` 执行。

## 配置

配置分成**两处**，边界清晰：

| 文件 | 内容 | 维护方式 | 进版本库 |
|---|---|---|---|
| `config/settings.yaml` | 部署设置：JumpServer 地址/端口、监听地址、心跳间隔、日志时区、DB 路径、网页写开关 | 手工编辑，改完重启 gateway | ❌（含内部域名，已 gitignore） |
| `data/logmcp.db` | 运行期数据：资产、模块、自定义命令白名单、审计 | **网页仪表盘 `/dashboard` 增删改** | ❌（已 gitignore） |

仓库里只有 `config/settings.example.yaml` 与 `config/log_targets.example.yaml` 两个示例。**首次使用先复制**：

```bash
cp config/settings.example.yaml config/settings.yaml
# 然后按需修改 host / bind_* / storage.db_path
```

```yaml
# config/settings.yaml
jumpserver:
  host: "jumpserver.example.com"
  port: 2222
  bind_host: "0.0.0.0"          # 只给本机用就写 127.0.0.1
  bind_port: 8765
  auto_connect: []              # 可选：启动时自动认证的资产
  keepalive_interval_minutes: 25
mcp_server:
  bind_host: "0.0.0.0"
  bind_port: 8000
log_timezone: "Asia/Shanghai"
storage:
  db_path: "data/logmcp.db"     # systemd 建议写 /var/lib/logmcp/logmcp.db
dashboard:
  write_enabled: true           # 关掉后网页只能看状态
```

**为什么 DB 而不是 YAML**：网页要支持增/改/删，整文件重写既会吃掉注释，也会让两个标签页互相覆盖；SQLite 提供行级校验（一条脏数据只跳过它自己，不会让整个网关 500）、事务、审计，而 `sqlite3` 是标准库、零新增依赖。开源仓库只需 gitignore 一个数据库文件。

**从旧版迁移**（BREAKING）：首次启动会自动把旧的 `config/log_targets.yaml` 与 `config/system_allowlist.yaml` 导入数据库，并重命名为 `.migrated` 保留备份。迁移是幂等的，重复启动不会覆盖你后来的修改。回滚方式：仪表盘「导出」→ 把内容存回 `config/log_targets.yaml` → 回退代码版本。

> 注意：`config/log_targets.yaml`、`.bak`、`.migrated` 也已 gitignore——它们含真实内网 IP 与 JumpServer 用户名，属于本机数据。开源仓库里只有 `.example.yaml`。


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
| 存储 | 与资产同一个 SQLite 库的 `allowlist_entries` 表（不再是独立 YAML） |
| `GET /dashboard/allowlist` | 列出当前条目、数据库路径与解析警告 |
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

## 网页维护资产

浏览器打开 `http://<gateway>:8765/dashboard?token=<local.token>`，「资产配置」面板可以：

- **新增 / 编辑资产**：填资产名、环境、`ssh_username`、模块与 `log_dir`；保存后**立即生效**，不用重启、不用手工改文件。
  新资产马上出现在上方状态表，可以直接点「连接」填密码/MFA。
- **以现有资产为模板**：把某个资产的用户名与全部模块一起填进表单，同一套模块的机器只改资产名和用户名。
- **停用 / 删除**：停用后从连接池消失但数据保留；删除会关闭其 SSH 连接。
- **批量导入 / 导出**：导入支持 YAML/JSON（先「试运行」看哪些条目被拒）；导出可下载合并后的完整配置，用于备份或迁移到别的机器。

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/dashboard/targets` | 资产清单 + 连接状态 + 数据库路径 + 配置告警 |
| `POST` | `/dashboard/targets` | 新增/整条覆盖（校验失败返回 400，原配置不动） |
| `DELETE` | `/dashboard/targets?name=` | 删除资产及其模块 |
| `POST` | `/dashboard/targets/enabled` | `{name, enabled}` 启用/停用 |
| `POST` | `/dashboard/targets/validate` | 干跑校验，不落库 |
| `POST` | `/dashboard/targets/import` | `{content, dry_run}` 批量导入 |
| `GET` | `/dashboard/targets/export?token=` | 导出完整 YAML |

写入前会统一校验：资产名仅限字母数字与 `. _ -`；`log_dir` 必须是绝对路径且不含 `..`；`ssh_username` 允许 `user#role#ip` 里的 `#`。写入使用临时文件 + 显式事务，失败时原数据库保持不变。

> 安全提示：`bind_host: 0.0.0.0` + token 走 URL query 时，拿到 token 等于能登记任意 `ssh_username`/`log_dir`。对外暴露前请把 `dashboard.write_enabled` 设为 `false`，或至少限制来源网段。密码/MFA **不会**入库，只存在于内存中的待认证凭据里，用完即清。

## 配套 Skill

`.claude/skills/log-query/SKILL.md` 提供项目级 skill，规范 Claude 的查询流程：连接状态前置检查 → 资产/模块选择（未指明必问、已指明不问）→ 时间与类型澄清 → 意图-工具映射 → 多资产分组汇总 → 只读边界。在本项目目录启动 Claude Code 即自动可用。

## DSH 插件（日志上下文）

`dsh-plugins/log-context/` 是同一套能力的 DSH 插件：右栏点选「环境 → 资产 → 模块 / K8S 命名空间、Pod → 动作模板」，
一键把「查哪里」写进对话输入框，你只需要补一句问题。详见 [dsh-plugins/log-context/README.md](dsh-plugins/log-context/README.md)。

它已发布到 npm：[`@zxlll/dsh-log-context`](https://www.npmjs.com/package/@zxlll/dsh-log-context)，**推荐直接用 `dsh` 命令安装**，不需要 clone 本仓库。

它由两部分组成，都不改动上面的 Python 业务代码：

1. **能力**：bundle patch 插入 `@deepseek-ai/dsh-mcp-client`，连本项目的 `mcp_server.py`，
   模型拿到 `mcp__logmcp__*` 工具；并追加一段系统提示，看到「【日志上下文】」块就直接按块内坐标查询。
2. **交互**：Host 半读 gateway `/targets`（本地 `config/log_targets.yaml` 仅作可选补充，缺失时自动降级）并提供 `/log-context/api`；Client 半注册右栏「日志上下文」标签页与输入框左侧入口按钮。

### 用 dsh 命令安装（推荐）

在**终端**里执行（不是在 DSH 会话里），`<profile>` 换成你自己的 profile 名（如 `web`）：

```bash
dsh plugin --profile web add @zxlll/dsh-log-context
```

一条命令做三件事：pnpm 把包装进 profile、把它写进依赖、把它追加到 `dsh.profile.bundles` 组合包列表。
装完重启 DSH（或让它热重载），右栏就会出现「日志上下文」标签页，输入框左侧也会出现入口按钮。

常用变体：

```bash
dsh plugin --profile web add @zxlll/dsh-log-context@0.1.1   # 锁定版本
dsh plugin --profile web remove @zxlll/dsh-log-context      # 卸载
dsh plugin --profile web list                               # 看装了哪些包
```

> `dsh plugin` 就是在 profile 目录里转发给 **pnpm**，所以本机需要装好 pnpm；报 `pnpm was not found` 先装 pnpm。
> 版本要求：包声明 `engines.dsh = ^0.2.2-rc.1`，DSH 版本不在范围内会被拒绝并要求显式豁免。
>
> ⚠️ **0.1.0 不要用**：那一版 patch 里插件行还写着 `@local/dsh-log-context`，按 npm 包装会报
> `Cannot find package '@local/dsh-log-context'`。请用 **0.1.1 及以上**（插件行改成相对路径 `./index.js`，
> 客户端模块 id 对齐真实包名）。

### 在 DSH 会话里安装

也可以让会话里的 agent 代劳（它走同一条包管理通道）：

```
plugin_manager(install_bundle, target="@zxlll/dsh-log-context")
```

### 从本地目录安装（改插件源码时用）

```
plugin_manager(install_bundle, target="D:\\pythonProject\\log-mcp-starter\\dsh-plugins\\log-context")
```

或 `dsh plugin --profile web add ./dsh-plugins/log-context`。两种安装形态都支持：
`cordis.patch.yml` 里的插件行用的是相对路径 `./index.js`，相对 patch 文件解析，不依赖包被装到哪个名字下。

### 装完要填的三项

**地址与 token 在 DSH 右栏「日志上下文」→「连接设置」里填**，保存在
`$DSH_HOME/log-context/settings.json`（默认 `~/.dsh/log-context/settings.json`），不进 git、也不用改文件。

| 字段 | 示例 |
|---|---|
| 网关地址 | `http://10.12.0.101:8765` |
| MCP 地址 | `http://10.12.0.101:8000/mcp` |
| 访问令牌 | 服务器上 `local.token` 的内容 |

填完点「试连」验证，再「保存」，MCP 行会自动重启一次生效。`cordis.patch.yml` 里的
`url` / `gatewayBaseUrl` 只是出厂默认值，设置文件缺失时才生效。

部署形态：`gateway_host.py` + `mcp_server.py` 跑在服务器 `10.12.0.101`（8000 = MCP，8765 = gateway），
Windows 本机只跑 DSH。

> ⚠️ token 是全局共享的一把，没有按用户/资产授权。对外给 token 前请把 `dashboard.write_enabled` 设为 `false`，
> 并确认网关只在内网/VPN 可达。


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

3. **Web 仪表盘**
   浏览器打开 `http://<服务器IP>:8765/dashboard?token=<local.token 内容>`，在表格中为每个资产填入 JumpServer 密码 + MFA 验证码，点击连接。连接成功后关闭浏览器即可——gateway 由 systemd 托管，SSH 由心跳任务维持。
   同一页面的「资产配置」面板可直接新增/编辑/删除资产（存入 `data/logmcp.db`，立即生效）。systemd 部署建议把 `storage.db_path` 指向 `/var/lib/logmcp/logmcp.db`，并在 unit 里加 `StateDirectory=logmcp` 保证目录可写。

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

- 只允许操作数据库中登记的资产/模块；模型不能传原始 SSH 命令，`execute_query` 的命令经白名单校验，路径限定在 `log_dir` 内。
- 资产与白名单的变化都会写入 `audit_log` 表（谁在什么时候加了哪台机器）；密码/MFA 不入库。
- 远程发现范围限制在 `log_dir` 下最多 5 层、最多 2000 个文件；查询最多使用 50 个文件。
- 压缩文件（.gz 等）会出现在发现列表中，`search_logs`/`read_log_tail` 不搜索压缩文件；用 `execute_query` 的 zgrep/zcat 查询。
- 日期识别覆盖常见文件名格式；非常规名称用 `discover_log_files` 让模型读取候选清单后以 `file_ids` 指定。
- 使用 GNU `find -printf`，目标系统应为常见 Linux/GNU find 环境。
- 心跳命令 `true` 每 25 分钟一次（可配置 1–60），会被 JumpServer 审计记录为正常操作。
- SSH 主机密钥验证保持开启；不要设置 `known_hosts=None`。本地 bridge 只监听 127.0.0.1 且需要 bearer token，切勿暴露到局域网或提交 token。
- 这是可供验证的开发工程，并非已完成生产安全审计。MFA 仍由用户正常完成；连接失效时按提示重新认证。
