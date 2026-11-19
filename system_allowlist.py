"""自定义系统命令白名单 —— 网关内置白名单的运行时扩展。

`system_guard.py` 内置了一份系统命令白名单；不在内置白/黑名单里的命令会返回
UNKNOWN，模型只能提示用户手工执行。本模块提供一份**可在线维护**的补充白名单：

  * 存储：``config/system_allowlist.yaml``（与 log_targets.yaml 同目录，便于备份/进版本库）。
  * 热加载：每次校验都按文件 mtime/size 判断是否变化，变了才重新解析，
    因此网页上增删一条后，下一次请求立即生效，无需重启 gateway。
  * 只做“加白”：黑名单（rm/sudo/docker rm…）与通用安全规则（禁止重定向、
    串联、命令替换、awk 写原语…）始终优先，无法通过本文件绕过。

文件格式::

    version: 1
    commands:
      - name: "htop"                 # 命令前缀，按空格切成 token 逐级匹配
      - name: "docker compose ls"
      - name: "ncdu"
        denied: ["-o", "--output"]   # 可选：出现这些 flag 时仍拒绝
        note: "只看目录占用"
        added_at: "2026-11-19 10:00:00"

``name`` 只允许字母/数字与少量符号（``- _ . / : = + @ * ?[]``），拒绝空白以外的
shell 元字符，防止有人把 ``ps; rm -rf /`` 之类整条命令塞进白名单。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent
ALLOWLIST_PATH = ROOT / "config" / "system_allowlist.yaml"

MAX_ENTRIES = 500
MAX_NAME_LEN = 120
MAX_TOKEN_COUNT = 4
"""一条白名单最多 4 个 token 前缀（如 'kubectl get node'），避免有人写整条命令。"""

# name 里允许出现的字符：命令名与常见参数值。刻意不含 ; & | $ ` > < ( ) 引号 反斜杠，
# 这些是 shell 元字符，出现在“命令前缀”里没有任何正当用途。
ALLOWED_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.:/=+@*?[]%"
)


class AllowlistError(ValueError):
    """白名单条目不合法（由 API 层转成 400）。"""


@dataclass(frozen=True)
class AllowEntry:
    """一条自定义白名单。``name`` 是命令前缀，``denied`` 是该命令仍禁止的 flag。"""

    name: str
    denied: tuple[str, ...] = ()
    note: str = ""
    added_at: str = ""

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self.name.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "denied": list(self.denied),
            "note": self.note,
            "added_at": self.added_at,
        }


@dataclass
class _Snapshot:
    """一次解析结果 + 用于判断文件是否变化的指纹。"""

    entries: tuple[AllowEntry, ...] = ()
    warnings: list[str] = field(default_factory=list)
    signature: tuple[int, int] | None = None


_lock = threading.Lock()
_cache = _Snapshot()
_override_path: Path | None = None


def set_path(path: str | os.PathLike[str] | None) -> None:
    """覆盖白名单文件路径（测试用）；传 None 恢复默认的 config/system_allowlist.yaml。"""
    global _override_path
    with _lock:
        _override_path = None if path is None else Path(path)
    invalidate()


def current_path() -> Path:
    """当前使用的白名单文件路径。"""
    return _override_path or ALLOWLIST_PATH



# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def normalize_name(raw: str) -> str:
    """校验并规范化命令前缀；不合法时抛 AllowlistError。"""
    if not isinstance(raw, str):
        raise AllowlistError("Command name must be a string.")
    name = " ".join(raw.split())
    if not name:
        raise AllowlistError("Command name cannot be empty.")
    if len(name) > MAX_NAME_LEN:
        raise AllowlistError(f"Command name is too long (max {MAX_NAME_LEN} characters).")
    bad = sorted({
        ch for ch in name
        if ch != " " and not ch.isalnum() and ch not in ALLOWED_NAME_CHARS
    })
    if bad:
        raise AllowlistError(
            "Command name may not contain shell metacharacters or spaces inside a token: "
            + " ".join(repr(ch) for ch in bad)
        )
    tokens = name.split(" ")
    if len(tokens) > MAX_TOKEN_COUNT:
        raise AllowlistError(
            f"Command prefix has too many tokens (max {MAX_TOKEN_COUNT}); "
            "register a prefix, not a full command line."
        )
    return name


def normalize_denied(raw: Iterable[str] | str | None) -> tuple[str, ...]:
    """校验 flag 列表（支持列表或逗号分隔字符串）。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = [item for item in raw.split(",")]
    else:
        items = list(raw)
    denied: list[str] = []
    for item in items:
        flag = str(item).strip()
        if not flag:
            continue
        if not flag.startswith("-"):
            raise AllowlistError(f"Forbidden flag must start with '-': {flag!r}")
        if len(flag) > 40:
            raise AllowlistError(f"Forbidden flag is too long: {flag!r}")
        if flag not in denied:
            denied.append(flag)
    return tuple(denied)


def _clean_note(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:200]


