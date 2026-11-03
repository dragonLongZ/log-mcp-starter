"""JumpServer SSH 会话与目录发现式日志桥接服务（多资产版）。

每个配置的资产（target）携带自己的 ssh_username，按需建立独立 SSH 连接。
认证可在 Web 仪表盘（/dashboard）中完成，也可回退到终端交互。
后台心跳任务维持已建立会话的活性，防止 JumpServer 空闲超时断连。
自由格式的只读查询在下发执行前，由 command_guard 进行静态校验。
"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import secrets
import shlex
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncssh
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from command_guard import CommandValidationError, validate_query_command
from system_guard import Verdict, ValidationResult, validate_system_info_command

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "log_targets.yaml"
TOKEN_PATH = ROOT / "local.token"
MAX_READ_BYTES = 100_000
MAX_LINES = 2_000
MAX_MATCHES = 200
MAX_DISCOVERY_FILES = 2_000
MAX_FILES_PER_QUERY = 50
MAX_DEPTH = 5
MAX_SERVERS_PER_QUERY = 10
DEFAULT_KEEPALIVE_MINUTES = 25
QUERY_TIMEOUT_SECONDS = 60
CONNECTION_TEST_TIMEOUT_SECONDS = 5

PENDING_CREDENTIALS: dict[str, dict[str, str]] = {}
"""Web 仪表盘提交的临时凭据：{server: {password, mfa_code}}。
ConsoleMFA 在认证时优先使用；建成后立即清除。"""

LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gateway 登录</title><style>body{font-family:system-ui,sans-serif;max-width:400px;margin:4rem auto;padding:0 1rem;background:#f5f5f5}h1{text-align:center}input{width:100%;padding:10px;margin:8px 0;border:1px solid #ddd;border-radius:6px;font-size:15px;box-sizing:border-box}button{width:100%;padding:10px;background:#3b82f6;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer}button:hover{background:#2563eb}</style></head><body><h1>Log MCP Gateway</h1><p>请输入 API Token（即 local.token 内容）：</p><form id="f"><input type="password" id="tk" placeholder="Bearer Token" autofocus><button type="submit">进入仪表盘</button></form><script>document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();window.location.href='/dashboard?token='+encodeURIComponent(document.getElementById('tk').value)})</script></body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gateway Dashboard</title><style>body{font-family:system-ui,sans-serif;max-width:960px;margin:1.5rem auto;padding:0 1rem;background:#f5f5f5}table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #eee;font-size:14px}th{background:#f8f9fa;font-weight:600}.connected{color:#22c55e}.disconnected{color:#ef4444}.connecting{color:#f59e0b}.needs_reauth{color:#f97316}button{padding:5px 12px;border:none;border-radius:4px;cursor:pointer;font-size:13px}.btn-connect{background:#3b82f6;color:#fff}.btn-connect:hover{background:#2563eb}.btn-cancel{background:#e5e7eb;color:#333}.btn-cancel:hover{background:#d1d5db}.form-row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}input{padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:14px}.hidden{display:none}#err{background:#fef2f2;color:#dc2626;padding:10px;border-radius:6px;margin-bottom:1rem}.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}h1{margin:0;font-size:20px}.note{color:#888;font-size:13px}</style></head><body><div class="header"><h1>Gateway 仪表盘</h1><span class="note">每 10s 自动刷新</span></div><div id="err" class="hidden"></div><table><thead><tr><th>资产名</th><th>状态</th><th>最后心跳</th><th>断开原因</th><th>操作</th></tr></thead><tbody id="tb"></tbody></table><script>var T=new URLSearchParams(window.location.search).get('token');var S='/dashboard/status',U='/dashboard/connect';function L(s){var m={connected:'\\u2705 已连接',disconnected:'\\u274e 断连',connecting:'\\u23f3 认证中',needs_reauth:'\\u26a0\\ufe0f 需重认证'};return m[s]||s}async function R(){try{var r=await fetch(S,{headers:{Authorization:'Bearer '+T}});if(!r.ok)throw new Error(r.status+' '+((await r.json()).detail||''));render((await r.json()).connections)}catch(e){E(e.message)}}function render(cs){document.getElementById('tb').innerHTML=cs.map(function(c){return'<tr><td>'+c.server+'</td><td class="'+c.state+'">'+L(c.state)+'</td><td>'+(c.last_heartbeat||'\\u2014')+'</td><td>'+(c.disconnect_reason||'\\u2014')+'</td><td>'+(c.state!=='connecting'?'<button class="btn-connect" onclick="F(\\''+c.server+'\\')">\\u8fde\\u63a5</button>':'\\u8ba4\\u8bc1\\u4e2d...')+'</td></tr><tr id="fm-'+c.server+'" class="hidden"><td colspan="5"><div class="form-row"><input type="password" id="pw-'+c.server+'" placeholder="JumpServer \\u5bc6\\u7801" style="width:180px"><input type="text" id="mf-'+c.server+'" placeholder="MFA \\u9a8c\\u8bc1\\u7801" style="width:120px"><button class="btn-connect" onclick="C(\\''+c.server+'\\')">\\u786e\\u8ba4\\u8fde\\u63a5</button><button class="btn-cancel" onclick="H(\\''+c.server+'\\')">\\u53d6\\u6d88</button></div></td></tr>'}).join('')}function F(s){document.querySelectorAll('[id^=fm-]').forEach(function(e){e.classList.add('hidden')});document.getElementById('fm-'+s).classList.remove('hidden')}function H(s){document.getElementById('fm-'+s).classList.add('hidden')}async function C(s){var p=document.getElementById('pw-'+s).value,m=document.getElementById('mf-'+s).value;if(!p){E('\\u8bf7\\u8f93\\u5165\\u5bc6\\u7801');return}try{var r=await fetch(U,{method:'POST',headers:{Authorization:'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify({server:s,password:p,mfa_code:m})});var d=await r.json();if(!r.ok)throw new Error(d.detail||'\\u8fde\\u63a5\\u5931\\u8d25');H(s);await R()}catch(e){E(e.message)}}function E(m){var e=document.getElementById('err');e.textContent=m;e.classList.remove('hidden');setTimeout(function(){e.classList.add('hidden')},6000)}R();setInterval(R,10000)</script></body></html>"""

