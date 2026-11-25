"""自定义命令白名单（system_allowlist）与 system_guard 集成测试。

白名单已并入 SQLite（与资产同库），存储位置由 test/conftest.py 的
``isolated_store`` 指向临时目录，绝不碰仓库里的 data/ 与 config/。
"""
from __future__ import annotations

import pytest

import config_store
import system_allowlist
from system_guard import Verdict, validate_system_info_command


class TestNameValidation:
    def test_plain_command(self):
        assert system_allowlist.normalize_name("htop") == "htop"

    def test_multi_token_prefix(self):
        assert system_allowlist.normalize_name("docker  compose   ls") == "docker compose ls"

    def test_rejects_shell_metacharacters(self):
        for bad in ("ps; rm -rf /", "ps && id", "ps | id", "$(id)", "`id`", "ps > /tmp/x"):
            with pytest.raises(system_allowlist.AllowlistError):
                system_allowlist.normalize_name(bad)

    def test_rejects_too_many_tokens(self):
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.normalize_name("a b c d e")

    def test_rejects_empty(self):
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.normalize_name("   ")

    def test_denied_flags_must_start_with_dash(self):
        # flag 与 jcmd 子命令两种形态都合法，但 shell 元字符不行
        assert system_allowlist.normalize_denied(["VM.set_flag"]) == ("VM.set_flag",)
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.normalize_denied(["a;b"])

    def test_denied_accepts_subcommands(self):
        """jcmd 这类命令的危险动作是子命令而非 flag，必须能登记。"""
        assert system_allowlist.normalize_denied(
            ["VM.set_flag", "GC.run", "GC.run_finalization"]
        ) == ("VM.set_flag", "GC.run", "GC.run_finalization")

    def test_denied_rejects_shell_metacharacters(self):
        for bad in ("x y", "a;b", "a|b", "a&b", "a$b", "a`b", "a>b"):
            with pytest.raises(system_allowlist.AllowlistError):
                system_allowlist.normalize_denied([bad])

    def test_denied_flags_from_comma_string(self):
        assert system_allowlist.normalize_denied("-o, --output ,") == ("-o", "--output")


class TestBlockedNames:
    """黑名单命令不能通过自定义白名单绕过。"""

    @pytest.mark.parametrize("name", ["rm", "rm -rf /", "sudo", "docker", "docker exec", "kubectl delete"])
    def test_cannot_allowlist_blocked_commands(self, name):
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.add_entry(name)

    def test_docker_compose_is_allowed(self):
        system_allowlist.add_entry("docker compose ls")
        assert validate_system_info_command("docker compose ls").verdict == Verdict.ALLOWED


class TestHotReload:
    """增删后立即生效：不需要重启、不需要手动刷新缓存。"""

    def test_add_makes_command_allowed(self):
        assert validate_system_info_command("htop").verdict == Verdict.UNKNOWN
        system_allowlist.add_entry("htop")
        result = validate_system_info_command("htop")
        assert result.verdict == Verdict.ALLOWED
        assert "custom allowlist" in (result.message or "")

    def test_remove_restores_unknown(self):
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("htop").verdict == Verdict.ALLOWED
        system_allowlist.remove_entry("htop")
        assert validate_system_info_command("htop").verdict == Verdict.UNKNOWN

    def test_pipeline_segment_allowed(self):
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("ps aux | htop").verdict == Verdict.ALLOWED

    def test_multi_token_prefix(self):
        system_allowlist.add_entry("docker compose ls")
        assert validate_system_info_command("docker compose ls --all").verdict == Verdict.ALLOWED
        # 只登记了 docker compose ls，docker compose up 仍是未知
        assert validate_system_info_command("docker compose up").verdict == Verdict.UNKNOWN

    def test_entry_update_overrides_previous(self):
        system_allowlist.add_entry("ncdu", note="first")
        entries = system_allowlist.add_entry("ncdu", denied=["-o"], note="second")
        assert len(entries) == 1
        assert entries[0].denied == ("-o",)
        assert entries[0].note == "second"


