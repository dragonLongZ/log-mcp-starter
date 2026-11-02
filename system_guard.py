"""系统/Docker/K8s 命令三段式校验模块 — 基于 safecmd AST 解析。

分类体系：
  ALLOWED  ─ 安全命令白名单，自动执行
  REJECTED ─ 明确危险命令或违规语法（重定向/串联/注入），自动拦截
  UNKNOWN  ─ 不在白名单也不在黑名单，提醒用户手动审核
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

from command_guard import (
    check_awk_sed,
    check_forbidden_ops,
    split_pipeline,
)
from safecmd import CmdSpec
from safecmd.bashxtract import extract_commands

MAX_SEGMENTS = 3

# 只在管道后段允许的文本处理命令：它们能直接读文件参数，而系统工具没有路径白名单。
PIPE_ONLY_CMDS = frozenset({
    "grep", "egrep", "fgrep", "head", "tail",
    "wc", "sort", "uniq", "cut", "tr", "awk",
})


class Verdict(Enum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class ValidationResult:
    verdict: Verdict
    message: str | None = None


# ---------------------------------------------------------------------------
# 安全白名单
# ---------------------------------------------------------------------------
ALLOWED_CMDS: set[CmdSpec] = {
    CmdSpec("ps"), CmdSpec("top"),
    CmdSpec("df"), CmdSpec("free"),
    CmdSpec("uptime"), CmdSpec("uname"),
    CmdSpec("who"), CmdSpec("w"), CmdSpec("last"),
    CmdSpec("id"), CmdSpec("pwd"),
    CmdSpec("date", denied={"-s", "--set"}),
    CmdSpec("netstat"), CmdSpec("ss"),
    CmdSpec("nslookup"), CmdSpec("dig"),
    CmdSpec("ip addr"), CmdSpec("ip link"),
    CmdSpec("ip route"), CmdSpec("ip neigh"),
    CmdSpec("iostat"), CmdSpec("vmstat"),
    CmdSpec("lscpu"), CmdSpec("lsblk"),
    CmdSpec("dmesg", denied={"-c", "--clear"}),
    CmdSpec("lsof"),
    CmdSpec("lspci"), CmdSpec("lsusb"),
    CmdSpec("pidof"), CmdSpec("pgrep"),
    CmdSpec("systemctl status"),
    CmdSpec("systemctl list-units"),
    CmdSpec("systemctl is-active"),
    CmdSpec("systemctl is-enabled"),
    CmdSpec("journalctl", denied={"-f", "--follow", "--flush", "--rotate", "--vacuum", "--sync", "--setup-keys"}),
    # Docker
    CmdSpec("docker ps"),
    CmdSpec("docker images"),
    CmdSpec("docker image ls"),
    CmdSpec("docker logs"),
    CmdSpec("docker inspect"),
    CmdSpec("docker stats"),
    CmdSpec("docker info"),
    CmdSpec("docker version"),
    CmdSpec("docker container ls"),
    CmdSpec("docker container inspect"),
    CmdSpec("docker container logs"),
    CmdSpec("docker network ls"),
    CmdSpec("docker network inspect"),
    CmdSpec("docker volume ls"),
    CmdSpec("docker volume inspect"),
    # Kubectl
    CmdSpec("kubectl get"),
    CmdSpec("kubectl describe"),
    CmdSpec("kubectl logs"),
    CmdSpec("kubectl top"),
    CmdSpec("kubectl api-resources"),
    CmdSpec("kubectl api-versions"),
    CmdSpec("kubectl cluster-info"),
    CmdSpec("kubectl config view"),
    CmdSpec("kubectl explain"),
    # 管道辅助（仅允许出现在 '|' 之后，见 PIPE_ONLY_CMDS）
    CmdSpec("grep"), CmdSpec("egrep"), CmdSpec("fgrep"),
    CmdSpec("head"), CmdSpec("tail"),
    CmdSpec("wc"), CmdSpec("sort"), CmdSpec("uniq"),
    CmdSpec("cut"), CmdSpec("tr"),
    CmdSpec("awk", denied={"-f"}),
}

# ---------------------------------------------------------------------------
# 危险黑名单
# ---------------------------------------------------------------------------
DENIED_CMDS: set[CmdSpec] = {
    CmdSpec("rm"), CmdSpec("mv"), CmdSpec("cp"),
    CmdSpec("dd"), CmdSpec("mkfs"), CmdSpec("mkswap"),
    CmdSpec("fdisk"), CmdSpec("parted"),
    CmdSpec("sudo"), CmdSpec("su"), CmdSpec("pkexec"),
    CmdSpec("reboot"), CmdSpec("shutdown"), CmdSpec("halt"),
    CmdSpec("poweroff"), CmdSpec("init"),
    CmdSpec("kill"), CmdSpec("pkill"), CmdSpec("killall"),
    CmdSpec("chmod"), CmdSpec("chown"), CmdSpec("chgrp"),
    CmdSpec("curl"), CmdSpec("wget"),
    CmdSpec("nc"), CmdSpec("ncat"), CmdSpec("netcat"),
    CmdSpec("ssh"), CmdSpec("scp"), CmdSpec("sftp"),
    CmdSpec("telnet"), CmdSpec("ftp"),
    CmdSpec("bash"), CmdSpec("sh"), CmdSpec("zsh"),
    CmdSpec("python"), CmdSpec("python3"), CmdSpec("perl"),
    CmdSpec("ruby"), CmdSpec("node"),
    CmdSpec("eval"), CmdSpec("exec"),
    CmdSpec("systemctl start"), CmdSpec("systemctl stop"),
    CmdSpec("systemctl restart"),
    CmdSpec("systemctl enable"), CmdSpec("systemctl disable"),
    CmdSpec("systemctl mask"), CmdSpec("systemctl unmask"),
    CmdSpec("docker rm"), CmdSpec("docker stop"),
    CmdSpec("docker kill"), CmdSpec("docker restart"),
    CmdSpec("docker exec"), CmdSpec("docker run"),
    CmdSpec("docker build"), CmdSpec("docker push"),
    CmdSpec("docker pull"), CmdSpec("docker create"),
    CmdSpec("docker start"), CmdSpec("docker update"),
    CmdSpec("kubectl delete"), CmdSpec("kubectl apply"),
    CmdSpec("kubectl create"), CmdSpec("kubectl edit"),
    CmdSpec("kubectl patch"), CmdSpec("kubectl scale"),
    CmdSpec("kubectl rollout"), CmdSpec("kubectl exec"),
    CmdSpec("kubectl cp"), CmdSpec("kubectl port-forward"),
}


def _match_spec(toks: list[str], specs: set[CmdSpec]) -> int:
    """检查命令 token 列表与 CmdSpec 集合的匹配结果。

    Returns:
        2 = 完全匹配（通过 denied 检查）→ 允许
        1 = 前缀匹配但因 denied flag 被拒绝 → 应拒绝
        0 = 完全不匹配 → 未知
    """
    for spec in specs:
        n = len(spec.name)
        if tuple(toks[:n]) != spec.name:
            continue
        if not spec.denied:
            return 2
        return 2 if spec(toks) else 1
    return 0


def _misleading_kubectl(toks: list[str]) -> str | None:
    """kubectl 只读子命令中会泄露凭据的组合。"""
    if toks[0] != "kubectl" or len(toks) < 2:
        return None
    if toks[1] in ("get", "describe") and any(
        token in ("secret", "secrets") for token in toks[2:]
    ):
        return "kubectl secret access can expose cluster credentials."
    if toks[1] == "config" and "--raw" in toks:
        return "kubectl config view --raw can expose kubeconfig credentials."
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def validate_system_info_command(command: str) -> ValidationResult:
    """三段式校验系统/Docker/K8s 命令。

    Returns:
        ValidationResult.verdict ∈ {ALLOWED, REJECTED, UNKNOWN}
    """
    # ─ 0. 空命令 / 换行 / 命令替换 ─
    if not command or not command.strip():
        return ValidationResult(Verdict.REJECTED, "Empty command.")
    if "\n" in command or "\r" in command:
        return ValidationResult(Verdict.REJECTED, "Newline not allowed.")
    if "$(" in command or "`" in command:
        return ValidationResult(Verdict.REJECTED, "Command substitution is not allowed.")

    # ─ 1. 解析 ─
    try:
        commands, ops, redirects = extract_commands(command)
    except (ValueError, FileNotFoundError) as exc:
        return ValidationResult(Verdict.REJECTED, f"Cannot parse bash: {exc}")

    if not commands:
        return ValidationResult(Verdict.REJECTED, "No commands found.")

    # ─ 2. 禁止串联操作符 ─
    ops_error = check_forbidden_ops(ops)
    if ops_error:
        return ValidationResult(Verdict.REJECTED, ops_error)

    # ─ 3. 禁止输出重定向 ─
    if redirects:
        return ValidationResult(Verdict.REJECTED, "Output redirection not allowed.")

    # ─ 4. 逐个命令分类 ─
    for index, toks in enumerate(commands):
        if not toks:
            continue
        cmd_display = " ".join(toks[:2]) if len(toks) > 1 and not toks[1].startswith("-") else toks[0]

        # 4a: 黑名单 → 直接拒绝
        if _match_spec(toks, DENIED_CMDS) > 0:
            return ValidationResult(Verdict.REJECTED,
                f"Command '{cmd_display}' is blocked for safety.")

        # 4b: 白名单 → 检查匹配级别
        allowed = _match_spec(toks, ALLOWED_CMDS)
        if allowed == 1:  # 前缀匹配但被 denied flag 拦截
            return ValidationResult(Verdict.REJECTED,
                f"Command '{cmd_display}' uses a forbidden flag.")
        if allowed == 0:
            quoted = cmd_display
            return ValidationResult(Verdict.UNKNOWN,
                f"Unknown command(s): [{quoted}]. This command will NOT be executed. "
                "Please review manually and approve if safe.")

        # 4c: 文本处理命令能直接读文件；系统工具无路径白名单，只允许它们接在管道后段
        if index == 0 and toks[0] in PIPE_ONLY_CMDS:
            return ValidationResult(Verdict.REJECTED,
                f"'{toks[0]}' may only be used after a pipe (e.g. 'docker ps | {toks[0]} ...'); "
                "reading files belongs to the log query tool.")

        # 4d: 交互式命令会占住连接直到超时（-bn1 这类组合短 flag 也算 -b）
        has_batch = any(
            token == "-b" or (token.startswith("-") and not token.startswith("--") and "b" in token)
            for token in toks[1:]
        )
        if toks[0] == "top" and not has_batch:
            return ValidationResult(Verdict.REJECTED,
                "top must run in batch mode: add '-b -n 1'.")

        # 4e: kubectl 凭据泄露组合
        kubectl_error = _misleading_kubectl(toks)
        if kubectl_error:
            return ValidationResult(Verdict.REJECTED, kubectl_error)

    # ─ 5. 管道段数 + awk/sed 内部写检查 ─
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return ValidationResult(Verdict.REJECTED, f"Cannot tokenize: {exc}")

    segments = split_pipeline(tokens)
    if any(not seg for seg in segments):
        return ValidationResult(Verdict.REJECTED, "Empty pipeline segment.")
    if len(segments) > MAX_SEGMENTS:
        return ValidationResult(Verdict.REJECTED,
            f"Too many pipeline segments (max {MAX_SEGMENTS}).")

    for seg in segments:
        err = check_awk_sed(seg[0], seg[1:])
        if err:
            return ValidationResult(Verdict.REJECTED, err)

    return ValidationResult(Verdict.ALLOWED)