AssetState = Literal["disconnected", "connecting", "connected", "needs_reauth"]

# 常见文本日志后缀，以及无常规后缀但可识别的日志名标记。
TEXT_SUFFIXES = {".log", ".out", ".txt", ".json", ".jsonl", ".trace", ".csv"}
LOG_NAME_MARKERS = (
    "log", "info", "error", "err", "warn", "debug", "trace", "fatal",
    "exception", "access", "application", "app", "stdout", "stderr",
    "catalina", "nginx", "gateway", "server",
)
COMPRESSED_SUFFIXES = (".gz", ".zip", ".bz2", ".xz", ".7z")
ERROR_MARKERS = ("error", "err", "exception", "fatal", "stderr", "panic", "crash")
INFO_MARKERS = ("info", "stdout", "access")


def load_config() -> dict[str, Any]:
    """加载并校验配置：每个资产必须有 ssh_username，心跳间隔须在 1-60 分钟。"""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict) or "jumpserver" not in config or "targets" not in config:
        raise RuntimeError(f"Invalid configuration: {CONFIG_PATH}")
    for name, target in (config["targets"] or {}).items():
        if not str((target or {}).get("ssh_username", "")).strip():
            raise RuntimeError(
                f"Target '{name}' is missing 'ssh_username'. Migration: move the old "
                "jumpserver.username into each target as ssh_username."
            )
    try:
        interval = int(config["jumpserver"].get("keepalive_interval_minutes", DEFAULT_KEEPALIVE_MINUTES))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("jumpserver.keepalive_interval_minutes must be an integer between 1 and 60.") from exc
    if not 1 <= interval <= 60:
        raise RuntimeError("jumpserver.keepalive_interval_minutes must be between 1 and 60.")
    return config


def read_token() -> str:
    """读取本地 bearer token（由 create_token.py 生成，不入库）。"""
    if not TOKEN_PATH.exists():
        raise RuntimeError("local.token is missing. Run `uv run python create_token.py` first.")
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("local.token is too short; regenerate it with create_token.py.")
    return token


class ConsoleMFA(asyncssh.SSHClient):
    """认证提示器：优先使用 Web 仪表盘提交的凭据，其次回退到终端交互。"""

    def __init__(self, server: str) -> None:
        self._server = server
        self._password: str | None = None

    def set_password(self, password: str) -> None:
        """复用 connect() 里已经设置的密码，避免事件循环里再阻塞一次。"""
        self._password = password

    def password_auth_requested(self) -> str:
        creds = PENDING_CREDENTIALS.get(self._server)
        if creds and creds.get("password"):
            return creds["password"]
        if self._password is None:
            self._password = getpass.getpass(f"[{self._server}] JumpServer password: ")
        return self._password

    def kbdint_auth_requested(self) -> str:
        return ""

    def kbdint_challenge_received(
        self, name: str, instructions: str, lang: str,
        prompts: list[tuple[str, bool]],
    ) -> list[str]:
        if instructions:
            print(instructions)
        creds = PENDING_CREDENTIALS.get(self._server)
        answers: list[str] = []
        for prompt, echo in prompts:
            lowered = prompt.lower()
            if "password" in lowered:
                answers.append(self.password_auth_requested())
            elif creds and creds.get("mfa_code"):
                # Web 仪表盘提交了 MFA 验证码，优先使用。
                answers.append(creds["mfa_code"])
            else:
                # 回退：终端交互输入。
                labeled = f"[{self._server}] {prompt}"
                answers.append(input(labeled) if echo else getpass.getpass(labeled))
        return answers


