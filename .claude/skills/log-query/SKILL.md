---
name: log-query
description: 通过 JumpServer 日志 MCP 查询远程服务器日志（多资产）。当用户提到"查日志"、"看报错"、"grep 日志"、"日志排查"、"服务报了什么错"、"昨天的日志"、"查一下 XX 模块的日志"，或需要在远程资产上检索 info/error 日志、追踪订单号/请求号/异常堆栈时使用本技能。
---

# 日志查询规范（log-query）

本技能规范如何使用 JumpServer 日志 MCP（工具前缀 `log-mcp`，服务器名以实际注册为准）查询远程日志。

**核心原则（优先级最高，与下文任何步骤冲突时以本节为准）：**

1. **先选定，再动作**：任何 `reconnect_asset`、任何查询之前，必须先确定"用户要操作哪些资产"。用户没明确说，就必须用 AskUserQuestion 问，**禁止默认全选**。
2. **最小操作范围**：只对用户选定的资产发起认证；查询的 `servers` 参数**只能**包含用户选定（明确指定或勾选确认）的资产。已连接的其他资产与本次查询无关。
3. **可用 ≠ 选定**：资产的连接状态（connected/disconnected）**永远不能**作为选择依据、默认值或过滤理由。选定只看用户意图（明确指定或勾选确认）。**即使整个 gateway 只有一台资产是 connected，用户没指明资产时也必须询问**——connected 只说明 SSH 通道已建立，不代表它就是用户要查的目标环境。相应地，在用户完成选定之前，禁止按连接状态预过滤候选资产清单；候选匹配只按业务归属（模块 description、资产 description、用户话语）判断。
4. **用户未完成选择时不得推进**：用户拒绝了选择题、未作答、或回复"继续/接着来"等**不含资产名**的指令时，一律视为"选定未完成"——必须重新给出选择题让用户勾选，或停下来等待；**禁止**将其解读为"用已连接/上次的资产继续"。用户明确表示不选择时，终止查询并说明"无法确定目标资产"。
5. **只读边界不可逾越**：任何写入/修改/删除远程文件的请求一律拒绝。

## 标准流程

```
1. 确定资产与模块（未指明必问，禁止默认全选）
2. 连接检查与认证（仅限选定资产）
3. 澄清时间范围与日志类型
4. 按意图选择工具执行查询
5. 多资产结果分组汇总
```

### 第 1 步：确定资产与模块（最先执行）

1. 调用 `list_log_targets` 获取全部资产、模块清单（每个模块带业务描述 description）。
2. **模块匹配依据**：用户话语中的业务词（如"云车场"、"会员"、"优惠券"）与各模块的 description 对照匹配，而不是只看模块英文名；description 不匹配任何模块且无法从英文名推断时，列出模块清单让用户选择。
3. **选择规则（硬性）**：
   - 用户已明确指定资产（如"查 server-172-26-73-80-2.0-test"）→ 直接使用，不再询问。
   - 用户只给了模块/业务线索（如"查云车场的日志"）：
     - 恰好**一个**资产含匹配模块 → 使用该资产，并在回复中告知"已选定 XX 资产"。**"恰好一个"以业务归属判断（模块/资产 description 对照用户话语），与连接状态无关**——禁止先按 connected/disconnected 过滤候选再套用本条。
     - **多个**资产都含匹配模块 → **必须用 AskUserQuestion 让用户勾选资产**（multiSelect，选项列出资产名+description），拿到结果前禁止认证、禁止查询。**即使"全都查"看起来更省事、即使其中只有一台已连接，也不允许默认全选或默认选中已连接的那台。**
   - 用户什么都没指明 → AskUserQuestion 让用户选择资产与模块（均可多选）。
4. 把用户选择结果记为"选定资产集合"，后续所有步骤只使用这个集合。

### 第 2 步：连接检查与认证（仅针对选定资产）

- 对**选定资产集合中的每一台**检查状态（来自第 1 步 list_log_targets 的连接状态，或调用 `list_connections`）：
  - `connected` → 就绪。
  - `disconnected` / `needs_reauth` → 对**该选定资产**调用 `reconnect_asset(server)`，然后**告知用户到 gateway_host.py 终端完成密码/MFA**，并进入下方轮询。
  - `connecting` → 提示用户去 gateway 终端完成认证，**不要重复调用**，直接进入下方轮询。
- **认证轮询（代替干等用户通知）**：告知用户后，每隔约 5 秒调用一次 `list_connections` 查询**选定资产**状态，最多轮询约 2 分钟（约 24 次）。
  - 期间变为 `connected` → 立即停止轮询，继续执行原查询，无需等用户回复。
  - 时间到仍未 `connected` → 停止轮询，回复用户"认证尚未完成；完成后告诉我，我再继续查询"，**不要无限轮询**，也不要反复调用 `reconnect_asset`。
  - 轮询只覆盖选定资产；其他资产的状态变化与本次任务无关，不查询、不提示。
- **禁止**对选定集合之外的资产调用 `reconnect_asset`，即使它们也处于断连状态。
- 若任何工具返回 "Local SSH bridge is not running"：提示用户在独立终端运行 `uv run python gateway_host.py`，**不要盲目重试**。
- 同一错误（断连/认证）**最多引导一次**，用户未确认完成前不重复发起相同查询。

### 第 3 步：澄清时间范围与日志类型

- `day`：用户说"今天/昨天/具体日期"直接映射 `today` / `yesterday` / `YYYY-MM-DD`；未说明时**默认 today**（排查现场问题多为当天），并在回复中说明所用日期。
- `log_type`：用户找报错/异常 → `error`；看业务流程/请求记录 → `info`；不确定或要全面排查 → `both`。
- 查询结果过多时，建议用户缩小时间范围或补充关键词，而不是反复放大 max_results。

