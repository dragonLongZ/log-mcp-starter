"""系统/Docker/K8s 命令三段式校验模块 — 基于 safecmd AST 解析。

分类体系：
  ALLOWED  ─ 安全命令白名单，自动执行
  REJECTED ─ 明确危险命令或违规语法（重定向/串联/注入），自动拦截
  UNKNOWN  ─ 不在白名单也不在黑名单，提醒用户手动审核

白名单有两层：本文件的内置白名单 + `system_allowlist.py` 提供的
可在线维护的自定义白名单（网页上增删，请求时热加载）。黑名单与通用语法规则
始终优先，自定义白名单只能“加白”，不能绕过拦截。
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

import system_allowlist

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
        if _spec_verdict(toks, spec) == 2:
            return 2
    for spec in specs:
        if _spec_verdict(toks, spec) == 1:
            return 1
    return 0


def _spec_verdict(toks: list[str], spec: CmdSpec) -> int:
    """单个 CmdSpec 的匹配结果，语义同 _match_spec（2/1/0）。"""
    n = len(spec.name)
    if tuple(toks[:n]) != spec.name:
        return 0
    if not spec.denied:
        return 2
    return 2 if spec(toks) else 1


def _custom_entries() -> list[system_allowlist.AllowEntry]:
    """读取当前生效的自定义白名单（内部已做 mtime 热加载，异常不影响内置规则）。"""
    try:
        return system_allowlist.load_allowlist()
    except Exception:  # 白名单文件坏了也不能让网关整体失效
        return []


def _custom_denied_hit(toks: list[str], entry: system_allowlist.AllowEntry) -> str | None:
    """自定义白名单的「禁用参数」检查，返回命中的那个值；未命中返回 None。

    为什么不能只用 safecmd 的 CmdSpec：它对长度 > 2 且不以 `--` 开头的 denied
    值只做**整 token 精确匹配**，于是组合写法能绕过：

        denied 含 `-dump:live` / `format=b` / `file=`
        `jmap -dump:live,format=b,file=/tmp/x.bin 1234`  ← 单个 token，精确匹配不到 → 放行

    jmap 的 `-dump:...` 本身就会把堆导出到文件，属于明确的写操作，不能被绕过。
    因此这里对长度 > 2 的值再做一次「子串包含」判断；长度 == 2 的短 flag
    （如 `-F`）仍交给 safecmd（它已按单字符包含判断，能拦住 `-Fb`）。
    """
    for denied in entry.denied:
        if len(denied) <= 2:
            continue
        for token in toks:
            if denied in token:
                return denied
    return None


def _match_custom(toks: list[str], entries: list[system_allowlist.AllowEntry]) -> int:
    """自定义白名单匹配，返回 2/1/0，语义同 _match_spec（叠加更严的 denied 判断）。"""
    for entry in entries:
        if _spec_verdict(toks, CmdSpec(entry.name, denied=set(entry.denied))) == 2:
            if _custom_denied_hit(toks, entry) is None:
                return 2
    for entry in entries:
        verdict = _spec_verdict(toks, CmdSpec(entry.name, denied=set(entry.denied)))
        if verdict == 1 or _custom_denied_hit(toks, entry) is not None:
            return 1
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
    custom = _custom_entries()
    custom_hits: list[str] = []
    for index, toks in enumerate(commands):
        if not toks:
            continue
        cmd_display = " ".join(toks[:2]) if len(toks) > 1 and not toks[1].startswith("-") else toks[0]

        # 4a: 黑名单 → 直接拒绝（自定义白名单不能覆盖）
        if _match_spec(toks, DENIED_CMDS) > 0:
            return ValidationResult(Verdict.REJECTED,
                f"Command '{cmd_display}' is blocked for safety.")

        # 4b: 内置白名单优先；只有内置白名单完全不认识时才查自定义白名单
        builtin_allowed = _match_spec(toks, ALLOWED_CMDS)
        if builtin_allowed == 1:  # 前缀匹配但被 denied flag 拦截（自定义白名单不放宽内置限制）
            return ValidationResult(Verdict.REJECTED,
                f"Command '{cmd_display}' uses a forbidden flag.")
        if builtin_allowed == 0:
            custom_allowed = _match_custom(toks, custom)
            if custom_allowed == 1:
                return ValidationResult(Verdict.REJECTED,
                    f"Command '{cmd_display}' uses a flag disabled in the custom allowlist.")
            if custom_allowed == 0:
                return ValidationResult(Verdict.UNKNOWN,
                    f"Unknown command(s): [{cmd_display}]. This command will NOT be executed. "
                    "Please review manually and approve if safe; an admin can register the "
                    "command prefix in the gateway dashboard's custom allowlist "
                    "(takes effect on the next request, no restart).")
            custom_hits.append(cmd_display)

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

    if custom_hits:
        return ValidationResult(Verdict.ALLOWED,
            f"Allowed by the custom allowlist: {', '.join(dict.fromkeys(custom_hits))}")
    return ValidationResult(Verdict.ALLOWED)