class AssetConnection:
    """单个资产的 SSH 连接：独立状态机、独立锁、独立 MFA 提示器。"""

    def __init__(self, server: str, ssh_username: str, host: str, port: int) -> None:
        self.server = server
        self.ssh_username = ssh_username
        self.host = host
        self.port = port
        self.state: AssetState = "disconnected"
        self.conn: asyncssh.SSHClientConnection | None = None
        self.lock = asyncio.Lock()
        self.last_heartbeat: str | None = None
        self.disconnect_reason: str | None = None
        self._mfa = ConsoleMFA(server)

    def mark_disconnected(self, reason: str) -> None:
        """标记连接失效，记录原因与时间，状态转为 needs_reauth。"""
        self.conn = None
        self.state = "needs_reauth"
        stamped = datetime.now().isoformat(timespec="seconds")
        self.disconnect_reason = f"{reason} (at {stamped})"[:300]

    def is_live(self) -> bool:
        return self.state == "connected" and self.conn is not None and not self.conn.is_closed()

    def status_payload(self) -> dict[str, Any]:
        if self.state == "connected" and not self.is_live():
            self.mark_disconnected("SSH connection closed")
        return {
            "server": self.server,
            "state": self.state,
            "last_heartbeat": self.last_heartbeat,
            "disconnect_reason": self.disconnect_reason,
        }

    async def connect(self) -> None:
        """在后台任务中执行；优先使用 Web 凭据（免终端），否则回退到终端提示。"""
        try:
            creds = PENDING_CREDENTIALS.get(self.server)
            if creds and creds.get("password"):
                print(f"[{self.server}] Connecting via web-submitted credentials.")
                self._mfa.set_password(creds["password"])
                password = creds["password"]
            else:
                print(f"[{self.server}] Connecting to JumpServer; complete password/MFA in this terminal.")
                # 密码在独立线程读取，避免 getpass 阻塞事件循环（轮询/心跳还能响应）。
                password = await asyncio.to_thread(
                    getpass.getpass, f"[{self.server}] JumpServer password: "
                )
                self._mfa.set_password(password)
            conn = await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.ssh_username,
                password=password,
                client_factory=lambda: self._mfa,
                password_auth=True,
                kbdint_auth=True,
                client_keys=[],
                agent_path=None,
                # 传输层 keepalive，与应用层心跳命令互补。
                keepalive_interval=30,
                keepalive_count_max=3,
            )
        except (EOFError, KeyboardInterrupt):
            self.state = "disconnected"
            self.disconnect_reason = "authentication cancelled"
            PENDING_CREDENTIALS.pop(self.server, None)
            return
        except asyncio.CancelledError:
            self.state = "disconnected"
            PENDING_CREDENTIALS.pop(self.server, None)
            raise
        except (asyncssh.Error, OSError) as exc:
            self.state = "needs_reauth"
            self.disconnect_reason = f"connect failed: {type(exc).__name__}: {exc}"[:300]
            PENDING_CREDENTIALS.pop(self.server, None)
            print(f"[{self.server}] Connection failed: {self.disconnect_reason}")
            return
        PENDING_CREDENTIALS.pop(self.server, None)
        self.conn = conn
        self.state = "connected"
        self.disconnect_reason = None
        self.last_heartbeat = None
        asyncio.create_task(self._watch_disconnect())
        print(f"[{self.server}] SSH authentication succeeded; the connection is kept alive by heartbeat.")

    async def _watch_disconnect(self) -> None:
        """后台任务：连接断开时自动标记为 needs_reauth。"""
        if self.conn is None:
            return
        await self.conn.wait_closed()
        self.mark_disconnected("SSH connection closed by remote or network")


class ConnectionManager:
    """按资产维护连接池；认证与查询解耦（查询不阻塞等待 MFA）。"""

    def __init__(self, config: dict[str, Any]) -> None:
        jump = config["jumpserver"]
        host = str(jump["host"])
        port = int(jump.get("port", 2222))
        self._assets: dict[str, AssetConnection] = {
            name: AssetConnection(name, str(target["ssh_username"]), host, port)
            for name, target in config["targets"].items()
        }
        # 同一时间只允许一个资产进行 MFA 认证，避免终端提示交错。
        self._auth_semaphore = asyncio.Semaphore(1)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # 保护 sync_config，避免并发请求同时重建同一资产条目。
        self._sync_lock = asyncio.Lock()

    @property
    def assets(self) -> dict[str, AssetConnection]:
        return self._assets

    async def sync_config(self, config: dict[str, Any]) -> None:
        """按磁盘最新配置热同步连接池。

        - 新增资产：创建 disconnected 条目，立即可认证；
        - 移除资产：删除条目并关闭其连接；
        - ssh_username/host/port 变更：重建条目（旧连接关闭，需重新认证）。
        修改配置无需重启 gateway。
        """
        jump = config["jumpserver"]
        host = str(jump["host"])
        port = int(jump.get("port", 2222))
        desired = {
            name: (str(target["ssh_username"]), host, port)
            for name, target in config["targets"].items()
        }
        async with self._sync_lock:
            for name in list(self._assets.keys()):
                asset = self._assets[name]
                if name not in desired:
                    self._assets.pop(name)
                    await self._retire(asset)
                elif (asset.ssh_username, asset.host, asset.port) != desired[name]:
                    self._assets[name] = AssetConnection(name, *desired[name])
                    await self._retire(asset)
            for name, params in desired.items():
                if name not in self._assets:
                    self._assets[name] = AssetConnection(name, *params)

    async def _retire(self, asset: AssetConnection) -> None:
        """关闭被移除/重建资产的连接并取消其认证任务。"""
        task = self._tasks.pop(asset.server, None)
        if task is not None and not task.done():
            task.cancel()
        if asset.conn is not None:
            asset.conn.close()
            try:
                await asset.conn.wait_closed()
            except (asyncssh.Error, OSError):
                pass

    def require_connected(self, server: str) -> AssetConnection:
        """取指定资产的可用连接；未连接时抛 404/409 并附操作指引。"""
        asset = self._assets.get(server)
        if asset is None:
            raise HTTPException(status_code=404, detail="Unknown server; not in the allowlist")
        if asset.is_live():
            assert asset.conn is not None
            return asset
        if asset.state == "connected":
            asset.mark_disconnected("SSH connection closed")
        if asset.state == "connecting":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Authentication in progress for '{server}'. Complete password/MFA in the "
                    "gateway terminal, then retry the query."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Asset '{server}' is not connected (state: {asset.state}). Call "
                f"reconnect_asset('{server}') and complete password/MFA in the gateway terminal."
            ),
        )

    def request_connect(self, server: str) -> dict[str, Any]:
        """触发指定资产的后台重连；已有认证任务在跑时去重返回。"""
        asset = self._assets.get(server)
        if asset is None:
            raise HTTPException(status_code=404, detail="Unknown server; not in the allowlist")
        task = self._tasks.get(server)
        if task is not None and not task.done():
            return asset.status_payload() | {
                "note": f"Authentication already in progress for '{server}'. Complete password/MFA in the gateway terminal."
            }
        if asset.is_live():
            return asset.status_payload() | {"note": "Already connected."}
        asset.state = "connecting"
        asset.disconnect_reason = None
        self._tasks[server] = asyncio.create_task(self._connect_guarded(asset))
        return asset.status_payload() | {
            "note": f"Connecting to '{server}'. Complete password/MFA in the gateway terminal now."
        }

    async def _connect_guarded(self, asset: AssetConnection) -> None:
        async with self._auth_semaphore:
            try:
                await asset.connect()
            except Exception as exc:
                PENDING_CREDENTIALS.pop(asset.server, None)
                if asset.state == "connecting":
                    asset.state = "needs_reauth"
                    asset.disconnect_reason = f"connect failed: {type(exc).__name__}: {exc}"[:300]
                    print(f"[{asset.server}] Connection failed: {asset.disconnect_reason}")
                else:
                    raise

    def connected_assets(self) -> list[AssetConnection]:
        return [asset for asset in self._assets.values() if asset.is_live()]

    def statuses(self) -> list[dict[str, Any]]:
        return [asset.status_payload() for asset in self._assets.values()]

    async def close_all(self) -> None:
        """关闭全部连接与认证任务（gateway 退出时调用）。"""
        for task in self._tasks.values():
            task.cancel()
        for asset in self._assets.values():
            if asset.conn is not None:
                asset.conn.close()
                try:
                    await asset.conn.wait_closed()
                except (asyncssh.Error, OSError):
                    pass