### 第 4 步：意图 → 工具映射

查询工具的 `servers` 参数**只传选定资产集合**（1 台或多台），不传其他资产。

| 用户意图 | 工具 | 说明 |
|---|---|---|
| 看最新日志/最近发生了什么 | `read_log_tail` | 默认 200 行，可调 lines |
| 按关键词/订单号/车牌号/手机号检索（固定字符串） | `search_logs` | grep -F，最常用 |
| 正则匹配（如 `ERROR.*timeout`） | `execute_query` | `grep -E 'PATTERN' file` |
| 查压缩/归档日志（.gz） | `execute_query` | `zgrep 'KEYWORD' xxx.log.gz` |
| 统计计数（错误出现次数、按字段聚合） | `execute_query` | `grep -c`、`... \| sort \| uniq -c` |
| 看某时间段上下文（匹配行前后 N 行） | `execute_query` | `grep -A 20 -B 5 'KEYWORD' file` |
| 文件名/日期不确定、自动选择失败 | `discover_log_files` | 先列候选，再用返回的 `file_ids` 传给 search/read |
| 多命令组合排查 | `execute_query` | 管道最多 5 段 |
| 看系统资源/磁盘/进程 | `execute_system_query` | `df -h`、`free -m`、`ps aux`、`top -bn1` |
| 查 Docker 容器/日志 | `execute_system_query` | `docker ps`、`docker logs xxx`，可接 `\| head -n 20` |
| 查 K8s 资源/日志 | `execute_system_query` | `kubectl get pods`、`kubectl logs xxx` |

**execute_query 使用要点**（避免被校验器拒绝）：

- 命令在模块 `log_dir` 下执行，**优先用相对路径**（如 `info.log`、`archive/x.log`）。
- 含 `/` 的搜索词必须写成 `grep -e '/api/v1'`，否则会被当作路径校验而拒绝。
- 禁止命令串联 `;` `&&` `||` 与换行；禁止反引号、`$(`、以及路径里的 `$变量`/`~`（shell 展开会绕过路径检查）。
- 禁止输出重定向 `>` `>>`；但 awk 内部的 `>` 比较是允许的（如 `awk '$3 > 10'`）。
- sed 不能 `-i`；awk 不能 `system()`/`getline`/`print > file`；find 不能 `-delete`，`-exec` 允许但嵌套命令必须在白名单内；sort 不能 `-o`。
- grep 退出码 1 = 无匹配，不是错误，如实向用户报告"未找到"。
- 输出上限 100KB、超时 60s；结果被截断时告知用户缩小范围。

**execute_system_query 使用要点**：

- 不需要传 `module`；`servers` 仍只传选定资产集合。
- 文本处理命令（grep/head/tail/awk 等）只能接在管道后面，不能单独作为第一条命令读文件；读日志文件请用 `execute_query`。
- `top` 必须加 `-b`（如 `top -bn1`）；`journalctl` 不能加 `-f`。
- 白名单/黑名单/未知三段：返回 UNKNOWN 时不要自行改写重试，先告知用户该命令未知、需要人工确认。
- 典型：`docker ps | head -20`、`df -h`、`kubectl get pods -A`、`systemctl status nginx`。

### 第 5 步：多资产结果汇总格式

多资产查询结果**按资产分组**呈现：

```
## 查询结果：<关键词/意图> （<日期>，<日志类型>）

### 资产 A（server-xxx）
- 模块/文件：<module> / <files>
- 匹配数：N 条
- 关键发现：<摘要或代表性日志行>

### 资产 B（server-yyy）
- ...

### 失败资产（如有）
- server-zzz：<失败原因 + 引导操作>

### 跨资产结论
<相同错误模式归并说明、时间线关联、初步定位>
```

- 单资产查询可简化格式，但保留"文件、匹配数、结论"三要素。
- 有资产失败时**先呈现成功结果**，再列失败原因与操作引导，不因部分失败放弃汇总。

## 只读边界（强制）

- 本 MCP **仅支持只读查询**。用户要求写入、修改、删除、清空、轮转远程日志文件时，**一律拒绝**，并说明："日志 MCP 出于安全设计仅支持只读查询，写操作请直接登录服务器处理。"
- 不要尝试通过 execute_query 构造绕过校验的写命令；校验器拒绝后如实告知用户被拒原因，不反复变形重试（同一意图被拒 2 次后停止，向用户解释边界）。

## 常见错误处理速查

| 错误信息 | 处理 |
|---|---|
| `Local SSH bridge is not running` | 引导用户启动 gateway_host.py |
| `Asset 'x' is not connected` / `needs_reauth` | 仅当 x 属于选定资产时：reconnect_asset + 引导用户到 gateway 终端 MFA，然后每 ~5s 轮询 list_connections，最多 ~2 分钟 |
| `Authentication in progress` | 引导用户完成 gateway 终端认证，进入同样的 5s/2min 有界轮询；不重复调用 reconnect_asset |
| `Unknown module 'x' for server 'y'` | 重新 list_log_targets 核对模块名，或让用户确认 |
| `None of the given file_ids belong to server` | 重新 discover_log_files 获取该资产的 file_ids |
| `... is not in the read-only whitelist` | 换用白名单命令重写查询，或改用 search_logs |
| `Path ... is outside the authorized log directory` | 改用相对路径；含 `/` 的搜索词改用 `grep -e` |
| `UNKNOWN: ...`（系统工具） | 命令不在已知安全/危险名单：告知用户需人工确认，不要改写后反复尝试 |
| `REJECTED: ...`（系统工具） | 命中危险命令或安全限制：如实说明被拒原因，不绕过 |
