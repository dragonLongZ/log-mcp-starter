"""Interactive JumpServer SSH session and directory-discovery log bridge.

The user configures only an allowlisted module log_dir. The bridge discovers
files under that directory at query time; it does not require filename templates.
Run in a dedicated terminal and complete the normal password/MFA flow.
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
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncssh
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "log_targets.yaml"
TOKEN_PATH = ROOT / "local.token"
MAX_READ_BYTES = 100_000
MAX_LINES = 2_000
MAX_MATCHES = 200
MAX_DISCOVERY_FILES = 2_000
MAX_FILES_PER_QUERY = 50
MAX_DEPTH = 5

# Common text-log suffixes plus recognizable names without a conventional suffix.
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
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict) or "jumpserver" not in config or "targets" not in config:
        raise RuntimeError(f"Invalid configuration: {CONFIG_PATH}")
    return config


def read_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError("local.token is missing. Run `uv run python create_token.py` first.")
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("local.token is too short; regenerate it with create_token.py.")
    return token


class ConsoleMFA(asyncssh.SSHClient):
    """Prompt in this dedicated terminal and reuse password if requested again."""
    def __init__(self) -> None:
        self._password: str | None = None

    def password_auth_requested(self) -> str:
        if self._password is None:
            self._password = getpass.getpass("JumpServer password: ")
        return self._password

    def kbdint_auth_requested(self) -> str:
        return ""

    def kbdint_challenge_received(
        self, name: str, instructions: str, lang: str,
        prompts: list[tuple[str, bool]],
    ) -> list[str]:
        if instructions:
            print(instructions)
        answers: list[str] = []
        for prompt, echo in prompts:
            lowered = prompt.lower()
            if "password" in lowered:
                answer = self.password_auth_requested()
            elif any(word in lowered for word in ("otp", "mfa", "verification", "code", "验证码")):
                answer = input(prompt) if echo else getpass.getpass(prompt)
            else:
                answer = input(prompt) if echo else getpass.getpass(prompt)
            answers.append(answer)
        return answers


class BaseLogRequest(BaseModel):
    server: str = Field(min_length=1, max_length=100)
    module: str = Field(min_length=1, max_length=100)
    day: str = Field(default="yesterday", min_length=1, max_length=10)
    log_type: Literal["info", "error", "both"] = "both"
    # Optional opaque IDs returned by discover_log_files; never raw paths.
    file_ids: list[str] | None = Field(default=None, max_length=MAX_FILES_PER_QUERY)


class DiscoverRequest(BaseModel):
    server: str = Field(min_length=1, max_length=100)
    module: str = Field(min_length=1, max_length=100)
    day: str = Field(default="yesterday", min_length=1, max_length=10)
    log_type: Literal["info", "error", "both"] = "both"
    max_files: int = Field(default=100, ge=1, le=500)


class ReadRequest(BaseLogRequest):
    lines: int = Field(default=200, ge=1, le=MAX_LINES)


class SearchRequest(BaseLogRequest):
    keyword: str = Field(min_length=1, max_length=256)
    max_results: int = Field(default=100, ge=1, le=MAX_MATCHES)


def check_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = read_token()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def resolve_target_module(config: dict[str, Any], server: str, module: str) -> tuple[dict[str, Any], dict[str, Any]]:
    active = config["jumpserver"]["active_target"]
    if server != active:
        raise HTTPException(status_code=400, detail=f"This SSH session is connected to '{active}', not '{server}'. This starter supports one active asset.")
    target = config["targets"].get(server)
    if not target:
        raise HTTPException(status_code=404, detail="Unknown server; not in the allowlist")
    module_config = (target.get("modules") or {}).get(module)
    if not module_config:
        raise HTTPException(status_code=404, detail="Unknown module; not in the allowlist")
    return target, module_config


def normalize_log_dir(module_config: dict[str, Any], module: str) -> str:
    log_dir = str(module_config.get("log_dir", "")).strip()
    if not log_dir.startswith("/") or "\n" in log_dir or "\r" in log_dir:
        raise HTTPException(status_code=400, detail=f"Configure an absolute Linux log_dir for module '{module}'.")
    if ".." in PurePosixPath(log_dir).parts:
        raise HTTPException(status_code=400, detail="Configured log_dir must not contain '..' path components")
    return str(PurePosixPath(log_dir))


def resolve_requested_date(config: dict[str, Any], module_config: dict[str, Any], day_arg: str) -> tuple[date, str]:
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
    """Find common date tokens anywhere in the relative path, including directory names."""
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
    name = PurePosixPath(path).as_posix().lower()
    basename = PurePosixPath(path).name.lower()
    if is_compressed(path):
        # Compressed files are discoverable for transparency but not searched by this starter.
        return any(marker in name for marker in LOG_NAME_MARKERS) or ".log" in basename
    suffix = PurePosixPath(basename).suffix
    return suffix in TEXT_SUFFIXES or any(marker in name for marker in LOG_NAME_MARKERS)


def infer_kind(path: str) -> str:
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
    # Include generic app.log/server.log in either search; exclude clearly typed opposite streams.
    if log_type == "error":
        return kind != "info"
    return kind != "error"


def file_id_for(server: str, module: str, relative_path: str) -> str:
    return hashlib.sha256(f"{server}\0{module}\0{relative_path}".encode("utf-8")).hexdigest()[:16]


async def get_connection(app: FastAPI) -> asyncssh.SSHClientConnection:
    conn: asyncssh.SSHClientConnection | None = getattr(app.state, "ssh_conn", None)
    if conn is None or conn.is_closed():
        raise HTTPException(status_code=503, detail="SSH session is closed. Restart gateway_host.py and complete MFA again.")
    return conn


async def run_remote(app: FastAPI, command: str, timeout: int = 30) -> asyncssh.SSHCompletedProcess[str]:
    conn = await get_connection(app)
    lock: asyncio.Lock = app.state.ssh_lock
    try:
        async with lock:
            return await conn.run(command, check=False, timeout=timeout)
    except (asyncssh.Error, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Remote log operation failed: {type(exc).__name__}") from exc


async def discover_remote_files(
    app: FastAPI, config: dict[str, Any], server: str, module: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], bool]:
    _, module_config = resolve_target_module(config, server, module)
    log_dir = normalize_log_dir(module_config, module)
    # The only path in the command comes from the server-side allowlist.
    command = (
        f"find {shlex.quote(log_dir)} -maxdepth {MAX_DEPTH} -type f "
        "-printf '%p\\t%s\\t%T@\\n' 2>/dev/null "
        f"| head -n {MAX_DISCOVERY_FILES}"
    )
    result = await run_remote(app, command, timeout=30)
    if result.exit_status not in (0, None):
        detail = (result.stderr or "Unable to list the configured log directory").strip()[:1000]
        raise HTTPException(status_code=502, detail=detail)

    selected, timezone_name = resolve_requested_date(config, module_config, "today")
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
    selected_ids: list[str] | None,
) -> list[dict[str, Any]]:
    candidates = [item for item in files if item["is_log_candidate"] and matches_type(item["relative_path"], log_type)]
    by_id = {item["file_id"]: item for item in candidates if item["searchable"]}
    if selected_ids:
        unknown = [file_id for file_id in selected_ids if file_id not in by_id]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown or non-searchable file_id(s): {', '.join(unknown)}. Call discover_log_files again.")
        return [by_id[file_id] for file_id in dict.fromkeys(selected_ids)]

    selected_iso = requested_date.isoformat()
    chosen = []
    for item in candidates:
        if not item["searchable"]:
            continue
        date_hint = item["date_hint"]
        if requested_date == today:
            # Today's active logs are often undated (e.g. info.log/error.log).
            if date_hint is None or date_hint == selected_iso:
                chosen.append(item)
        elif date_hint == selected_iso:
            chosen.append(item)
    # Avoid an unbounded remote grep if a directory contains many matching log files.
    return chosen[:MAX_FILES_PER_QUERY]


def candidate_summary(files: list[dict[str, Any]], log_type: str, limit: int = 30) -> list[dict[str, Any]]:
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
    selected_date, timezone_name = resolve_requested_date(config, module_config, day)
    today, _ = resolve_requested_date(config, module_config, "today")
    chosen = select_files(files, selected_date, today, log_type, selected_ids)
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


async def read_tail_from_ssh(app: FastAPI, paths: list[str], lines: int) -> tuple[str, bool]:
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = f"tail -n {int(lines)} -- {quoted_paths} | head -c {MAX_READ_BYTES}"
    result = await run_remote(app, command, timeout=45)
    if result.exit_status not in (0, None):
        detail = (result.stderr or "Remote tail command failed").strip()[:1500]
        raise HTTPException(status_code=502, detail=detail)
    output = result.stdout or ""
    return output, len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES


async def search_files_on_ssh(app: FastAPI, paths: list[str], keyword: str, max_results: int) -> tuple[str, bool]:
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = (
        f"grep -F -H -n -- {shlex.quote(keyword)} {quoted_paths} "
        f"| head -n {int(max_results)} | head -c {MAX_READ_BYTES}"
    )
    result = await run_remote(app, command, timeout=60)
    if result.exit_status not in (0, None):
        detail = (result.stderr or "Remote grep command failed").strip()[:1500]
        raise HTTPException(status_code=502, detail=detail)
    output = result.stdout or ""
    return output, len(output.encode("utf-8", errors="replace")) >= MAX_READ_BYTES


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    jump = config["jumpserver"]
    read_token()
    print("Connecting to JumpServer. Enter credentials only in this PowerShell window.")
    conn = await asyncssh.connect(
        jump["host"],
        port=int(jump.get("port", 2222)),
        username=jump["username"],
        client_factory=ConsoleMFA,
        password_auth=True,
        kbdint_auth=True,
        client_keys=[],
        agent_path=None,
        keepalive_interval=30,
        keepalive_count_max=3,
    )
    app.state.ssh_conn = conn
    app.state.ssh_lock = asyncio.Lock()
    print("SSH authentication succeeded. The connection will be reused until it closes.")
    try:
        yield
    finally:
        conn.close()
        await conn.wait_closed()


app = FastAPI(title="Local Directory-discovery Log Gateway", lifespan=lifespan)


@app.get("/health")
async def health(_: None = Depends(check_bearer)) -> dict[str, Any]:
    conn: asyncssh.SSHClientConnection | None = getattr(app.state, "ssh_conn", None)
    return {"ssh_connected": bool(conn and not conn.is_closed())}


@app.get("/targets")
async def targets(_: None = Depends(check_bearer)) -> dict[str, Any]:
    config = load_config()
    active = config["jumpserver"]["active_target"]
    target = config["targets"].get(active, {})
    return {
        "active_target": active,
        "description": target.get("description", ""),
        "modules": {name: {"log_dir": cfg.get("log_dir", "")} for name, cfg in (target.get("modules") or {}).items()},
        "log_timezone": config.get("log_timezone", "UTC"),
        "supported_day_values": ["today", "yesterday", "YYYY-MM-DD"],
    }


@app.post("/discover-logs")
async def discover_logs(request: DiscoverRequest, http_request: Request, _: None = Depends(check_bearer)) -> dict[str, Any]:
    config = load_config()
    log_dir, module_config, files, truncated = await discover_remote_files(http_request.app, config, request.server, request.module)
    payload = selection_payload(config, module_config, request.day, request.log_type, files, None, truncated, request.server, request.module)
    # Includes all discovered candidate IDs so the model can select a nonstandard filename safely.
    payload["candidate_files"] = candidate_summary(files, request.log_type, request.max_files)
    payload["log_dir"] = log_dir
    payload["auto_selected_files"] = [
        {key: item[key] for key in ("file_id", "relative_path", "size_bytes", "mtime_local", "date_hint", "kind_hint", "searchable")}
        for item in payload.pop("selected_files")
    ]
    payload["note"] = "Only file_ids discovered under the configured log_dir may be passed to read/search. Compressed files are listed but not searchable in this starter."
    return payload


@app.post("/read-tail")
async def read_tail(request: ReadRequest, http_request: Request, _: None = Depends(check_bearer)) -> dict[str, Any]:
    config = load_config()
    log_dir, module_config, files, truncated_listing = await discover_remote_files(http_request.app, config, request.server, request.module)
    payload = selection_payload(config, module_config, request.day, request.log_type, files, request.file_ids, truncated_listing, request.server, request.module)
    chosen = payload["selected_files"]
    if not chosen:
        return {"found": False, **{k: v for k, v in payload.items() if k != "selected_files"}, "message": "Could not automatically identify searchable log files for that day/type. Review candidate_files from discover_log_files and retry with file_ids."}
    paths = [str(PurePosixPath(log_dir) / item["relative_path"]) for item in chosen]
    text, truncated = await read_tail_from_ssh(http_request.app, paths, request.lines)
    return {"found": True, **{k: v for k, v in payload.items() if k != "selected_files"}, "files_read": chosen, "lines_requested_per_file": request.lines, "text": text, "possibly_truncated_by_bytes": truncated}


@app.post("/search-logs")
async def search_logs(request: SearchRequest, http_request: Request, _: None = Depends(check_bearer)) -> dict[str, Any]:
    config = load_config()
    log_dir, module_config, files, truncated_listing = await discover_remote_files(http_request.app, config, request.server, request.module)
    payload = selection_payload(config, module_config, request.day, request.log_type, files, request.file_ids, truncated_listing, request.server, request.module)
    chosen = payload["selected_files"]
    if not chosen:
        return {"found": False, **{k: v for k, v in payload.items() if k != "selected_files"}, "keyword": request.keyword, "message": "Could not automatically identify searchable log files for that day/type. Review candidate_files from discover_log_files and retry with file_ids."}
    paths = [str(PurePosixPath(log_dir) / item["relative_path"]) for item in chosen]
    text, truncated = await search_files_on_ssh(http_request.app, paths, request.keyword, request.max_results)
    return {"found": True, **{k: v for k, v in payload.items() if k != "selected_files"}, "keyword": request.keyword, "files_searched": chosen, "matches_text": text, "possibly_truncated_by_bytes": truncated}


if __name__ == "__main__":
    config = load_config()
    jump = config["jumpserver"]
    uvicorn.run(app, host=str(jump.get("bind_host", "127.0.0.1")), port=int(jump.get("bind_port", 8765)), log_level="warning")