async def heartbeat_loop(manager: ConnectionManager, interval_minutes: int) -> None:
    """应用层保活：周期性在每条活动会话上执行 `true`。

    与 asyncssh 传输层 keepalive 互补；JumpServer 的空闲超时以命令活动为准，
    因此需要真实（但零输出）的命令来重置计时。心跳失败即标记资产为需重新认证。
    """
    interval = max(60, interval_minutes * 60)
    while True:
        await asyncio.sleep(interval)
        for asset in manager.connected_assets():
            try:
                async with asset.lock:
                    if asset.conn is None or asset.conn.is_closed():
                        raise OSError("connection closed")
                    await asset.conn.run("true", check=False, timeout=15)
                asset.last_heartbeat = datetime.now().isoformat(timespec="seconds")
            except (asyncssh.Error, asyncio.TimeoutError, OSError) as exc:
                asset.mark_disconnected(f"keepalive failed: {type(exc).__name__}: {exc}")
                print(f"[{asset.server}] {asset.disconnect_reason}")


class BaseLogRequest(BaseModel):
    servers: list[str] = Field(min_length=1, max_length=MAX_SERVERS_PER_QUERY)
    module: str = Field(min_length=1, max_length=100)
    day: str = Field(default="yesterday", min_length=1, max_length=10)
    log_type: Literal["info", "error", "both"] = "both"
    # 可选的不透明 ID（由 discover_log_files 返回）；绝不接受原始路径。
    file_ids: list[str] | None = Field(default=None, max_length=MAX_FILES_PER_QUERY)


class DiscoverRequest(BaseModel):
    servers: list[str] = Field(min_length=1, max_length=MAX_SERVERS_PER_QUERY)
    module: str = Field(min_length=1, max_length=100)
    day: str = Field(default="yesterday", min_length=1, max_length=10)
    log_type: Literal["info", "error", "both"] = "both"
    max_files: int = Field(default=100, ge=1, le=500)


class ReadRequest(BaseLogRequest):
    lines: int = Field(default=200, ge=1, le=MAX_LINES)


class SearchRequest(BaseLogRequest):
    keyword: str = Field(min_length=1, max_length=256)
    max_results: int = Field(default=100, ge=1, le=MAX_MATCHES)


class ExecuteQueryRequest(BaseModel):
    servers: list[str] = Field(min_length=1, max_length=MAX_SERVERS_PER_QUERY)
    module: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=2000)


class SystemQueryRequest(BaseModel):
    servers: list[str] = Field(min_length=1, max_length=MAX_SERVERS_PER_QUERY)
    command: str = Field(min_length=1, max_length=2000)


class ConnectRequest(BaseModel):
    server: str = Field(min_length=1, max_length=100)


class DashboardConnectRequest(BaseModel):
    server: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    mfa_code: str = Field(default="", max_length=32)


def check_bearer(authorization: str | None = Header(default=None)) -> None:
    """校验请求携带的 bearer token（本地 bridge 的唯一鉴权）。"""
    expected = read_token()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


async def get_synced_config(request: Request) -> dict[str, Any]:
    """FastAPI 依赖：每次请求热读配置并同步连接池。

    保证配置文件的新增/删除/凭据变更立即生效，无需重启 gateway；
    同时消除"配置热读、连接池快照"两者不一致的问题。
    """
    config = load_config()
    await request.app.state.manager.sync_config(config)
    return config


def unique_servers(servers: list[str]) -> list[str]:
    """去重并保持顺序。"""
    return list(dict.fromkeys(servers))


