"""stdio MCP client for the local persistent JumpServer SSH bridge."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal
import httpx
import yaml
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "log_targets.yaml"
TOKEN_PATH = ROOT / "local.token"
LogType = Literal["info", "error", "both"]


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def bridge_settings() -> tuple[str, str]:
    if not TOKEN_PATH.exists():
        raise RuntimeError("local.token missing; run `uv run python create_token.py` first.")
    config = load_config()
    jump = config["jumpserver"]
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    base_url = f"http://{jump.get('bind_host', '127.0.0.1')}:{int(jump.get('bind_port', 8765))}"
    return base_url, token


async def bridge_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url, token = bridge_settings()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=httpx.Timeout(75.0, connect=2.0)) as client:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError as exc:
        raise RuntimeError("Local SSH bridge is not running. Start gateway_host.py in a separate terminal and complete MFA.") from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:
            detail = exc.response.text
        raise RuntimeError(f"Log gateway returned HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Timed out while waiting for the local SSH gateway.") from exc


def format_files(files: list[dict[str, Any]]) -> str:
    if not files:
        return "(none)"
    return "\n".join(
        f"- file_id={f['file_id']} | {f['relative_path']} | size={f['size_bytes']} bytes | "
        f"mtime={f['mtime_local']} | date_hint={f['date_hint'] or 'unknown'} | "
        f"kind={f['kind_hint']} | searchable={f['searchable']}"
        for f in files
    )


def format_search_result(result: dict[str, Any]) -> str:
    header = (
        f"Target: {result['server']} / {result['module']}\n"
        f"Directory: {result['log_dir']}\nDay: {result['day']} ({result['log_type']})\n"
        f"Timezone: {result['timezone']}\n"
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


mcp = FastMCP("JumpServer Directory-discovery Log MCP")


@mcp.tool()
async def list_log_targets() -> str:
    """List allowed servers, modules and their log directories. Config only needs directories, not filenames."""
    try:
        result = await bridge_request("GET", "/targets")
        lines = [f"Active target: {result.get('active_target')}", f"Description: {result.get('description', '')}", f"Log timezone: {result.get('log_timezone')}", "Configured modules:"]
        for module, cfg in result.get("modules", {}).items():
            lines.append(f"- {module}: log_dir={cfg.get('log_dir')}")
        lines.append("Supported day values: today, yesterday, or YYYY-MM-DD.")
        return "\n".join(lines)
    except Exception as exc:
        return f"Unable to list log targets: {exc}"


@mcp.tool()
async def discover_log_files(
    server: str,
    module: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    max_files: int = 100,
) -> str:
    """Discover log files under the module's configured directory at query time. No filenames or naming templates need configuration. Returns opaque file_ids; use those IDs when filenames are unusual or date inference is ambiguous."""
    if not 1 <= max_files <= 500:
        return "Invalid max_files: choose an integer from 1 to 500."
    try:
        result = await bridge_request("POST", "/discover-logs", {
            "server": server, "module": module, "day": day, "log_type": log_type, "max_files": max_files,
        })
        lines = [f"Target: {result['server']} / {result['module']}", f"Directory: {result['log_dir']}", f"Requested day: {result['day']} ({result['log_type']})", f"Timezone: {result['timezone']}", f"Directory listing truncated: {result['truncated_listing']}", "Automatically selected for this date:", format_files(result.get("auto_selected_files", [])), "Discovered candidates:", format_files(result.get("candidate_files", [])), result.get("note", "")]
        return "\n".join(lines)
    except Exception as exc:
        return f"Log discovery failed: {exc}"


@mcp.tool()
async def read_log_tail(
    server: str,
    module: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    lines: int = 200,
    file_ids: list[str] | None = None,
) -> str:
    """Read the last lines from dynamically discovered files under the configured module directory. day is today, yesterday, or YYYY-MM-DD. log_type is info, error, or both. Normally files are selected by filename date hints. If auto-selection cannot infer a custom filename, call discover_log_files and pass its file_ids here."""
    if not 1 <= lines <= 2000:
        return "Invalid lines value: choose an integer from 1 to 2000."
    try:
        result = await bridge_request("POST", "/read-tail", {
            "server": server, "module": module, "day": day, "log_type": log_type, "lines": lines, "file_ids": file_ids,
        })
        return format_search_result(result)
    except Exception as exc:
        return f"Log read failed: {exc}"


@mcp.tool()
async def search_logs(
    server: str,
    module: str,
    keyword: str,
    day: str = "yesterday",
    log_type: LogType = "both",
    max_results: int = 100,
    file_ids: list[str] | None = None,
) -> str:
    """Search a fixed string in dynamically discovered logs under the configured module directory. No exact filenames are configured. For today, undated active files such as info.log/error.log are considered; for historical days, common date tokens in filenames are matched. If the naming scheme is unusual, call discover_log_files first and pass its file_ids."""
    if not keyword or not keyword.strip() or len(keyword) > 256:
        return "Invalid keyword: provide 1–256 characters."
    if not 1 <= max_results <= 200:
        return "Invalid max_results: choose an integer from 1 to 200."
    try:
        result = await bridge_request("POST", "/search-logs", {
            "server": server, "module": module, "keyword": keyword,
            "day": day, "log_type": log_type, "max_results": max_results, "file_ids": file_ids,
        })
        return format_search_result(result)
    except Exception as exc:
        return f"Log search failed: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
