"""运行期配置存储 —— SQLite 单文件数据库。

设计边界（重要）：

* ``config/settings.yaml`` 保留**部署设置**（JumpServer 地址/端口、监听地址、
  心跳间隔、日志时区、MCP 端口）。这些是"改一次要 review"的常量，不进数据库，
  因此 ``mcp_server.py`` 完全不需要碰 DB。
* ``data/logmcp.db`` 存**运行期数据**：资产、模块、自定义命令白名单、审计。
  该文件被 ``.gitignore`` 忽略，开源仓库里只留 ``config/log_targets.example.yaml``
  作为可读的示例/种子。

对外最关键的一点：``load_config()`` 返回的 dict 与旧版 ``log_targets.yaml``
**结构完全一致**，所以 ``ConnectionManager.sync_config`` /
``resolve_target_module`` / ``normalize_log_dir`` 等调用方一行都不用改。

容错原则：单条资产不合法时**跳过并记 warning**，绝不因为一条脏数据让整个
网关 500（旧版 ``load_config()`` 是直接 raise 的，这是本次一并修掉的坑）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
EXAMPLE_SETTINGS_PATH = ROOT / "config" / "settings.example.yaml"
LEGACY_TARGETS_PATH = ROOT / "config" / "log_targets.yaml"
LEGACY_ALLOWLIST_PATH = ROOT / "config" / "system_allowlist.yaml"
EXAMPLE_TARGETS_PATH = ROOT / "config" / "log_targets.example.yaml"
DB_PATH = ROOT / "data" / "logmcp.db"

SCHEMA_VERSION = 1
MAX_TARGETS = 500
MAX_MODULES_PER_TARGET = 50
MAX_NAME_LEN = 100
MAX_DESCRIPTION_LEN = 200
BUSY_TIMEOUT_MS = 5000

TARGET_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
# ssh_username 形如 user#role#ip；# 是合法字符（JumpServer 的格式），
# 只禁空白/引号/控制字符，避免把 YAML/SQL 结构注进去。
SSH_USERNAME_EXTRA = frozenset("._-#@:/+=,")
KIND_VALUES = ("linux", "k8s")

_overrides: dict[str, Path | None] = {
    "settings": None, "db": None, "legacy": None, "legacy_allowlist": None,
}
_write_lock = threading.Lock()
_schema_lock = threading.Lock()
_ready_dbs: set[str] = set()
"""已确认建好表的库（路径+mtime 指纹）。避免每次 connect 都重复执行 DDL。"""


class ConfigError(ValueError):
    """配置条目或导入内容不合法（API 层转成 400）。"""


# ---------------------------------------------------------------------------
# 路径（测试可覆盖）
# ---------------------------------------------------------------------------
_UNSET = object()


def set_paths(
    *, settings: Any = _UNSET,
    db: Any = _UNSET,
    legacy: Any = _UNSET,
    legacy_allowlist: Any = _UNSET,
) -> None:
    """覆盖 settings / db / legacy 路径（测试用）。

    只改**显式传入**的那一项，其余保持不变；显式传 ``None`` 表示该项恢复默认。
    早期版本把所有未传项都重置为默认值，导致只覆盖 db 的调用（例如
    ``system_allowlist.set_path``）把 legacy 指回仓库真实的
    ``config/log_targets.yaml``，于是下一次 init_db 会把它迁移重命名掉。
    """
    if settings is not _UNSET:
        _overrides["settings"] = None if settings is None else Path(settings)
    if db is not _UNSET:
        _overrides["db"] = None if db is None else Path(db)
    if legacy is not _UNSET:
        _overrides["legacy"] = None if legacy is None else Path(legacy)
    if legacy_allowlist is not _UNSET:
        _overrides["legacy_allowlist"] = (
            None if legacy_allowlist is None else Path(legacy_allowlist)
        )
    _ready_dbs.clear()


def settings_path() -> Path:
    return _overrides["settings"] or SETTINGS_PATH


def db_path() -> Path:
    """数据库文件位置：settings.yaml 的 ``storage.db_path`` 优先（systemd 可指向
    ``/var/lib/logmcp/logmcp.db``），其次仓库内 ``data/logmcp.db``。"""
    if _overrides["db"] is not None:
        return _overrides["db"]
    try:
        storage = (load_settings().get("storage") or {})
        configured = str(storage.get("db_path") or "").strip()
    except Exception:  # settings 缺失/损坏时回落到默认路径，由 init_db 报真正的错
        configured = ""
    if not configured:
        return DB_PATH
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else (ROOT / candidate)


def legacy_targets_path() -> Path:
    return _overrides["legacy"] or LEGACY_TARGETS_PATH


def legacy_allowlist_path() -> Path:
    return _overrides["legacy_allowlist"] or LEGACY_ALLOWLIST_PATH


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------
def _raw_connect() -> sqlite3.Connection:
    """底层连接：不触发建表，供 init_db 自身使用。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect() -> sqlite3.Connection:
    """每次都开一条短连接：省掉跨线程复用 sqlite3 连接的坑，读开销微秒级。

    顺带保证表结构已建立——首次读（gateway 启动前、或库文件被删）也不会
    因为 "no such table" 直接炸掉。
    """
    ensure_schema()
    return _raw_connect()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS targets (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT NOT NULL DEFAULT '',
    env            TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'linux',
    ssh_username   TEXT NOT NULL DEFAULT '',
    k8s_context    TEXT NOT NULL DEFAULT '',
    k8s_namespaces TEXT NOT NULL DEFAULT '[]',
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS target_modules (
    id          INTEGER PRIMARY KEY,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    log_dir     TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (target_id, name)
);

CREATE TABLE IF NOT EXISTS allowlist_entries (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    denied   TEXT NOT NULL DEFAULT '[]',
    note     TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""


def ensure_schema() -> None:
    """只建表、不迁移（可被任意读路径安全调用，幂等且不重入迁移）。"""
    if _schema_ready():
        return
    with _schema_lock:
        if _schema_ready():
            return
        with closing(_raw_connect()) as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.DatabaseError:
                pass
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _ready_dbs.add(_db_fingerprint())


def _db_fingerprint() -> str:
    """库文件路径。按路径缓存（而非 mtime）：WAL 写入会不断改变 mtime，
    否则每次读都会重跑一遍 DDL。"""
    return str(db_path().resolve())


def _schema_ready() -> bool:
    return _db_fingerprint() in _ready_dbs


def init_db() -> None:
    """建表 + 一次性导入旧 YAML（资产 + 白名单）。幂等，可反复调用。"""
    db_path().parent.mkdir(parents=True, exist_ok=True)
    ensure_schema()
    with closing(_raw_connect()) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    from_legacy_yaml()
    from_legacy_allowlist()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _audit(conn: sqlite3.Connection, action: str, target: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (at, action, target, detail) VALUES (?, ?, ?, ?)",
        (_now(), action, target, detail[:2000]),
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# 校验（写接口与读取共用同一套）
# ---------------------------------------------------------------------------
def _clean_text(raw: Any, limit: int) -> str:
    """单行文本：折叠空白并按长度截断。"""
    return " ".join(str(raw or "").split())[:limit]


def normalize_target_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ConfigError("资产名不能为空。")
    if len(name) > MAX_NAME_LEN:
        raise ConfigError(f"资产名过长（最多 {MAX_NAME_LEN} 字符）。")
    bad = sorted({ch for ch in name if ch not in TARGET_NAME_CHARS})
    if bad:
        raise ConfigError(
            "资产名只允许字母、数字与 . _ - ；发现非法字符："
            + " ".join(repr(ch) for ch in bad)
        )
    return name


def normalize_ssh_username(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ConfigError("ssh_username 不能为空。")
    if len(value) > 200:
        raise ConfigError("ssh_username 过长（最多 200 字符）。")
    bad = sorted({
        ch for ch in value
        if ch.isspace() or ch in "\"'`\\" or ord(ch) < 32
    })
    if bad:
        raise ConfigError(
            "ssh_username 不能包含空白、引号或控制字符："
            + " ".join(repr(ch) for ch in bad)
        )
    extra = [ch for ch in value if not ch.isalnum() and ch not in SSH_USERNAME_EXTRA]
    if extra:
        raise ConfigError(
            "ssh_username 含意外字符：" + " ".join(sorted({repr(ch) for ch in extra}))
        )
    return value


def normalize_log_dir(raw: Any, module: str) -> str:
    """模块日志目录：必须绝对路径、不含 ..、不含换行。"""
    value = str(raw or "").strip()
    if not value:
        raise ConfigError(f"模块 '{module}' 的 log_dir 不能为空。")
    if "\n" in value or "\r" in value:
        raise ConfigError(f"模块 '{module}' 的 log_dir 不能包含换行。")
    if not value.startswith("/"):
        raise ConfigError(f"模块 '{module}' 的 log_dir 必须是绝对路径（以 / 开头）。")
    if ".." in PurePosixPath(value).parts:
        raise ConfigError(f"模块 '{module}' 的 log_dir 不能包含 '..'。")
    return str(PurePosixPath(value))


def normalize_modules(raw: Any) -> dict[str, dict[str, str]]:
    """校验 modules 段：名称 → {log_dir, description}。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("modules 必须是「模块名 → 配置」的映射。")
    if len(raw) > MAX_MODULES_PER_TARGET:
        raise ConfigError(f"单个资产最多 {MAX_MODULES_PER_TARGET} 个模块。")
    modules: dict[str, dict[str, str]] = {}
    for module_name, module_cfg in raw.items():
        name = normalize_target_name(module_name)
        cfg = module_cfg if isinstance(module_cfg, dict) else {}
        modules[name] = {
            "log_dir": normalize_log_dir(cfg.get("log_dir"), name),
            "description": _clean_text(cfg.get("description"), MAX_DESCRIPTION_LEN),
        }
    return modules


def validate_target(name: Any, target: Any) -> dict[str, Any]:
    """把一条 target 规范化为入库结构；不合法时抛 ConfigError。"""
    target_name = normalize_target_name(name)
    if not isinstance(target, dict):
        raise ConfigError(f"资产 '{target_name}' 的配置必须是映射。")

    kind = str(target.get("kind") or "linux").strip().lower()
    if kind not in KIND_VALUES:
        raise ConfigError(f"资产 '{target_name}' 的 kind 只能是 linux 或 k8s。")

    modules = normalize_modules(target.get("modules"))
    # 允许 0 个模块：现网存在只做系统查询（execute_system_query）的资产，
    # 强制要求模块会让这类历史配置在迁移时被静默丢弃。

    k8s = target.get("k8s") if isinstance(target.get("k8s"), dict) else {}
    namespaces = k8s.get("namespaces")
    if namespaces is None:
        namespaces = []
    if not isinstance(namespaces, list):
        raise ConfigError(f"资产 '{target_name}' 的 k8s.namespaces 必须是列表。")
    cleaned_namespaces: list[str] = []
    for item in namespaces:
        value = _clean_text(item, 200)
        if value and value not in cleaned_namespaces:
            cleaned_namespaces.append(value)

    return {
        "name": target_name,
        "description": _clean_text(target.get("description"), MAX_DESCRIPTION_LEN),
        "env": _clean_text(target.get("env"), MAX_DESCRIPTION_LEN),
        "kind": kind,
        "ssh_username": normalize_ssh_username(target.get("ssh_username")),
        "k8s_context": _clean_text(k8s.get("context"), MAX_DESCRIPTION_LEN),
        "k8s_namespaces": cleaned_namespaces,
        "modules": modules,
    }


# ---------------------------------------------------------------------------
# 读：settings.yaml + DB → 旧版 dict 形状
# ---------------------------------------------------------------------------
def load_settings() -> dict[str, Any]:
    """读取部署设置；缺失时给出「复制示例文件」的可操作提示。"""
    path = settings_path()
    if not path.exists():
        raise RuntimeError(
            f"缺少部署设置文件 {path}。\n"
            "首次使用请先复制示例并按需修改：\n"
            f"    cp {EXAMPLE_SETTINGS_PATH} {path}\n"
            "至少需要 jumpserver.host；其余（bind_*、keepalive、log_timezone、storage）可沿用示例值。"
        )
    with path.open("r", encoding="utf-8") as handle:
        settings = yaml.safe_load(handle) or {}
    if not isinstance(settings, dict) or "jumpserver" not in settings:
        raise RuntimeError(
            f"部署设置文件 {path} 格式不对：顶层必须是映射且包含 jumpserver 段。"
            f"可参照 {EXAMPLE_SETTINGS_PATH}。"
        )
    return settings


def _module_rows(conn: sqlite3.Connection, target_id: int) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        "SELECT name, log_dir, description FROM target_modules WHERE target_id = ? ORDER BY id",
        (target_id,),
    ).fetchall()
    return {
        str(row["name"]): {
            "log_dir": str(row["log_dir"]),
            "description": str(row["description"]),
        }
        for row in rows
    }


def load_targets() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读出全部启用资产；单条不合法则跳过并收集 warning。"""
    warnings: list[str] = []
    targets: dict[str, dict[str, Any]] = {}
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM targets WHERE enabled = 1 ORDER BY id"
        ).fetchall()
        for row in rows:
            raw = {
                "description": row["description"],
                "env": row["env"],
                "kind": row["kind"],
                "ssh_username": row["ssh_username"],
                "modules": _module_rows(conn, int(row["id"])),
            }
            if str(row["kind"]) == "k8s":
                try:
                    namespaces = json.loads(row["k8s_namespaces"] or "[]")
                except (TypeError, ValueError):
                    namespaces = []
                    warnings.append(f"资产 '{row['name']}' 的 k8s.namespaces 不是合法 JSON，已按空列表处理。")
                raw["k8s"] = {
                    "context": str(row["k8s_context"]),
                    "namespaces": namespaces if isinstance(namespaces, list) else [],
                }
            try:
                cleaned = validate_target(row["name"], raw)
            except ConfigError as exc:
                warnings.append(f"资产 '{row['name']}' 已被跳过：{exc}")
                continue
            entry: dict[str, Any] = {
                "description": cleaned["description"],
                "env": cleaned["env"],
                "kind": cleaned["kind"],
                "ssh_username": cleaned["ssh_username"],
                "modules": cleaned["modules"],
            }
            if cleaned["kind"] == "k8s":
                entry["k8s"] = {
                    "context": cleaned["k8s_context"],
                    "namespaces": cleaned["k8s_namespaces"],
                }
            targets[cleaned["name"]] = entry
    return targets, warnings


def load_config() -> dict[str, Any]:
    """完整配置 = settings.yaml（部署设置）+ DB（资产）。

    返回结构与旧版 ``log_targets.yaml`` 完全一致，调用方无感知。
    """
    config = dict(load_settings())
    targets, warnings = load_targets()
    config["targets"] = targets
    config["_warnings"] = warnings
    return config


def warnings() -> list[str]:
    """当前读取时产生的告警（坏行被跳过等），供页面与 /health 透出。"""
    try:
        return load_targets()[1]
    except Exception as exc:  # 数据库不可读时也要给出可读提示
        return [f"无法读取配置数据库：{type(exc).__name__}: {exc}"]


# ---------------------------------------------------------------------------
# 写：资产
# ---------------------------------------------------------------------------
def _target_payload(entry: dict[str, Any]) -> list[Any]:
    return [
        entry["description"], entry["env"], entry["kind"], entry["ssh_username"],
        entry["k8s_context"], json.dumps(entry["k8s_namespaces"], ensure_ascii=False),
    ]


def _replace_modules(conn: sqlite3.Connection, target_id: int, modules: dict[str, dict[str, str]]) -> None:
    conn.execute("DELETE FROM target_modules WHERE target_id = ?", (target_id,))
    for module_name, cfg in modules.items():
        conn.execute(
            "INSERT INTO target_modules (target_id, name, log_dir, description) VALUES (?, ?, ?, ?)",
            (target_id, module_name, cfg["log_dir"], cfg["description"]),
        )


def upsert_target(name: Any, target: Any) -> dict[str, Any]:
    """新增或整条覆盖一个资产；返回规范化后的条目。"""
    entry = validate_target(name, target)
    timestamp = _now()
    detail = json.dumps(
        {"ssh_username": entry["ssh_username"], "kind": entry["kind"],
         "modules": sorted(entry["modules"])},
        ensure_ascii=False,
    )
    with _write_lock, closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM targets WHERE name = ?", (entry["name"],)
            ).fetchone()
            if existing is None:
                count = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
                if count >= MAX_TARGETS:
                    raise ConfigError(f"资产数量已达上限 {MAX_TARGETS}，请先删除部分资产。")
                cursor = conn.execute(
                    "INSERT INTO targets (name, description, env, kind, ssh_username, "
                    "k8s_context, k8s_namespaces, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (entry["name"], *_target_payload(entry), timestamp, timestamp),
                )
                target_id = int(cursor.lastrowid)
                action = "create"
            else:
                target_id = int(existing["id"])
                conn.execute(
                    "UPDATE targets SET description = ?, env = ?, kind = ?, ssh_username = ?, "
                    "k8s_context = ?, k8s_namespaces = ?, enabled = 1, updated_at = ? WHERE id = ?",
                    (*_target_payload(entry), timestamp, target_id),
                )
                action = "update"
            _replace_modules(conn, target_id, entry["modules"])
            _audit(conn, action, entry["name"], detail)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return entry


def delete_target(name: Any) -> None:
    """删除一个资产（连同其模块）。不存在时抛 ConfigError。"""
    target_name = normalize_target_name(name)
    with _write_lock, closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM targets WHERE name = ?", (target_name,)
            ).fetchone()
            if row is None:
                raise ConfigError(f"资产不存在：{target_name}")
            conn.execute("DELETE FROM targets WHERE id = ?", (int(row["id"]),))
            _audit(conn, "delete", target_name)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def set_enabled(name: Any, enabled: bool) -> dict[str, Any]:
    """启用/停用资产（停用后从连接池消失，数据保留）。"""
    target_name = normalize_target_name(name)
    with _write_lock, closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM targets WHERE name = ?", (target_name,)
            ).fetchone()
            if row is None:
                raise ConfigError(f"资产不存在：{target_name}")
            conn.execute(
                "UPDATE targets SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _now(), int(row["id"])),
            )
            _audit(conn, "enable" if enabled else "disable", target_name)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"name": target_name, "enabled": bool(enabled)}