class TestRealWorldCommands:
    """真实场景：jcmd 的危险动作是子命令（VM.set_flag/GC.run），不是 flag。"""

    def test_jcmd_selective_subcommands(self):
        system_allowlist.add_entry(
            "jcmd", denied=["VM.set_flag", "GC.run", "GC.run_finalization"], note="JVM 诊断"
        )
        # 只读查询放行
        assert validate_system_info_command("jcmd 1234 VM.version").verdict == Verdict.ALLOWED
        assert validate_system_info_command("jcmd 1234 help").verdict == Verdict.ALLOWED
        assert validate_system_info_command("jcmd -l").verdict == Verdict.ALLOWED
        # 危险子命令仍拒绝
        assert validate_system_info_command("jcmd 1234 GC.run").verdict == Verdict.REJECTED
        assert validate_system_info_command("jcmd 1234 GC.run_finalization").verdict == Verdict.REJECTED
        assert validate_system_info_command(
            "jcmd 1234 VM.set_flag ManagementServer.Verbose true"
        ).verdict == Verdict.REJECTED

    def test_jcmd_without_denied_allows_everything(self):
        # 不填禁用参数时，登记即整体放行（用户自己的选择）
        system_allowlist.add_entry("jcmd")
        assert validate_system_info_command("jcmd 1234 GC.run").verdict == Verdict.ALLOWED


class TestGuardsStillApply:

    """自定义白名单不能削弱黑名单与通用安全规则。"""

    def test_denied_flag_on_custom_entry(self):
        system_allowlist.add_entry("ncdu", denied=["-o"])
        assert validate_system_info_command("ncdu").verdict == Verdict.ALLOWED
        assert validate_system_info_command("ncdu -o out.txt").verdict == Verdict.REJECTED

    def test_redirect_still_rejected(self):
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("htop > /tmp/x").verdict == Verdict.REJECTED

    def test_chaining_still_rejected(self):
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("htop && rm -rf /").verdict == Verdict.REJECTED

    def test_command_substitution_still_rejected(self):
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("htop $(id)").verdict == Verdict.REJECTED

    def test_blocked_command_with_allowlisted_prefix(self):
        # 登记 htop 不应影响 docker rm 的拦截
        system_allowlist.add_entry("htop")
        assert validate_system_info_command("docker rm -f x").verdict == Verdict.REJECTED


class TestPersistence:
    """并库后的持久化：数据在 allowlist_entries 表，进程重启（清缓存）后仍在。"""

    def test_entry_survives_cache_invalidate(self):
        system_allowlist.add_entry("htop", denied=["-d"], note="进程查看")
        system_allowlist.invalidate()
        entries = system_allowlist.load_allowlist()
        assert [entry.name for entry in entries] == ["htop"]
        assert entries[0].denied == ("-d",)
        assert entries[0].note == "进程查看"
        assert entries[0].added_at

    def test_stored_in_same_db_as_targets(self):
        """白名单与资产同库，这是「开源只 gitignore 一个文件」的前提。"""
        system_allowlist.add_entry("htop")
        rows = config_store.load_allowlist_rows()
        assert rows[0]["name"] == "htop"
        assert system_allowlist.current_path() == config_store.db_path()

    def test_missing_file_means_empty(self):
        assert system_allowlist.load_allowlist() == []

    def test_broken_entry_is_skipped_with_warning(self):
        # 绕过 API 直接往库里塞一条非法条目（模拟手工改库/旧数据）
        config_store.init_db()
        with config_store.connect() as conn:
            conn.execute(
                "INSERT INTO allowlist_entries (name, denied, note, added_at) VALUES (?, ?, '', '')",
                ("ps; rm -rf /", "[]"),
            )
        system_allowlist.invalidate()
        entries = system_allowlist.load_allowlist()
        assert entries == []
        assert any("skipped" in warning for warning in system_allowlist.warnings())

    def test_remove_missing_entry_raises(self):
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.remove_entry("htop")
