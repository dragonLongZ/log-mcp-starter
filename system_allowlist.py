"""自定义系统命令白名单 —— 网关内置白名单的运行时扩展。

``system_guard.py`` 内置了一份系统命令白名单；不在内置白/黑名单里的命令会返回
UNKNOWN，模型只能提示用户手工执行。本模块提供一份**可在线维护**的补充白名单：

  * 存储：与资产同一个 SQLite 数据库（``config_store``，默认 ``data/logmcp.db``），
    表 ``allowlist_entries``。开源仓库只需 gitignore 一个数据库文件。
  * 即时生效：校验时直接从数据库读取（进程内按内容指纹做短缓存），
    网页上增删一条后，下一次请求立即生效，无需重启 gateway。
  * 只做“加白”：黑名单（rm/sudo/docker rm…）与通用安全规则（禁止重定向、
    串联、命令替换、awk 写原语…）始终优先，无法通过本模块绕过。

库表字段::

    name      命令前缀（按空格切成 token 逐级匹配）
    denied    该命令仍禁止的 flag / 子命令列表（JSON 数组）
    note      备注
    added_at  添加时间

``name`` 只允许字母/数字与少量符号（``- _ . / : = + @ * ?[]``），拒绝空白以外的
shell 元字符，防止有人把 ``ps; rm -rf /`` 之类整条命令塞进白名单。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import config_store

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

# “禁用参数”在 name 的字符集之外，还允许 - 前缀 flag 与 jcmd 那种 a.b.c 子命令，
# 但仍然拒绝空白与一切 shell 元字符。
DENIED_EXTRA_CHARS = ALLOWED_NAME_CHARS
MAX_DENIED_LEN = 60


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
    """一次读取结果 + 用于判断数据是否变化的指纹。"""

    entries: tuple[AllowEntry, ...] = ()
    warnings: list[str] = field(default_factory=list)
    signature: tuple | None = None


_lock = threading.Lock()
_cache = _Snapshot()
_override_db: Path | None = None


def set_path(path: str | os.PathLike[str] | None) -> None:
    """覆盖白名单所在的数据库路径（测试用）；传 None 恢复默认数据库。"""
    config_store.set_paths(db=path)
    invalidate()


def current_path() -> Path:
    """当前使用的数据库文件路径（页面展示「配置存哪里」用）。"""
    return config_store.db_path()


def _db_ready() -> Path:
    config_store.init_db()
    return config_store.db_path()


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
    """校验“仍禁止的参数”列表（支持列表或逗号分隔字符串）。

    两种形态都接受：
      * flag：``-o``、``--output``、``-f``（按 token 精确匹配，也支持 ``--flag=value``）；
      * 子命令 / 子动作：``VM.set_flag``、``GC.run``、``GC.run_finalization``
        （jcmd、systemctl 这类命令的危险动作不是 flag 而是子命令，必须能禁）。
    含 shell 元字符的值一律拒绝。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = [item for item in raw.split(",")]
    else:
        items = list(raw)
    denied: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        if len(value) > MAX_DENIED_LEN:
            raise AllowlistError(f"Forbidden parameter is too long: {value!r}")
        bad = sorted({
            ch for ch in value
            if not ch.isalnum() and ch not in DENIED_EXTRA_CHARS
        })
        if bad:
            raise AllowlistError(
                f"Invalid forbidden parameter {value!r}: may not contain "
                + " ".join(repr(ch) for ch in bad)
            )
        if value not in denied:
            denied.append(value)
    return tuple(denied)


def _clean_note(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:200]


# ---------------------------------------------------------------------------
# 读：数据库 → 条目快照（按内容指纹缓存，写盘后 invalidate）
# ---------------------------------------------------------------------------
def _build_snapshot() -> _Snapshot:
    _db_ready()
    try:
        rows = config_store.load_allowlist_rows()
    except Exception as exc:  # 数据库坏了也不能让网关整体失效
        return _Snapshot(
            entries=(),
            warnings=[f"cannot read allowlist database: {type(exc).__name__}: {exc}"],
            signature=None,
        )

    entries: list[AllowEntry] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            name = normalize_name(str(row.get("name", "")))
            denied = normalize_denied(row.get("denied"))
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
        if name in seen:
            warnings.append(f"duplicate entry '{name}' ignored.")
            continue
        seen.add(name)
        entries.append(AllowEntry(
            name=name,
            denied=denied,
            note=_clean_note(row.get("note")),
            added_at=str(row.get("added_at", "") or ""),
        ))
    if len(entries) >= MAX_ENTRIES:
        warnings.append(
            f"allowlist reached the {MAX_ENTRIES}-entry limit; further entries are kept but may be trimmed."
        )
    signature = tuple((entry.name, entry.denied, entry.note) for entry in entries)
    return _Snapshot(entries=tuple(entries), warnings=warnings, signature=signature)


def current() -> _Snapshot:
    """当前白名单快照；数据没变就复用上一次的解析结果。"""
    global _cache
    with _lock:
        snapshot = _build_snapshot()
        if _cache.signature is not None and _cache.signature == snapshot.signature:
            return _cache
        _cache = snapshot
        return _cache


def load_allowlist() -> list[AllowEntry]:
    """当前生效的白名单条目（供校验与页面展示）。"""
    return list(current().entries)


def warnings() -> list[str]:
    return list(current().warnings)


def invalidate() -> None:
    """强制下次读取重新构建快照（写盘后调用）。"""
    global _cache
    with _lock:
        _cache = _Snapshot()


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
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
    _db_ready()
    config_store.upsert_allowlist_entry(norm_name, norm_denied, _clean_note(note))
    invalidate()
    return load_allowlist()


def remove_entry(name: str) -> list[AllowEntry]:
    """删除一条白名单；不存在时抛 AllowlistError。"""
    norm_name = normalize_name(name)
    _db_ready()
    rows = config_store.load_allowlist_rows()
    if not any(str(row.get("name")) == norm_name for row in rows):
        raise AllowlistError(f"Entry not found: {norm_name}")
    config_store.delete_allowlist_entry(norm_name)
    invalidate()
    return load_allowlist()