def list_targets_meta() -> list[dict[str, Any]]:
    """页面用的资产清单（含停用条目、创建/更新时间）。"""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append({
                "name": str(row["name"]),
                "description": str(row["description"]),
                "env": str(row["env"]),
                "kind": str(row["kind"]),
                "ssh_username": str(row["ssh_username"]),
                "enabled": bool(row["enabled"]),
                "k8s": {
                    "context": str(row["k8s_context"]),
                    "namespaces": json.loads(row["k8s_namespaces"] or "[]"),
                } if str(row["kind"]) == "k8s" else None,
                "modules": _module_rows(conn, int(row["id"])),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })
        return items


def get_target(name: Any) -> dict[str, Any] | None:
    for item in list_targets_meta():
        if item["name"] == normalize_target_name(name):
            return item
    return None


# ---------------------------------------------------------------------------
# 写：自定义命令白名单（与资产同库）
# ---------------------------------------------------------------------------
def load_allowlist_rows() -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT name, denied, note, added_at FROM allowlist_entries ORDER BY id"
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            denied = json.loads(row["denied"] or "[]")
        except (TypeError, ValueError):
            denied = []
        items.append({
            "name": str(row["name"]),
            "denied": [str(value) for value in denied] if isinstance(denied, list) else [],
            "note": str(row["note"]),
            "added_at": str(row["added_at"]),
        })
    return items


