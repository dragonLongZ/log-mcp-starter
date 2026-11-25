"""config_store（SQLite 存储层）单元测试。

覆盖：schema 幂等、资产增删改查、坏行降级为 warning、旧 YAML 迁移幂等、
导入/导出往返、以及「无白名单条目时不再依赖 YAML 文件」。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

import config_store


def make_target(**overrides):
    base = {
        "description": "订单服务",
        "ssh_username": "user#root#10.0.0.11",
        "modules": {"order-api": {"log_dir": "/data/apps/order-api/logs", "description": "订单"}},
    }
    base.update(overrides)
    return base


class TestSchema:
    def test_init_is_idempotent(self):
        config_store.init_db()
        config_store.init_db()
        with config_store.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == config_store.SCHEMA_VERSION

    def test_tables_exist(self):
        config_store.init_db()
        with config_store.connect() as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert {"targets", "target_modules", "allowlist_entries", "audit_log", "meta"} <= names


class TestConfigShape:
    def test_load_config_keeps_legacy_shape(self):
        """load_config() 的输出必须与旧 log_targets.yaml 结构一致，调用方零改动。"""
        config_store.upsert_target("srv-a", make_target())
        config = config_store.load_config()
        assert set(config) >= {"jumpserver", "targets", "log_timezone"}
        target = config["targets"]["srv-a"]
        assert target["ssh_username"] == "user#root#10.0.0.11"
        assert target["modules"]["order-api"]["log_dir"] == "/data/apps/order-api/logs"
        # 顶层可以直接 yaml.safe_dump，说明结构仍是纯数据
        yaml.safe_dump(config["targets"])

    def test_k8s_target_roundtrip(self):
        config_store.upsert_target("k8s-a", {
            "description": "集群",
            "kind": "k8s",
            "ssh_username": "user#root#10.0.0.12",
            "modules": {},
            "k8s": {"context": "dev", "namespaces": ["default", "kube-system"]},
        })
        target = config_store.load_config()["targets"]["k8s-a"]
        assert target["kind"] == "k8s"
        assert target["k8s"]["context"] == "dev"
        assert target["k8s"]["namespaces"] == ["default", "kube-system"]

    def test_disabled_target_disappears_from_config(self):
        config_store.upsert_target("srv-a", make_target())
        config_store.set_enabled("srv-a", False)
        assert "srv-a" not in config_store.load_config()["targets"]
        assert config_store.get_target("srv-a")["enabled"] is False


class TestValidation:
    @pytest.mark.parametrize("bad_name", ["", "has space", "semi;colon", "sl/ash", "a" * 200])
    def test_rejects_bad_names(self, bad_name):
        with pytest.raises(config_store.ConfigError):
            config_store.upsert_target(bad_name, make_target())

    def test_rejects_relative_log_dir(self):
        with pytest.raises(config_store.ConfigError, match="绝对路径"):
            config_store.upsert_target("srv-a", make_target(
                modules={"m": {"log_dir": "data/logs"}}
            ))

    def test_rejects_dotdot_log_dir(self):
        with pytest.raises(config_store.ConfigError, match="\\.\\."):
            config_store.upsert_target("srv-a", make_target(
                modules={"m": {"log_dir": "/data/logs/../../../etc"}}
            ))

    def test_rejects_whitespace_in_ssh_username(self):
        with pytest.raises(config_store.ConfigError):
            config_store.upsert_target("srv-a", make_target(ssh_username="user root"))

    def test_allows_hash_in_ssh_username(self):
        """JumpServer 的用户名格式是 user#role#ip，# 必须放行。"""
        entry = config_store.validate_target("srv-a", make_target(
            ssh_username="zhangxiaolong#readonly#172.26.73.39"
        ))
        assert entry["ssh_username"] == "zhangxiaolong#readonly#172.26.73.39"

    def test_linux_target_may_have_no_modules(self):
        """现网存在只做系统查询的资产（无模块），迁移时不能被丢弃。"""
        entry = config_store.upsert_target("srv-a", make_target(modules={}))
        assert entry["modules"] == {}
        assert "srv-a" in config_store.load_config()["targets"]

    def test_rejects_unknown_kind(self):
        with pytest.raises(config_store.ConfigError, match="kind"):
            config_store.upsert_target("srv-a", make_target(kind="windows"))


