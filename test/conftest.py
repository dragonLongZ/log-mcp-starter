"""测试全局隔离：把运行时数据库与部署设置指向临时目录。

关键点：资产与自定义白名单现在都存在 SQLite 里，若不加隔离，跑测试会
写坏（甚至迁移掉）仓库里的 ``config/settings.yaml`` 与 ``data/logmcp.db``。
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
import yaml

import config_store

TMP_ROOT = Path(tempfile.gettempdir())


def _write_settings(path: Path, *, write_enabled: bool = True) -> None:
    path.write_text(
        yaml.safe_dump({
            "jumpserver": {
                "host": "jumpserver.test.invalid",
                "port": 2222,
                "bind_host": "127.0.0.1",
                "bind_port": 8765,
                "auto_connect": [],
                "keepalive_interval_minutes": 25,
            },
            "mcp_server": {"bind_host": "127.0.0.1", "bind_port": 8000},
            "log_timezone": "Asia/Shanghai",
            "storage": {"db_path": str(path.parent / "logmcp.db")},
            "dashboard": {"write_enabled": write_enabled},
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def isolated_store():
    """每个用例一套独立的 settings + 空数据库。"""
    workdir = TMP_ROOT / f"log_mcp_test_{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    settings = workdir / "settings.yaml"
    _write_settings(settings)
    db = workdir / "logmcp.db"
    config_store.set_paths(
        settings=settings,
        db=db,
        legacy=workdir / "legacy.yaml",
        legacy_allowlist=workdir / "legacy_allowlist.yaml",
    )
    try:
        yield workdir
    finally:
        config_store.set_paths(
            settings=None, db=None, legacy=None, legacy_allowlist=None
        )


@pytest.fixture()
def write_disabled():
    """把当前用例的 settings 改成禁止网页写操作。"""
    settings = config_store.settings_path()
    _write_settings(settings, write_enabled=False)
    yield settings
