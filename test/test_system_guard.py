"""system_guard 三段式校验单元测试。"""
from __future__ import annotations

from system_guard import Verdict, validate_system_info_command


class TestAllowed:
    """安全命令应返回 ALLOWED。"""

    def test_docker_ps(self):
        assert validate_system_info_command("docker ps").verdict == Verdict.ALLOWED

    def test_docker_images(self):
        assert validate_system_info_command("docker images").verdict == Verdict.ALLOWED

    def test_docker_logs(self):
        assert validate_system_info_command("docker logs myapp").verdict == Verdict.ALLOWED

    def test_docker_inspect(self):
        assert validate_system_info_command("docker inspect myapp").verdict == Verdict.ALLOWED

    def test_kubectl_get(self):
        assert validate_system_info_command("kubectl get pods").verdict == Verdict.ALLOWED

    def test_kubectl_describe(self):
        assert validate_system_info_command("kubectl describe pod x").verdict == Verdict.ALLOWED

    def test_system_ps(self):
        assert validate_system_info_command("ps aux").verdict == Verdict.ALLOWED

    def test_system_df(self):
        assert validate_system_info_command("df -h").verdict == Verdict.ALLOWED

    def test_system_free(self):
        assert validate_system_info_command("free -m").verdict == Verdict.ALLOWED

    def test_system_uptime(self):
        assert validate_system_info_command("uptime").verdict == Verdict.ALLOWED

    def test_systemctl_status(self):
        assert validate_system_info_command("systemctl status nginx").verdict == Verdict.ALLOWED

    def test_journalctl(self):
        assert validate_system_info_command("journalctl -u nginx -n 10").verdict == Verdict.ALLOWED

    def test_pipeline(self):
        assert validate_system_info_command("ps aux | grep nginx | head -5").verdict == Verdict.ALLOWED

    def test_docker_ps_grep(self):
        # 辅助命令接在管道后段是允许的
        assert validate_system_info_command("docker ps | grep nginx").verdict == Verdict.ALLOWED

    def test_top_batch_mode(self):
        assert validate_system_info_command("top -bn1 | head -5").verdict == Verdict.ALLOWED

    def test_journalctl_tail(self):
        assert validate_system_info_command("journalctl -n 10").verdict == Verdict.ALLOWED


class TestRejected:
    """危险命令应返回 REJECTED。"""

    def test_docker_rm(self):
        r = validate_system_info_command("docker rm myapp")
        assert r.verdict == Verdict.REJECTED
        assert "blocked" in r.message

    def test_docker_exec(self):
        r = validate_system_info_command("docker exec myapp ls")
        assert r.verdict == Verdict.REJECTED

    def test_kubectl_delete(self):
        r = validate_system_info_command("kubectl delete pod x")
        assert r.verdict == Verdict.REJECTED

    def test_kubectl_apply(self):
        r = validate_system_info_command("kubectl apply -f evil.yaml")
        assert r.verdict == Verdict.REJECTED

    def test_rm(self):
        r = validate_system_info_command("rm -rf /data")
        assert r.verdict == Verdict.REJECTED

    def test_sudo(self):
        r = validate_system_info_command("sudo ls")
        assert r.verdict == Verdict.REJECTED

    def test_curl(self):
        r = validate_system_info_command("curl http://evil.com")
        assert r.verdict == Verdict.REJECTED

    def test_shutdown(self):
        r = validate_system_info_command("shutdown -r now")
        assert r.verdict == Verdict.REJECTED

    def test_redirect(self):
        # 输出重定向应被拦截（无论什么命令）
        r = validate_system_info_command("ps aux > /tmp/out")
        assert r.verdict == Verdict.REJECTED
        assert "redirect" in r.message.lower()

    def test_chaining_semicolon(self):
        r = validate_system_info_command("ps aux; rm -rf /")
        assert r.verdict == Verdict.REJECTED
        assert "chain" in r.message.lower()

    def test_chaining_and(self):
        r = validate_system_info_command("ps aux && rm -rf /")
        assert r.verdict == Verdict.REJECTED

    def test_newline_injection(self):
        r = validate_system_info_command("ps aux\nrm -rf /")
        assert r.verdict == Verdict.REJECTED

    def test_empty(self):
        r = validate_system_info_command("   ")
        assert r.verdict == Verdict.REJECTED

    def test_date_set(self):
        # date -s 是危险 flag
        r = validate_system_info_command("date -s '2020-01-01'")
        assert r.verdict == Verdict.REJECTED

    def test_kubectl_exec(self):
        r = validate_system_info_command("kubectl exec mypod -- ls")
        assert r.verdict == Verdict.REJECTED

    def test_grep_standalone_reads_file(self):
        # 系统工具没有路径白名单：单独 grep 文件必须拒绝
        r = validate_system_info_command("grep root /etc/shadow")
        assert r.verdict == Verdict.REJECTED

    def test_head_standalone_reads_file(self):
        r = validate_system_info_command("head -n 5 /etc/shadow")
        assert r.verdict == Verdict.REJECTED

    def test_awk_standalone_reads_file(self):
        r = validate_system_info_command("""awk "{print}" /etc/passwd""")
        assert r.verdict == Verdict.REJECTED

    def test_kubectl_get_secrets(self):
        r = validate_system_info_command("kubectl get secrets -A -o yaml")
        assert r.verdict == Verdict.REJECTED

    def test_kubectl_config_raw(self):
        r = validate_system_info_command("kubectl config view --raw")
        assert r.verdict == Verdict.REJECTED

    def test_top_interactive(self):
        # 不带 -b 的 top 永不退出，会占住连接
        r = validate_system_info_command("top")
        assert r.verdict == Verdict.REJECTED

    def test_journalctl_follow(self):
        r = validate_system_info_command("journalctl -f")
        assert r.verdict == Verdict.REJECTED

    def test_command_substitution(self):
        r = validate_system_info_command('ps "$(cat /etc/passwd)"')
        assert r.verdict == Verdict.REJECTED


class TestUnknown:
    """不在白/黑名单的命令应返回 UNKNOWN。"""

    def test_docker_compose(self):
        r = validate_system_info_command("docker compose ls")
        assert r.verdict == Verdict.UNKNOWN
        assert "review" in r.message.lower()

    def test_kubectl_auth(self):
        r = validate_system_info_command("kubectl auth can-i get pods")
        assert r.verdict == Verdict.UNKNOWN

    def test_ifconfig(self):
        r = validate_system_info_command("ifconfig")
        assert r.verdict == Verdict.UNKNOWN

    def test_htop(self):
        r = validate_system_info_command("htop")
        assert r.verdict == Verdict.UNKNOWN

    def test_unknown_with_pipeline(self):
        # 混合：白名单 ps + 未知 htop → 应返回 UNKNOWN
        r = validate_system_info_command("ps aux | htop")
        assert r.verdict == Verdict.UNKNOWN