class TestCrud:
    def test_upsert_then_delete(self):
        config_store.upsert_target("srv-a", make_target())
        assert "srv-a" in config_store.load_config()["targets"]
        config_store.delete_target("srv-a")
        assert config_store.load_config()["targets"] == {}

    def test_upsert_replaces_modules(self):
        config_store.upsert_target("srv-a", make_target())
        config_store.upsert_target("srv-a", make_target(
            modules={"other": {"log_dir": "/data/other", "description": ""}}
        ))
        target = config_store.load_config()["targets"]["srv-a"]
        assert list(target["modules"]) == ["other"]

    def test_delete_missing_raises(self):
        with pytest.raises(config_store.ConfigError):
            config_store.delete_target("nope")

    def test_audit_records_changes(self):
        config_store.upsert_target("srv-a", make_target())
        config_store.delete_target("srv-a")
        actions = [row["action"] for row in config_store.recent_audit()]
        assert "create" in actions and "delete" in actions


class TestBadRowTolerance:
    def test_broken_row_is_skipped_not_raised(self):
        """一条脏数据不能让整个网关起不来——旧版是直接 raise 的。"""
        config_store.upsert_target("good", make_target())
        config_store.init_db()
        with config_store.connect() as conn:
            # 绕过校验直接插一条非法资产（模拟手工改库 / 旧数据）
            conn.execute(
                "INSERT INTO targets (name, description, env, kind, ssh_username, "
                "k8s_context, k8s_namespaces, enabled, created_at, updated_at) "
                "VALUES ('bad', '', '', 'linux', '  ', '', '[]', 1, '', '')"
            )
        targets, warnings = config_store.load_targets()
        assert "good" in targets
        assert "bad" not in targets
        assert any("bad" in warning for warning in warnings)


class TestLegacyMigration:
    def test_imports_targets_and_renames_file(self):
        legacy = config_store.legacy_targets_path()
        legacy.write_text(
            yaml.safe_dump({"targets": {"old-a": make_target()}}, allow_unicode=True),
            encoding="utf-8",
        )
        config_store.init_db()
        assert "old-a" in config_store.load_config()["targets"]
        assert not legacy.exists()
        assert legacy.with_name(legacy.name + ".migrated").exists()

    def test_migration_is_idempotent(self):
        legacy = config_store.legacy_targets_path()
        legacy.write_text(
            yaml.safe_dump({"targets": {"old-a": make_target()}}, allow_unicode=True),
            encoding="utf-8",
        )
        config_store.init_db()
        config_store.upsert_target("old-a", make_target(description="人工改过"))
        # 再跑一次 init_db 不应覆盖人工修改
        config_store.init_db()
        assert config_store.load_config()["targets"]["old-a"]["description"] == "人工改过"

    def test_missing_legacy_file_is_fine(self):
        config_store.init_db()
        assert config_store.load_config()["targets"] == {}


class TestImportExport:
    def test_export_then_import_roundtrip(self):
        config_store.upsert_target("srv-a", make_target())
        text = config_store.export_config_text()
        config_store.delete_target("srv-a")
        assert config_store.load_config()["targets"] == {}

        result = config_store.import_targets(text)
        assert result["accepted"] == ["srv-a"]
        assert "srv-a" in config_store.load_config()["targets"]

    def test_dry_run_does_not_write(self):
        result = config_store.import_targets(
            yaml.safe_dump({"targets": {"srv-a": make_target()}}, allow_unicode=True),
            dry_run=True,
        )
        assert result["accepted"] == ["srv-a"]
        assert config_store.load_config()["targets"] == {}

    def test_rejected_entries_are_reported(self):
        content = """
targets:
  ok:
    description: "ok"
    ssh_username: "user#root#10.0.0.1"
    modules:
      m:
        log_dir: "/data/m"
  bad:
    description: "no username"
"""
        result = config_store.import_targets(content)
        assert result["accepted"] == ["ok"]
        assert [item["name"] for item in result["rejected"]] == ["bad"]

    def test_invalid_yaml_is_400(self):
        with pytest.raises(config_store.ConfigError):
            config_store.import_targets("targets: [unclosed")