async def fan_out(
    servers: list[str], handler: Callable[[str], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """按资产并行执行 handler；单个资产失败转为该资产的 error 条目，不阻断其他资产。"""

    async def one(server: str) -> dict[str, Any]:
        try:
            payload = await handler(server)
            return {"server": server, **payload}
        except HTTPException as exc:
            return {"server": server, "error": f"HTTP {exc.status_code}: {exc.detail}"}
        except Exception as exc:  # 单资产失败不应中断其他资产
            return {"server": server, "error": f"{type(exc).__name__}: {exc}"}

    return list(await asyncio.gather(*(one(server) for server in servers)))


def resolve_target_module(config: dict[str, Any], server: str, module: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """白名单校验：资产与模块必须已在配置中登记。"""
    target = config["targets"].get(server)
    if not target:
        raise HTTPException(status_code=404, detail="Unknown server; not in the allowlist")
    module_config = (target.get("modules") or {}).get(module)
    if not module_config:
        raise HTTPException(status_code=404, detail=f"Unknown module '{module}' for server '{server}'; not in the allowlist")
    return target, module_config


def normalize_log_dir(module_config: dict[str, Any], module: str) -> str:
    """校验并规范化配置的 log_dir：必须为绝对路径且不含 '..'。"""
    log_dir = str(module_config.get("log_dir", "")).strip()
    if not log_dir.startswith("/") or "\n" in log_dir or "\r" in log_dir:
        raise HTTPException(status_code=400, detail=f"Configure an absolute Linux log_dir for module '{module}'.")
    if ".." in PurePosixPath(log_dir).parts:
        raise HTTPException(status_code=400, detail="Configured log_dir must not contain '..' path components")
    return str(PurePosixPath(log_dir))


def resolve_requested_date(config: dict[str, Any], module_config: dict[str, Any], day_arg: str) -> tuple[date, str]:
    """把 today/yesterday/YYYY-MM-DD 解析为配置时区下的具体日期。"""
    timezone_name = str(module_config.get("timezone") or config.get("log_timezone") or "UTC")
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"Unknown log timezone in config: {timezone_name}") from exc
    today = datetime.now(tz).date()
    normalized = day_arg.strip().lower()
    if normalized == "today":
        selected = today
    elif normalized == "yesterday":
        selected = today - timedelta(days=1)
    else:
        try:
            selected = date.fromisoformat(day_arg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="day must be 'today', 'yesterday', or YYYY-MM-DD") from exc
    return selected, timezone_name


def parse_date_hint(path: str) -> str | None:
    """在相对路径任意位置（含目录名）识别常见日期格式，返回 ISO 日期或 None。"""
    import re
    name = PurePosixPath(path).as_posix()
    patterns = (
        r"(?<!\d)(20\d{2})[-_.](\d{2})[-_.](\d{2})(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, name)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            except ValueError:
                continue
    return None


def is_compressed(path: str) -> bool:
    return PurePosixPath(path).name.lower().endswith(COMPRESSED_SUFFIXES)


def is_log_candidate(path: str) -> bool:
    """判断文件是否像日志：文本后缀或名称含日志标记。"""
    name = PurePosixPath(path).as_posix().lower()
    basename = PurePosixPath(path).name.lower()
    if is_compressed(path):
        # 压缩文件可被发现，配合 execute_query 的 zgrep/zcat 查询。
        return any(marker in name for marker in LOG_NAME_MARKERS) or ".log" in basename
    suffix = PurePosixPath(basename).suffix
    return suffix in TEXT_SUFFIXES or any(marker in name for marker in LOG_NAME_MARKERS)


def infer_kind(path: str) -> str:
    """按文件名标记推断日志类型：error / info / generic。"""
    name = PurePosixPath(path).as_posix().lower()
    if any(marker in name for marker in ERROR_MARKERS):
        return "error"
    if any(marker in name for marker in INFO_MARKERS):
        return "info"
    return "generic"


def matches_type(path: str, log_type: str) -> bool:
    if log_type == "both":
        return True
    kind = infer_kind(path)
    # 通用的 app.log/server.log 两类搜索都纳入；仅排除明确属于另一类型的文件。
    if log_type == "error":
        return kind != "info"
    return kind != "error"


def file_id_for(server: str, module: str, relative_path: str) -> str:
    """生成不透明 file_id（含 server，因此多资产批量查询时各资产只匹配自己的 ID）。"""
    return hashlib.sha256(f"{server}\0{module}\0{relative_path}".encode("utf-8")).hexdigest()[:16]


async def probe_connection(asset: AssetConnection) -> None:
    """主动执行 `true` 验证会话真实可用；失败抛异常由调用方处理。

    `conn.is_closed()` 无法及时感知服务端断连或半开连接，
    会出现"状态 connected、命令 exit 0 但输出为空"的假象。
    在下发真实命令前探测，可将此类故障提前暴露为明确的 409 重连提示。
    """
    assert asset.conn is not None
    result = await asset.conn.run("true", check=False, timeout=CONNECTION_TEST_TIMEOUT_SECONDS)
    if result.exit_status != 0:
        raise OSError(f"probe returned exit status {result.exit_status}")


async def run_remote(app: FastAPI, server: str, command: str, timeout: int = 30) -> asyncssh.SSHCompletedProcess[str]:
    """在指定资产的 SSH 会话上执行命令（持该资产的锁，串行化）。"""
    manager: ConnectionManager = app.state.manager
    asset = manager.require_connected(server)
    try:
        async with asset.lock:
            if asset.conn is None or asset.conn.is_closed():
                asset.mark_disconnected("SSH connection closed")
                raise HTTPException(
                    status_code=409,
                    detail=f"Asset '{server}' connection closed. Call reconnect_asset('{server}') and complete MFA in the gateway terminal.",
                )
            try:
                await probe_connection(asset)
            except (asyncssh.Error, asyncio.TimeoutError, OSError) as exc:
                asset.mark_disconnected(f"connection probe failed: {type(exc).__name__}: {exc}")
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Connection probe failed on '{server}' ({type(exc).__name__}). Call "
                        f"reconnect_asset('{server}') and complete password/MFA in the gateway terminal."
                    ),
                ) from exc
            return await asset.conn.run(command, check=False, timeout=timeout)
    except (asyncssh.Error, asyncio.TimeoutError) as exc:
        asset.mark_disconnected(f"remote operation failed: {type(exc).__name__}")
        raise HTTPException(status_code=502, detail=f"Remote log operation failed on '{server}': {type(exc).__name__}") from exc


async def run_remote_piped(app: FastAPI, server: str, command: str, timeout: int = 30) -> asyncssh.SSHCompletedProcess[str]:
    """执行命令并把输出截断到 MAX_READ_BYTES。

    pipefail 让退出码反映命令本身（而不是 head）。输出超限时 head 提前关闭管道，
    上游收到 SIGPIPE（141）属于预期的截断，调用方需把 141 当作成功。
    """
    inner = f"set -o pipefail; {command} | head -c {MAX_READ_BYTES}"
    return await run_remote(app, server, "bash -c " + shlex.quote(inner), timeout=timeout)


async def discover_remote_files(
    app: FastAPI, config: dict[str, Any], server: str, module: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], bool]:
    """在远程 log_dir 下执行受限 find，返回带元信息与推断结果的候选文件列表。"""
    _, module_config = resolve_target_module(config, server, module)
    log_dir = normalize_log_dir(module_config, module)
    # 命令中唯一的路径来自服务端白名单配置，模型无法注入。
    command = (
        f"find {shlex.quote(log_dir)} -maxdepth {MAX_DEPTH} -type f "
        "-printf '%p\\t%s\\t%T@\\n' 2>/dev/null "
        f"| head -n {MAX_DISCOVERY_FILES}"
    )
    result = await run_remote_piped(app, server, command, timeout=30)
    # find 权限不足会返回 1 但仍输出部分结果；141 = 输出被 head 截断。
    if result.exit_status not in (0, 1, None, 141):
        detail = (result.stderr or "Unable to list the configured log directory").strip()[:1000]
        raise HTTPException(status_code=502, detail=detail)

    _, timezone_name = resolve_requested_date(config, module_config, "today")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"Unknown log timezone in config: {timezone_name}") from exc

    files: list[dict[str, Any]] = []
    truncated_listing = False
    lines = (result.stdout or "").splitlines()
    if len(lines) >= MAX_DISCOVERY_FILES:
        truncated_listing = True
    root = PurePosixPath(log_dir)
    for line in lines:
        parts = line.rsplit("\t", 2)
        if len(parts) != 3:
            continue
        path, size_raw, mtime_raw = parts
        p = PurePosixPath(path)
        try:
            relative = p.relative_to(root).as_posix()
            size = int(size_raw)
            mtime = float(mtime_raw)
        except (ValueError, TypeError):
            continue
        if relative in ("", ".") or relative.startswith("../"):
            continue
        candidate = is_log_candidate(path)
        compressed = is_compressed(path)
        mtime_local = datetime.fromtimestamp(mtime, timezone).isoformat(timespec="seconds")
        files.append({
            "file_id": file_id_for(server, module, relative),
            "relative_path": relative,
            "size_bytes": size,
            "mtime_epoch": mtime,
            "mtime_local": mtime_local,
            "date_hint": parse_date_hint(path),
            "kind_hint": infer_kind(path),
            "is_log_candidate": candidate,
            "compressed": compressed,
            "searchable": candidate and not compressed,
        })
    files.sort(key=lambda item: item["mtime_epoch"], reverse=True)
    return log_dir, module_config, files, truncated_listing