def upsert_allowlist_entry(name: str, denied: Iterable[str], note: str) -> list[dict[str, Any]]:
    denied_list = [str(item) for item in denied]
    timestamp = _now()
    with _write_lock, closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO allowlist_entries (name, denied, note, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET denied = excluded.denied, note = excluded.note, "
                "added_at = excluded.added_at",
                (name, json.dumps(denied_list, ensure_ascii=False), note, timestamp),
            )
            _audit(conn, "allowlist", name, json.dumps(denied_list, ensure_ascii=False))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return load_allowlist_rows()


def delete_allowlist_entry(name: str) -> list[dict[str, Any]]:
    with _write_lock, closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM allowlist_entries WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise ConfigError(f"白名单条目不存在：{name}")
            _audit(conn, "allowlist-delete", name)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return load_allowlist_rows()


# ---------------------------------------------------------------------------
# 迁移 / 导入 / 导出
# ---------------------------------------------------------------------------
def from_legacy_yaml(force: bool = False) -> int:
    """首次启动时把旧 ``config/log_targets.yaml`` 的 targets 导入数据库。

    幂等：已导入过（meta 标记）或数据库已有资产时跳过。原文件重命名为
    ``.migrated`` 保留，不删除——开源项目改存储格式要给回滚余地。
    """
    path = legacy_targets_path()
    if not path.exists() and not force:
        return 0
    with closing(connect()) as conn:
        if _meta_get(conn, "legacy_imported") and not force:
            return 0
        imported = 0
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                raw = {}
            targets = raw.get("targets") if isinstance(raw, dict) else None
            if isinstance(targets, dict):
                for name, target in targets.items():
                    try:
                        entry = validate_target(name, target)
                    except ConfigError as exc:
                        print(f"[warn] 迁移跳过资产 '{name}'：{exc}")
                        continue
                    try:
                        upsert_target(entry["name"], entry)
                        imported += 1
                    except ConfigError as exc:
                        print(f"[warn] 迁移失败资产 '{name}'：{exc}")
        _meta_set(conn, "legacy_imported", _now())
    if path.exists():
        try:
            path.replace(path.with_name(path.name + ".migrated"))
            print(f"[info] 已把 {path.name} 重命名为 {path.name}.migrated（保留备份）。")
        except OSError as exc:
            print(f"[warn] 无法重命名 {path.name}：{exc}")
    if imported:
        print(f"[info] 已从旧 YAML 导入 {imported} 个资产到 {db_path()}")
    return imported