class TestPathOverrides:
    """回归：set_paths 只改显式传入的项，否则测试会把仓库真实配置迁移掉。"""

    def test_partial_override_keeps_other_paths(self):
        config_store.set_paths(settings="/tmp/a.yaml", db="/tmp/a.db", legacy="/tmp/a-legacy.yaml")
        config_store.set_paths(db="/tmp/b.db")
        assert config_store.db_path() == Path("/tmp/b.db")
        # 其余两项必须保持上一次的设置，不能被重置回仓库默认值
        assert config_store.settings_path() == Path("/tmp/a.yaml")
        assert config_store.legacy_targets_path() == Path("/tmp/a-legacy.yaml")

    def test_explicit_none_restores_default(self):
        config_store.set_paths(settings="/tmp/a.yaml")
        config_store.set_paths(settings=None)
        assert config_store.settings_path() == config_store.SETTINGS_PATH

    def test_legacy_default_never_points_inside_test_run(self):
        """测试期间 legacy 路径必须已被 conftest 指到临时目录。"""
        assert config_store.legacy_targets_path() != config_store.LEGACY_TARGETS_PATH
        assert config_store.legacy_allowlist_path() != config_store.LEGACY_ALLOWLIST_PATH


class TestLegacyAllowlistMigration:
    def test_imports_entries_and_renames_file(self):
        legacy = config_store.legacy_allowlist_path()
        legacy.write_text(
            yaml.safe_dump({"version": 1, "commands": [
                {"name": "htop", "note": "进程"},
                {"name": "ncdu", "denied": ["-o"]},
            ]}, allow_unicode=True),
            encoding="utf-8",
        )
        config_store.init_db()
        rows = config_store.load_allowlist_rows()
        assert {row["name"] for row in rows} == {"htop", "ncdu"}
        assert not legacy.exists()
        assert legacy.with_name(legacy.name + ".migrated").exists()

    def test_missing_file_is_fine(self):
        config_store.init_db()
        assert config_store.load_allowlist_rows() == []


