"""stdio MCP 客户端：对接本地持久化 JumpServer SSH 桥接服务（多资产版）。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, Literal
import httpx
import yaml
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "log_targets.yaml"
TOKEN_PATH = ROOT / "local.token"
LogType = Literal["info", "error", "both"]
MAX_SERVERS_PER_QUERY = 10


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def bridge_settings() -> tuple[str, str]:
    """读取本地 bridge 的地址与 bearer token。"""
    if not TOKEN_PATH.exists():
        raise RuntimeError("local.token missing; run `uv run python create_token.py` first.")
    config = load_config()
    jump = config["jumpserver"]
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    base_url = f"http://{jump.get('bind_host', '127.0.0.1')}:{int(jump.get('bind_port', 8765))}"
    return base_url, token


async def bridge_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """向本地 bridge 发起带鉴权的 HTTP 请求，并把常见故障转成可读错误。"""
    base_url, token = bridge_settings()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=httpx.Timeout(75.0, connect=2.0)) as client:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError as exc:
        raise RuntimeError("Local SSH bridge is not running. Start gateway_host.py in a separate terminal, then authenticate assets with reconnect_asset.") from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:
            detail = exc.response.text
        raise RuntimeError(f"Log gateway returned HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Timed out while waiting for the local SSH gateway.") from exc


def check_servers(servers: list[str]) -> str | None:
    """校验 servers 参数合法性；返回错误提示或 None（合法）。"""
    if not servers or not isinstance(servers, list):
        return "Invalid servers: provide a non-empty list of asset names from list_log_targets."
    if len(servers) > MAX_SERVERS_PER_QUERY:
        return f"Invalid servers: at most {MAX_SERVERS_PER_QUERY} assets per query."
    return None


def format_files(files: list[dict[str, Any]]) -> str:
    """把文件候选列表格式化为可读文本行。"""
    if not files:
        return "(none)"
    return "\n".join(
        f"- file_id={f['file_id']} | {f['relative_path']} | size={f['size_bytes']} bytes | "
        f"mtime={f['mtime_local']} | date_hint={f['date_hint'] or 'unknown'} | "
        f"kind={f['kind_hint']} | searchable={f['searchable']}"
        for f in files
    )


def format_log_payload(result: dict[str, Any]) -> str:
    """格式化 search/tail 的单资产结果。"""
    header = (
        f"Module: {result['module']}\nDirectory: {result['log_dir']}\n"
        f"Day: {result['day']} ({result['log_type']})\nTimezone: {result['timezone']}\n"
    )
    if not result.get("found"):
        return header + result.get("message", "No files automatically matched.") + "\nCandidates:\n" + format_files(result.get("candidate_files", []))
    files = result.get("files_searched", result.get("files_read", []))
    body = result.get("matches_text", result.get("text", ""))
    return (
        header + "Files selected:\n" + format_files(files) + "\n"
        f"Directory listing truncated: {result.get('truncated_listing')}\n"
        f"Output may be byte-truncated: {result.get('possibly_truncated_by_bytes')}\n"
        + ("--- RESULTS ---\n" + (body or "(no matches found)"))
    )


def format_discover_payload(result: dict[str, Any]) -> str:
    """格式化 discover 的单资产结果。"""
    return "\n".join([
        f"Module: {result['module']}",
        f"Directory: {result['log_dir']}",
        f"Requested day: {result['day']} ({result['log_type']})",
        f"Timezone: {result['timezone']}",
        f"Directory listing truncated: {result['truncated_listing']}",
        "Automatically selected for this date:",
        format_files(result.get("auto_selected_files", [])),
        "Discovered candidates:",
        format_files(result.get("candidate_files", [])),
        result.get("note", ""),
    ])


def format_query_payload(result: dict[str, Any]) -> str:
    """格式化 execute_query 的单资产结果（含命令、退出码、截断标志）。"""
    lines = [
        f"Module: {result['module']} | Directory: {result['log_dir']}",
        f"Command: {result['executed_command']}",
        f"Exit code: {result['exit_code']} (for grep, exit 1 means no match, not an error)",
        f"Output byte-truncated: {result['possibly_truncated_by_bytes']}",
    ]
    if result.get("stderr"):
        lines.append(f"Stderr: {result['stderr']}")
    lines.append("--- OUTPUT ---")
    lines.append(result.get("stdout") or "(no output)")
    return "\n".join(lines)


def format_grouped(results: list[dict[str, Any]], formatter: Callable[[dict[str, Any]], str]) -> str:
    """把多资产结果按资产分组拼接；失败资产单独呈现 error 条目。"""
    blocks: list[str] = []
    for entry in results:
        server = entry.get("server", "?")
        if entry.get("error"):
            blocks.append(f"=== {server} ===\nERROR: {entry['error']}")
        else:
            blocks.append(f"=== {server} ===\n{formatter(entry)}")
    return "\n\n".join(blocks)


mcp = FastMCP("JumpServer Multi-asset Log MCP")


@mcp.tool()
async def list_log_targets() -> str:
    """列出全部已配置资产（服务器）、其模块（含业务描述）、允许的日志目录及当前 SSH 连接状态。查询前先调用本工具了解有哪些资产/模块、哪些已连接；模块的业务描述（description）是判断用户话语对应哪个模块的主要依据。"""
    try:
        result = await bridge_request("GET", "/targets")
        lines = [f"Log timezone: {result.get('log_timezone')}", "Configured assets:"]
        for name, target in result.get("targets", {}).items():
            connection = target.get("connection") or {}
            lines.append(f"- {name}: state={connection.get('state', 'unknown')} | {target.get('description', '')}")
            for module, cfg in target.get("modules", {}).items():
                # 模块 description 是模型判断业务归属、匹配用户话语的主要依据。
                module_desc = cfg.get("description") or ""
                suffix = f" | {module_desc}" if module_desc else ""
                lines.append(f"    - {module}: log_dir={cfg.get('log_dir')}{suffix}")
        lines.append("Supported day values: today, yesterday, or YYYY-MM-DD.")
        lines.append("Assets with state != connected must be authenticated via reconnect_asset first.")
        return "\n".join(lines)
    except Exception as exc:
        return f"Unable to list log targets: {exc}"


@mcp.tool()
async def list_connections() -> str:
    """查看所有已配置资产的 SSH 连接状态：disconnected（未连接）、connecting（认证中）、connected（已连接）、needs_reauth（需重新认证），含上次心跳时间与断开原因。"""
    try:
        result = await bridge_request("GET", "/connections")
        lines = ["Asset connections:"]
        for item in result.get("connections", []):
            lines.append(
                f"- {item['server']}: state={item['state']} | last_heartbeat={item.get('last_heartbeat') or 'never'}"
                + (f" | reason={item['disconnect_reason']}" if item.get("disconnect_reason") else "")
            )
        lines.append("Use reconnect_asset(server) to (re-)authenticate an asset; MFA is completed in the gateway terminal.")
        return "\n".join(lines)
    except Exception as exc:
        return f"Unable to list connections: {exc}"


@mcp.tool()
async def reconnect_asset(server: str) -> str:
    """触发单个资产的 SSH（重新）认证。密码/MFA 提示出现在 gateway_host.py 终端，必须由用户在该终端完成。本工具立即返回 state=connecting；待用户确认完成后轮询 list_connections 或重试原查询。state 为 connecting 期间不要重复调用。"""
    try:
        result = await bridge_request("POST", "/connect", {"server": server})
        return (
            f"Asset: {result.get('server')}\nState: {result.get('state')}\nNote: {result.get('note', '')}\n"
            "Tell the user to complete password/MFA in the gateway terminal, then retry the original query."
        )
    except Exception as exc:
        return f"Reconnect failed: {exc}"


@mcp.tool()
async def discover_log_files(
    servers: list[str],
    module: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    max_files: int = 100,
) -> str:
    """在查询时动态发现各资产模块目录下的日志文件，无需配置任何文件名。按资产返回不透明 file_id；当文件名不规则或日期推断不明确时，把 file_id 传给 search_logs/read_log_tail 精确查询。servers 接受 1-10 个资产名（来自 list_log_targets）。"""
    invalid = check_servers(servers)
    if invalid:
        return invalid
    if not 1 <= max_files <= 500:
        return "Invalid max_files: choose an integer from 1 to 500."
    try:
        result = await bridge_request("POST", "/discover-logs", {
            "servers": servers, "module": module, "day": day, "log_type": log_type, "max_files": max_files,
        })
        return format_grouped(result.get("results", []), format_discover_payload)
    except Exception as exc:
        return f"Log discovery failed: {exc}"


@mcp.tool()
async def read_log_tail(
    servers: list[str],
    module: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    lines: int = 200,
    file_ids: list[str] | None = None,
) -> str:
    """读取一个或多个资产上动态发现文件的末尾若干行。day 取 today、yesterday 或 YYYY-MM-DD；log_type 取 info、error 或 both。文件通常按文件名日期提示自动选择；自动选择失败时先调用 discover_log_files，再把其 file_ids 传进来。结果按资产分组。"""
    invalid = check_servers(servers)
    if invalid:
        return invalid
    if not 1 <= lines <= 2000:
        return "Invalid lines value: choose an integer from 1 to 2000."
    try:
        result = await bridge_request("POST", "/read-tail", {
            "servers": servers, "module": module, "day": day, "log_type": log_type, "lines": lines, "file_ids": file_ids,
        })
        return format_grouped(result.get("results", []), format_log_payload)
    except Exception as exc:
        return f"Log read failed: {exc}"


@mcp.tool()
async def search_logs(
    servers: list[str],
    module: str,
    keyword: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    max_results: int = 100,
    file_ids: list[str] | None = None,
) -> str:
    """在一个或多个资产的动态发现日志中搜索固定字符串（grep -F）。查当天时会考虑无日期后缀的活动文件（如 info.log/error.log）；查历史日期时匹配文件名中的常见日期标记。命名不规则时先调用 discover_log_files 并传其 file_ids。正则/压缩文件/统计需求请改用 execute_query。结果按资产分组。"""
    invalid = check_servers(servers)
    if invalid:
        return invalid
    if not keyword or not keyword.strip() or len(keyword) > 256:
        return "Invalid keyword: provide 1-256 characters."
    if not 1 <= max_results <= 200:
        return "Invalid max_results: choose an integer from 1 to 200."
    try:
        result = await bridge_request("POST", "/search-logs", {
            "servers": servers, "module": module, "keyword": keyword,
            "day": day, "log_type": log_type, "max_results": max_results, "file_ids": file_ids,
        })
        return format_grouped(result.get("results", []), format_log_payload)
    except Exception as exc:
        return f"Log search failed: {exc}"


@mcp.tool()
async def execute_query(
    servers: list[str],
    module: str,
    command: str,
) -> str:
    """在一个或多个资产的模块日志目录下执行只读 shell 管道命令。只读：任何写入/修改/删除尝试都会在执行前被拒绝。