# ---------------------------------------------------------------------------
# 读盘 / 缓存
# ---------------------------------------------------------------------------
def _signature() -> tuple[int, int] | None:
    try:
        stat = current_path().stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _parse(raw: Any) -> tuple[list[AllowEntry], list[str]]:
    """把 YAML 内容解析成条目；坏条目跳过并收集警告（不因一条坏数据让整个网关不可用）。"""
    entries: list[AllowEntry] = []
    warnings: list[str] = []
    if raw is None:
        return entries, warnings
    if not isinstance(raw, dict):
        return entries, ["system_allowlist.yaml is not a mapping; ignored."]
    commands = raw.get("commands") or []
    if not isinstance(commands, list):
        return entries, ["'commands' must be a list; ignored."]

    seen: dict[str, AllowEntry] = {}
    for index, item in enumerate(commands):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            warnings.append(f"entry #{index + 1} is not a mapping; skipped.")
            continue
        try:
            name = normalize_name(str(item.get("name", "")))
            denied = normalize_denied(item.get("denied"))
        except AllowlistError as exc:
            warnings.append(f"entry #{index + 1} skipped: {exc}")
            continue
        try:
            blocked = is_blocked_name(name)
        except Exception:  # 循环导入等异常时不影响读取
            blocked = None
        if blocked:
            warnings.append(f"entry #{index + 1} skipped: {blocked}")
            continue
        entry = AllowEntry(
            name=name,
            denied=denied,
            note=_clean_note(item.get("note")),
            added_at=str(item.get("added_at", "") or ""),
        )
        if name in seen:
            warnings.append(f"duplicate entry '{name}' ignored.")
            continue
        seen[name] = entry
        entries.append(entry)
    if len(entries) >= MAX_ENTRIES:
        warnings.append(f"allowlist reached the {MAX_ENTRIES}-entry limit; further entries are kept but may be trimmed.")
    return entries, warnings


def _read_entries() -> tuple[list[AllowEntry], list[str]]:
    path = current_path()
    if not path.exists():
        return [], []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [], [f"cannot read {path.name}: {type(exc).__name__}: {exc}"]
    return _parse(raw)


def current() -> _Snapshot:
    """返回当前白名单快照；文件变了才重新解析（热加载）。"""
    global _cache
    signature = _signature()
    with _lock:
        if _cache.signature == signature:
            return _cache
        entries, warnings = _read_entries()
        _cache = _Snapshot(entries=tuple(entries), warnings=warnings, signature=signature)
        return _cache


def load_allowlist() -> list[AllowEntry]:
    """当前生效的白名单条目（供校验与页面展示）。"""
    return list(current().entries)


def warnings() -> list[str]:
    return list(current().warnings)


def invalidate() -> None:
    """强制下次读取重新解析（写盘后调用）。"""
    global _cache
    with _lock:
        _cache = _Snapshot()


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def _write(entries: list[AllowEntry]) -> None:
    """原子写入：先写临时文件再 os.replace，避免读到半个文件。"""
    path = current_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "commands": [
            {k: v for k, v in entry.to_dict().items() if v not in ((), [], "")}
            for entry in entries
        ],
    }
    text = (
        "# 自定义系统命令白名单 —— 由 gateway 仪表盘维护，也可手工编辑。\n"
        "# 这里的命令会追加到 system_guard.py 的内置白名单之后；黑名单与通用安全规则仍然优先。\n"
        "# 每条：name（命令前缀，必填）、denied（可选，禁用 flag 列表）、note、added_at。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    )
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise AllowlistError(
            f"Cannot write {path.name}: {type(exc).__name__}: {exc}. "
            "Check that the gateway process owns the config directory and has write permission."
        ) from exc
    invalidate()


def is_blocked_name(name: str) -> str | None:
    """拒绝把黑名单命令登记进自定义白名单；返回原因，可登记时返回 None。

    两种情形都拒绝：
      1. name 是某个黑名单命令的前缀 —— 例如登记 ``docker`` 会让 ``docker rm -f x``
         这类黑名单命令进入自定义白名单分支；
      2. name 的第一个 token 本身就是单 token 黑名单命令 —— 例如 ``rm -rf /``。
    """
    tokens = tuple(name.split())
    if not tokens:
        return None
    for spec in _denied_specs():
        if tokens == spec.name or spec.name[: len(tokens)] == tokens:
            return (
                f"'{name}' would unlock blocked command '{' '.join(spec.name)}'. "
                "Blocked commands cannot be allowlisted."
            )
        if (tokens[0],) == spec.name:
            return (
                f"'{name}' starts with blocked command '{' '.join(spec.name)}'. "
                "Blocked commands cannot be allowlisted."
            )
    return None


def _denied_specs() -> set:
    """延迟导入，避免与 system_guard 循环依赖。"""
    from system_guard import DENIED_CMDS

    return DENIED_CMDS


def add_entry(name: str, denied: Iterable[str] | str | None = None, note: str = "") -> list[AllowEntry]:
    """新增或覆盖一条白名单（同名视为更新）；返回最新条目列表。"""
    norm_name = normalize_name(name)
    norm_denied = normalize_denied(denied)
    blocked = is_blocked_name(norm_name)
    if blocked:
        raise AllowlistError(blocked)
    entries = load_allowlist()
    exists = any(entry.name == norm_name for entry in entries)
    if not exists and len(entries) >= MAX_ENTRIES:
        raise AllowlistError(f"Allowlist is full ({MAX_ENTRIES} entries); delete some first.")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = AllowEntry(
        name=norm_name,
        denied=norm_denied,
        note=_clean_note(note),
        added_at=timestamp,
    )
    merged: list[AllowEntry] = []
    replaced = False
    for entry in entries:
        if entry.name == norm_name:
            merged.append(updated)
            replaced = True
        else:
            merged.append(entry)
    if not replaced:
        merged.append(updated)
    _write(merged)
    return merged


def remove_entry(name: str) -> list[AllowEntry]:
    """删除一条白名单；不存在时抛 AllowlistError。"""
    norm_name = normalize_name(name)
    entries = load_allowlist()
    remaining = [entry for entry in entries if entry.name != norm_name]
    if len(remaining) == len(entries):
        raise AllowlistError(f"Entry not found: {norm_name}")
    _write(remaining)
    return remaining