def from_legacy_allowlist(force: bool = False) -> int:
    """把旧 ``config/system_allowlist.yaml`` 的条目并入数据库（白名单已并库）。

    幂等：meta 标记过、或库里已有条目时跳过。原文件重命名为 ``.migrated`` 保留。
    """
    path = legacy_allowlist_path()
    if not path.exists():
        return 0
    with closing(connect()) as conn:
        if _meta_get(conn, "legacy_allowlist_imported") and not force:
            return 0
        existing = conn.execute("SELECT COUNT(*) FROM allowlist_entries").fetchone()[0]
    imported = 0
    if not existing or force:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            print(f"[warn] 无法解析 {path.name}，跳过白名单迁移：{exc}")
            raw = {}
        commands = raw.get("commands") if isinstance(raw, dict) else None
        for item in commands or []:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            denied = item.get("denied") or []
            if isinstance(denied, str):
                denied = [part for part in denied.split(",")]
            try:
                upsert_allowlist_entry(
                    name,
                    [str(value).strip() for value in denied if str(value).strip()],
                    str(item.get("note") or ""),
                )
                imported += 1
            except Exception as exc:
                print(f"[warn] 白名单条目 '{name}' 迁移失败：{exc}")
    with closing(_raw_connect()) as conn:
        _meta_set(conn, "legacy_allowlist_imported", _now())
    try:
        path.replace(path.with_name(path.name + ".migrated"))
        print(f"[info] 已把 {path.name} 重命名为 {path.name}.migrated（保留备份）。")
    except OSError as exc:
        print(f"[warn] 无法重命名 {path.name}：{exc}")
    if imported:
        print(f"[info] 已从旧 YAML 导入 {imported} 条自定义白名单")
    return imported


