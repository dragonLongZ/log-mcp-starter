# JumpServer Directory-discovery Log MCP

这个版本按“只配置目录、运行时发现文件”的思路设计。你只需要登记每个模块允许访问的日志目录，不必配置 `info.log`、`error.log` 或每天变化的归档文件名。

## 工作方式

1. `gateway_host.py` 在独立终端运行，手动输入 JumpServer 密码和 MFA，并保留 SSH 连接。
2. MCP 查询指定模块时，在服务端配置的 `log_dir` 下执行受限 `find`，获取文件相对路径、大小和修改时间。
3. 服务端识别文件名中的常见日期格式，包括 `2026-10-27`、`20261027`、`2026_10_27`、`2026.10.27`，并尝试根据名称识别 info/error 类型。
4. 今天会考虑无日期后缀的活动文件（如 `info.log`、`error.log`）；历史日期会优先选择文件名中带目标日期的文件。
5. 如果文件名格式不常见，Claude 可调用 `discover_log_files` 查看候选文件，再把返回的 `file_id` 传给 `search_logs` 或 `read_log_tail`。模型不能直接传任意远程路径；`file_id` 会在每次操作时重新对照目录发现结果进行验证。

## 配置

`config/log_targets.yaml` 中每个模块只需要：

```yaml
log_timezone: "Asia/Shanghai"

targets:
  server-172-26-73-80:
    modules:
      cheyichong:
        log_dir: "/data/logs/cheyichong"
      order-api:
        log_dir: "/data/apps/order-api/logs"
```

路径必须是目标 Linux 机器上的绝对路径，且必须位于该资产的授权访问范围内。`active_target` 应与 SSH 用户名实际选中的资产一致。本 starter 目前只支持一条 SSH 会话连接的一台资产。

## MCP 工具

- `list_log_targets()`：列出服务器、模块和允许的日志目录。
- `discover_log_files(server, module, day="yesterday", log_type="both")`：动态列出目录下发现的候选日志文件，返回不透明的 `file_id`、相对路径、大小、修改时间、日期推断和类型推断。
- `search_logs(server, module, keyword, day="yesterday", log_type="both", max_results=100, file_ids=None)`：按日期和类型自动选择候选文件并进行固定字符串搜索。若自动推断失败，先调用 `discover_log_files`，再传合适的 `file_ids`。
- `read_log_tail(server, module, day="yesterday", log_type="both", lines=200, file_ids=None)`：读取动态选择的文件末尾。

`day` 支持 `today`、`yesterday` 或 `YYYY-MM-DD`；`log_type` 支持 `info`、`error`、`both`。

## 安装与运行

```powershell
uv sync
uv run python create_token.py
```

在独立 PowerShell 窗口启动 SSH 会话桥接并完成 MFA：

```powershell
uv run python gateway_host.py
```

保持该窗口运行。在项目根目录 `.mcp.json` 中复制 `claude-mcp.example.json` 并改成当前工程绝对路径，再启动或重载 Claude Code。MCP 进程通过 stdio 与 Claude Code 通信；持久 SSH 会话和 MFA 提示位于独立进程，因此不占用 MCP 的 stdin/stdout。

## 安全边界及当前限制

- 只允许操作配置文件中登记的 server/module。模型不能传原始 SSH 命令或任意路径。
- 远程发现范围限制在 `log_dir` 下最多 5 层、最多 2000 个文件；查询最多使用 50 个文件。
- 只读查询用 `tail` 和固定字符串 `grep -F`，并限制输出字节数、行数、搜索结果和超时。
- gzip/zip 等压缩文件会出现在发现列表中，但当前 starter 不直接搜索压缩文件；需先扩展 zgrep/解压流支持。
- 日期识别覆盖常见文件名格式，不可能保证推断任意自定义格式。遇到非常规名称时，使用 `discover_log_files` 让模型读取候选清单，然后用 `file_ids` 指定真实发现的候选。
- 使用 GNU `find -printf`，目标系统应为常见 Linux/GNU find 环境。
- SSH 主机密钥验证保持开启；不要设置 `known_hosts=None`。本地 bridge 只监听 127.0.0.1 且需要 bearer token，切勿暴露到局域网或提交 token。
- 这是可供验证的开发起步工程，并非已完成生产安全审计。MFA 仍由用户正常完成；连接失效时须按正常流程重新认证。