def select_files(
    files: list[dict[str, Any]], requested_date: date, today: date, log_type: str,
    selected_ids: list[str] | None, server: str,
) -> list[dict[str, Any]]:
    """按日期/类型自动选择文件；若传入 file_ids 则只取属于本资产的交集。"""
    candidates = [item for item in files if item["is_log_candidate"] and matches_type(item["relative_path"], log_type)]
    by_id = {item["file_id"]: item for item in candidates if item["searchable"]}
    if selected_ids:
        # file_id 内嵌 server，多资产批量查询时每个资产只匹配自己的 ID；
        # 不匹配的 ID 忽略，交集为空才报错。
        matched = [by_id[file_id] for file_id in dict.fromkeys(selected_ids) if file_id in by_id]
        if not matched:
            raise HTTPException(
                status_code=400,
                detail=f"None of the given file_ids belong to server '{server}' or are searchable. Call discover_log_files again.",
            )
        return matched

    selected_iso = requested_date.isoformat()
    chosen = []
    for item in candidates:
        if not item["searchable"]:
            continue
        date_hint = item["date_hint"]
        if requested_date == today:
            # 当天的活动日志通常无日期后缀（如 info.log/error.log）。
            if date_hint is None or date_hint == selected_iso:
                chosen.append(item)
        elif date_hint == selected_iso:
            chosen.append(item)
    # 防止目录内匹配文件过多导致远程 grep 无界扫描。
    return chosen[:MAX_FILES_PER_QUERY]


def candidate_summary(files: list[dict[str, Any]], log_type: str, limit: int = 30) -> list[dict[str, Any]]:
    """裁剪候选文件列表，仅保留展示所需字段。"""
    candidates = [item for item in files if item["is_log_candidate"] and matches_type(item["relative_path"], log_type)]
    return [{
        "file_id": item["file_id"],
        "relative_path": item["relative_path"],
        "size_bytes": item["size_bytes"],
        "mtime_local": item["mtime_local"],
        "date_hint": item["date_hint"],
        "kind_hint": item["kind_hint"],
        "searchable": item["searchable"],
    } for item in candidates[:limit]]


def selection_payload(
    config: dict[str, Any], module_config: dict[str, Any], day: str, log_type: str,
    files: list[dict[str, Any]], selected_ids: list[str] | None, truncated_listing: bool,
    server: str, module: str,
) -> dict[str, Any]:
    """组装单资产的选择结果载荷（选中文件 + 候选清单 + 时区等元信息）。"""
    selected_date, timezone_name = resolve_requested_date(config, module_config, day)
    today, _ = resolve_requested_date(config, module_config, "today")
    chosen = select_files(files, selected_date, today, log_type, selected_ids, server)
    return {
        "server": server,
        "module": module,
        "log_dir": module_config["log_dir"],
        "day": selected_date.isoformat(),
        "log_type": log_type,
        "selected_files": chosen,
        "candidate_files": candidate_summary(files, log_type),
        "truncated_listing": truncated_listing,
        "timezone": timezone_name,
    }