def import_targets(payload: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """从 YAML 文本 / dict 批量导入资产。

    ``dry_run=True`` 时只做校验，不落库（页面「试运行」用）。
    """
    if isinstance(payload, str):
        try:
            raw = yaml.safe_load(payload) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML 解析失败：{exc}") from exc
    elif isinstance(payload, dict):
        raw = payload
    else:
        raise ConfigError("导入内容必须是 YAML 文本或 JSON 对象。")

    if not isinstance(raw, dict):
        raise ConfigError("导入内容顶层必须是映射。")
    targets = raw.get("targets", raw)
    if not isinstance(targets, dict):
        raise ConfigError("缺少 targets 段，或 targets 不是映射。")

    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    for name, target in targets.items():
        if str(name).startswith("_"):
            continue
        try:
            entry = validate_target(name, target)
        except ConfigError as exc:
            rejected.append({"name": str(name), "error": str(exc)})
            continue
        accepted.append(entry["name"])
        if not dry_run:
            upsert_target(entry["name"], entry)
    return {"accepted": accepted, "rejected": rejected, "applied": not dry_run}


def export_config_text() -> str:
    """导出合并后的完整 YAML（结构与旧 log_targets.yaml 一致，可回灌）。"""
    config = load_config()
    config.pop("_warnings", None)
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def export_allowlist_text() -> str:
    payload = {"version": 1, "commands": load_allowlist_rows()}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def recent_audit(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT at, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{key: str(row[key]) for key in ("at", "action", "target", "detail")} for row in rows]
