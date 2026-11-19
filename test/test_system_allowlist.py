"""自定义命令白名单（system_allowlist）与 system_guard 集成测试。

关键点：白名单文件用临时路径覆盖，绝不写项目的 config/ 目录。
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest

import system_allowlist
from system_guard import Verdict, validate_system_info_command


@pytest.fixture(autouse=True)
def temp_allowlist():
    """每个用例使用独立的临时白名单文件，结束后恢复默认路径。"""
    path = Path(tempfile.gettempdir()) / f"log_mcp_allowlist_{uuid.uuid4().hex}.yaml"
    system_allowlist.set_path(path)
    yield path
    system_allowlist.set_path(None)
    path.unlink(missing_ok=True)


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
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.normalize_denied(["output"])

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

    def test_manual_file_edit_is_picked_up(self, temp_allowlist):
        temp_allowlist.write_text(
            "version: 1\ncommands:\n  - name: 'ifconfig'\n", encoding="utf-8"
        )
        assert validate_system_info_command("ifconfig").verdict == Verdict.ALLOWED

    def test_entry_update_overrides_previous(self):
        system_allowlist.add_entry("ncdu", note="first")
        entries = system_allowlist.add_entry("ncdu", denied=["-o"], note="second")
        assert len(entries) == 1
        assert entries[0].denied == ("-o",)
        assert entries[0].note == "second"


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
    def test_written_file_is_valid_yaml(self, temp_allowlist):
        import yaml

        system_allowlist.add_entry("htop", denied=["-d"], note="进程查看")
        data = yaml.safe_load(temp_allowlist.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["commands"][0]["name"] == "htop"
        assert data["commands"][0]["denied"] == ["-d"]
        assert data["commands"][0]["note"] == "进程查看"
        assert data["commands"][0]["added_at"]

    def test_missing_file_means_empty(self, temp_allowlist):
        assert system_allowlist.load_allowlist() == []

    def test_broken_entry_is_skipped_with_warning(self, temp_allowlist):
        temp_allowlist.write_text(
            "version: 1\ncommands:\n  - name: 'htop'\n  - name: 'ps; rm -rf /'\n  - name: 'ifconfig'\n",
            encoding="utf-8",
        )
        entries = system_allowlist.load_allowlist()
        assert [entry.name for entry in entries] == ["htop", "ifconfig"]
        assert any("skipped" in warning for warning in system_allowlist.warnings())

    def test_remove_missing_entry_raises(self, temp_allowlist):
        with pytest.raises(system_allowlist.AllowlistError):
            system_allowlist.remove_entry("htop")