async def read_tail_from_ssh(app: FastAPI, server: str, paths: list[str], lines: int) -> tuple[str, bool, str]:
    """远程 tail 多个文件并按字节截断；返回 (输出, 是否被截断, stderr)。"""
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = f"tail -n {int(lines)} -- {quoted_paths}"
    result = await run_remote_piped(app, server, command, timeout=45)
    if result.exit_status not in (0, None, 141):  # 141 = 输出被 head 截断
        detail = (result.stderr or "Remote tail command failed").strip()[:1500]
        raise HTTPException(status_code=502, detail=detail)
    output = result.stdout or ""
    return output, len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES, (result.stderr or "").strip()[:1000]


async def search_files_on_ssh(app: FastAPI, server: str, paths: list[str], keyword: str, max_results: int) -> tuple[str, bool, str]:
    """远程固定字符串搜索（grep -F），限行数与字节数；返回 (输出, 是否被截断, stderr)。"""
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = (
        f"grep -F -H -n -- {shlex.quote(keyword)} {quoted_paths} "
        f"| head -n {int(max_results)}"
    )
    result = await run_remote_piped(app, server, command, timeout=QUERY_TIMEOUT_SECONDS)
    # grep 退出 1 = 无匹配，不是错误；141 = 行数/字节被 head 截断。
    if result.exit_status not in (0, 1, None, 141):
        detail = (result.stderr or "Remote grep command failed").strip()[:1500]
        raise HTTPException(status_code=502, detail=detail)
    output = result.stdout or ""
    return output, len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES, (result.stderr or "").strip()[:1000]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：建连接池、起心跳任务、按 auto_connect 预认证；退出时清理。"""
    config = load_config()
    read_token()
    manager = ConnectionManager(config)
    app.state.manager = manager
    interval = int(config["jumpserver"].get("keepalive_interval_minutes", DEFAULT_KEEPALIVE_MINUTES))
    heartbeat = asyncio.create_task(heartbeat_loop(manager, interval))
    for server in config["jumpserver"].get("auto_connect") or []:
        if server in manager.assets:
            manager.request_connect(server)
        else:
            print(f"[warn] auto_connect: unknown server '{server}' ignored.")
    print(
        "Gateway ready. Authenticate assets via the reconnect_asset MCP tool, "
        "POST /connect, or the web dashboard at /dashboard."
    )
    try:
        yield
    finally:
        heartbeat.cancel()
        await manager.close_all()


app = FastAPI(title="Local Multi-asset Directory-discovery Log Gateway", lifespan=lifespan)


@app.get("/health")
async def health(_config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """健康检查：资产总数、已连接数、已连接资产名。"""
    manager: ConnectionManager = app.state.manager
    connected = manager.connected_assets()
    return {
        "assets_total": len(manager.assets),
        "assets_connected": len(connected),
        "connected_servers": [asset.server for asset in connected],
    }


@app.get("/targets")
async def targets(config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """列出全部资产、模块（含业务描述）、日志目录与连接状态。"""
    manager: ConnectionManager = app.state.manager
    statuses = {item["server"]: item for item in manager.statuses()}
    return {
        "targets": {
            name: {
                "description": target.get("description", ""),
                "modules": {
                    module: {"log_dir": cfg.get("log_dir", ""), "description": cfg.get("description", "")}
                    for module, cfg in (target.get("modules") or {}).items()
                },
                "connection": statuses.get(name),
            }
            for name, target in config["targets"].items()
        },
        "log_timezone": config.get("log_timezone", "UTC"),
        "supported_day_values": ["today", "yesterday", "YYYY-MM-DD"],
    }


@app.get("/connections")
async def connections(_config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """列出各资产的连接状态、上次心跳时间与断开原因。"""
    manager: ConnectionManager = app.state.manager
    return {"connections": manager.statuses()}


@app.post("/connect")
async def connect(request: ConnectRequest, _config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """触发指定资产的后台重连认证（MFA 在 gateway 终端完成）。"""
    manager: ConnectionManager = app.state.manager
    return manager.request_connect(request.server)


@app.post("/discover-logs")
async def discover_logs(request: DiscoverRequest, http_request: Request, config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """多资产动态发现日志文件；返回全部候选的 file_id 供后续精确查询。"""

    async def handler(server: str) -> dict[str, Any]:
        log_dir, module_config, files, truncated = await discover_remote_files(http_request.app, config, server, request.module)
        payload = selection_payload(config, module_config, request.day, request.log_type, files, None, truncated, server, request.module)
        payload["candidate_files"] = candidate_summary(files, request.log_type, request.max_files)
        payload["log_dir"] = log_dir
        payload["auto_selected_files"] = [
            {key: item[key] for key in ("file_id", "relative_path", "size_bytes", "mtime_local", "date_hint", "kind_hint", "searchable")}
            for item in payload.pop("selected_files")
        ]
        payload["note"] = (
            "Only file_ids discovered under the configured log_dir may be passed to read/search. "
            "Compressed files are not searchable with search/read-tail; use execute_query with zgrep/zcat for them."
        )
        return payload

    return {"results": await fan_out(unique_servers(request.servers), handler)}


@app.post("/read-tail")
async def read_tail(request: ReadRequest, http_request: Request, config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """多资产读取日志末尾；文件按日期/类型自动选择或用 file_ids 指定。"""

    async def handler(server: str) -> dict[str, Any]:
        log_dir, module_config, files, truncated_listing = await discover_remote_files(http_request.app, config, server, request.module)
        payload = selection_payload(config, module_config, request.day, request.log_type, files, request.file_ids, truncated_listing, server, request.module)
        chosen = payload.pop("selected_files")
        if not chosen:
            return {"found": False, **payload, "message": "Could not automatically identify searchable log files for that day/type. Review candidate_files from discover_log_files and retry with file_ids."}
        paths = [str(PurePosixPath(log_dir) / item["relative_path"]) for item in chosen]
        text, truncated, stderr = await read_tail_from_ssh(http_request.app, server, paths, request.lines)
        return {"found": True, **payload, "files_read": chosen, "lines_requested_per_file": request.lines, "text": text, "possibly_truncated_by_bytes": truncated, "stderr": stderr}

    return {"results": await fan_out(unique_servers(request.servers), handler)}


@app.post("/search-logs")
async def search_logs(request: SearchRequest, http_request: Request, config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """多资产固定字符串搜索（grep -F）；文件按日期/类型自动选择或用 file_ids 指定。"""

    async def handler(server: str) -> dict[str, Any]:
        log_dir, module_config, files, truncated_listing = await discover_remote_files(http_request.app, config, server, request.module)
        payload = selection_payload(config, module_config, request.day, request.log_type, files, request.file_ids, truncated_listing, server, request.module)
        chosen = payload.pop("selected_files")
        if not chosen:
            return {"found": False, **payload, "keyword": request.keyword, "message": "Could not automatically identify searchable log files for that day/type. Review candidate_files from discover_log_files and retry with file_ids."}
        paths = [str(PurePosixPath(log_dir) / item["relative_path"]) for item in chosen]
        text, truncated, stderr = await search_files_on_ssh(http_request.app, server, paths, request.keyword, request.max_results)
        return {"found": True, **payload, "keyword": request.keyword, "files_searched": chosen, "matches_text": text, "possibly_truncated_by_bytes": truncated, "stderr": stderr}

    return {"results": await fan_out(unique_servers(request.servers), handler)}


@app.post("/execute-query")
async def execute_query(request: ExecuteQueryRequest, http_request: Request, config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """多资产受限自由只读查询：静态校验通过后以 cwd=log_dir 执行命令。"""

    async def handler(server: str) -> dict[str, Any]:
        _, module_config = resolve_target_module(config, server, request.module)
        log_dir = normalize_log_dir(module_config, request.module)
        try:
            validate_query_command(request.command, log_dir)
        except CommandValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 工作目录锁定在授权 log_dir；输出由服务端按字节截断。
        inner = f"cd {shlex.quote(log_dir)} && {request.command}"
        result = await run_remote_piped(http_request.app, server, inner, timeout=QUERY_TIMEOUT_SECONDS)
        output = result.stdout or ""
        truncated = len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES
        # 141 = SIGPIPE（head 提前关管道，可能是服务端截断，也可是用户自己的 head），不是失败。
        exit_code = 0 if result.exit_status == 141 else result.exit_status
        return {
            "module": request.module,
            "log_dir": log_dir,
            "executed_command": request.command,
            "exit_code": exit_code,
            "stdout": output,
            "stderr": (result.stderr or "").strip()[:500],
            "possibly_truncated_by_bytes": truncated,
        }

    return {"results": await fan_out(unique_servers(request.servers), handler)}


@app.post("/execute-system-query")
async def execute_system_query(request: SystemQueryRequest, http_request: Request, config: dict[str, Any] = Depends(get_synced_config), _: None = Depends(check_bearer)) -> dict[str, Any]:
    """多资产系统/Docker/K8s 只读查看：校验通过后在远程默认工作目录执行命令。"""

    async def handler(server: str) -> dict[str, Any]:
        if server not in (config.get("targets") or {}):
            raise HTTPException(status_code=404, detail="Unknown server; not in the allowlist")
        verdict = validate_system_info_command(request.command)
        if verdict.verdict == Verdict.REJECTED:
            raise HTTPException(status_code=400, detail=f"REJECTED: {verdict.message}")
        if verdict.verdict == Verdict.UNKNOWN:
            raise HTTPException(status_code=400, detail=f"UNKNOWN: {verdict.message}")
        # ALLOWED — 系统命令不需要锁定 log_dir；在远程默认目录执行
        result = await run_remote_piped(
            http_request.app, server, request.command, timeout=QUERY_TIMEOUT_SECONDS
        )
        output = result.stdout or ""
        truncated = len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES
        exit_code = 0 if result.exit_status == 141 else result.exit_status
        return {
            "executed_command": request.command,
            "exit_code": exit_code,
            "stdout": output,
            "stderr": (result.stderr or "").strip()[:500],
            "possibly_truncated_by_bytes": truncated,
        }

    return {"results": await fan_out(unique_servers(request.servers), handler)}


# ── Web 仪表盘 ──────────────────────────────────────────────


@app.get("/dashboard")
async def dashboard(token: str | None = None) -> HTMLResponse:
    """Web 仪表盘：访问 /dashboard?token=<local.token> 查看全部资产状态并提交认证凭据。"""
    if not token or not secrets.compare_digest(token, read_token()):
        return HTMLResponse(LOGIN_HTML)
    return HTMLResponse(DASHBOARD_HTML.replace("$TOKEN", token))


@app.get("/dashboard/status")
async def dashboard_status(request: Request, _: None = Depends(check_bearer)) -> dict[str, Any]:
    """返回所有资产连接状态（JSON），供仪表盘自动轮询。"""
    return {"connections": request.app.state.manager.statuses()}


@app.post("/dashboard/connect")
async def dashboard_connect(
    body: DashboardConnectRequest,
    request: Request,
    _: None = Depends(check_bearer),
) -> dict[str, Any]:
    """仪表盘提交 JumpServer 认证凭据，触发指定资产后台连接。"""
    PENDING_CREDENTIALS[body.server] = {
        "password": body.password,
        "mfa_code": body.mfa_code,
    }
    return request.app.state.manager.request_connect(body.server)


if __name__ == "__main__":
    config = load_config()
    jump = config["jumpserver"]
    uvicorn.run(app, host=str(jump.get("bind_host", "127.0.0.1")), port=int(jump.get("bind_port", 8765)), log_level="warning")