gateway 强制的规则：
- 仅允许白名单命令：grep、egrep、fgrep、zgrep、head、tail、cat、zcat、wc、sort、uniq、awk、sed、cut、tr、tac、ls、find、du、file。允许管道 '|'（最多 5 段）。
- 任何位置禁止出现：; && || 反引号 $( > < 以及换行。因此 awk 中含 '>' 的比较也不可用。
- 路径：相对路径在模块 log_dir 内解析（推荐）；绝对路径必须位于 log_dir 内；拒绝 '..'。含 '/' 的搜索词必须写成 grep -e 'PATTERN'。
- sed 禁止 -i 与 w/W/e 命令；awk 禁止 system()/getline；find 禁止 -exec/-delete；sort 禁止 -o；uniq 最多一个文件参数。
- 输出 100KB 截断；超时 60 秒。grep 退出码 1 表示"无匹配"，不是错误。

示例：grep -E 'ERROR.*timeout' info.log | tail -n 50 ；zgrep 'OutOfMemory' info.log.2026-10-27.gz ；tail -n 200 error.log | grep -e '/api/v1' ；cat info.log | grep ERROR | sort | uniq -c | head -20"""
    invalid = check_servers(servers)
    if invalid:
        return invalid
    if not command or not command.strip() or len(command) > 2000:
        return "Invalid command: provide 1-2000 characters."
    try:
        result = await bridge_request("POST", "/execute-query", {
            "servers": servers, "module": module, "command": command,
        })
        return format_grouped(result.get("results", []), format_query_payload)
    except Exception as exc:
        return f"Query execution failed: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