class TestSettings:
    @staticmethod
    def _drop_settings() -> None:
        """模拟「老部署 pull 完新代码」：settings.yaml 不存在。

        conftest 会为每个用例预置一份可用的 settings.yaml，这里显式删掉才能
        走到「自动从旧配置生成」的分支。
        """
        config_store.settings_path().unlink(missing_ok=True)

    def test_missing_settings_gives_copy_hint(self):
        """settings.yaml 不进版本库，首次使用必须先复制示例：报错要能指导这一步。"""
        config_store.set_paths(settings=config_store.db_path().parent / "nope.yaml")
        with pytest.raises(RuntimeError) as excinfo:
            config_store.load_settings()
        message = str(excinfo.value)
        assert "settings.example.yaml" in message
        assert "cp " in message
        assert "jumpserver.host" in message

    # ── 升级路径回归：老部署 pull 新代码后 settings.yaml 不存在 ──
    def test_bootstrap_from_legacy_targets(self):
        """旧 log_targets.yaml 还在时，应自动生成 settings.yaml 而不是崩掉。"""
        self._drop_settings()
        legacy = config_store.legacy_targets_path()
        legacy.write_text(
            yaml.safe_dump({
                "jumpserver": {"host": "jump.internal", "port": 2222,
                               "bind_host": "0.0.0.0", "bind_port": 8765,
                               "auto_connect": [], "keepalive_interval_minutes": 25},
                "mcp_server": {"bind_host": "0.0.0.0", "bind_port": 8000},
                "log_timezone": "Asia/Shanghai",
                "targets": {"srv-a": make_target()},
            }, allow_unicode=True),
            encoding="utf-8",
        )
        settings = config_store.load_settings()
        assert settings["jumpserver"]["host"] == "jump.internal"
        assert settings["jumpserver"]["bind_host"] == "0.0.0.0"
        assert settings["mcp_server"]["bind_port"] == 8000
        assert config_store.settings_path().exists()
        # targets 段不应被抄进 settings.yaml（资产走 DB 迁移）
        written = yaml.safe_load(config_store.settings_path().read_text(encoding="utf-8"))
        assert "targets" not in written

    def test_bootstrap_from_migrated_file(self):
        """迁移后原文件被重命名为 .migrated，也要能找到。"""
        self._drop_settings()
        legacy = config_store.legacy_targets_path()
        migrated = legacy.with_name(legacy.name + ".migrated")
        migrated.write_text(
            yaml.safe_dump({"jumpserver": {"host": "from-migrated"}}, allow_unicode=True),
            encoding="utf-8",
        )
        settings = config_store.load_settings()
        assert settings["jumpserver"]["host"] == "from-migrated"

    def test_bootstrap_writes_posix_db_path(self):
        """生成的 db_path 用正斜杠，配置可能被复制到 Linux。"""
        self._drop_settings()
        legacy = config_store.legacy_targets_path()
        legacy.write_text(
            yaml.safe_dump({"jumpserver": {"host": "h"}}, allow_unicode=True), encoding="utf-8"
        )
        config_store.load_settings()
        text = config_store.settings_path().read_text(encoding="utf-8")
        assert "data/logmcp.db" in text
        assert "data\\logmcp.db" not in text

    def test_bootstrap_ignores_unusable_legacy(self):
        """旧文件坏了就正常报错，不要写出半个 settings.yaml。"""
        self._drop_settings()
        legacy = config_store.legacy_targets_path()
        legacy.write_text("not: [valid", encoding="utf-8")
        with pytest.raises(RuntimeError, match="settings.example.yaml"):
            config_store.load_settings()
        assert not config_store.settings_path().exists()

    def test_existing_settings_wins_over_legacy(self):
        """已有 settings.yaml 时绝不能被旧文件覆盖。"""
        config_store.legacy_targets_path().write_text(
            yaml.safe_dump({"jumpserver": {"host": "legacy-value"}}, allow_unicode=True),
            encoding="utf-8",
        )
        # 不删除 conftest 预置的 settings.yaml，验证它没被覆盖
        assert config_store.load_settings()["jumpserver"]["host"] != "legacy-value"

    def test_example_settings_is_valid_and_has_no_internal_host(self):
        """示例文件必须能直接跑，且不能泄露真实内网域名。"""
        example = config_store.EXAMPLE_SETTINGS_PATH
        raw = yaml.safe_load(example.read_text(encoding="utf-8"))
        assert "jumpserver" in raw and raw["jumpserver"]["host"]
        assert "." in raw["jumpserver"]["host"]
        text = example.read_text(encoding="utf-8")
        for leaked in ("cheyifu", "10.12.0.101", "172.26."):
            assert leaked not in text, f"示例文件不应包含内网标识：{leaked}"
        # 复制成 settings.yaml 后应当能被正常加载
        import shutil

        target = config_store.db_path().parent / "settings.yaml"
        shutil.copy(example, target)
        config_store.set_paths(settings=target)
        loaded = config_store.load_settings()
        assert loaded["jumpserver"]["keepalive_interval_minutes"] == 25

    def test_keepalive_out_of_range_is_rejected_by_gateway(self):
        import gateway_host

        settings_path = config_store.settings_path()
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        settings["jumpserver"]["keepalive_interval_minutes"] = 999
        settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        with pytest.raises(RuntimeError, match="keepalive"):
            gateway_host.load_config()
