"""端到端覆盖测试：gateway + 2.0 测试资产。

用法：uv run python e2e_check.py
需要 gateway_host.py 已运行；资产未连接时会提示并轮询最多 120 秒。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

SERVER = "server-172-26-73-80-2.0-test"
MODULE = "member"
BASE = "http://127.0.0.1:8765"
TOKEN = (Path(__file__).resolve().parent.parent / "local.token").read_text(encoding="utf-8").strip()
CLIENT = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=75)

# (kind, label, command, expect)  kind: log|sys   expect: ok|reject|unknown|exit1
CASES = [
    ("log", "grep 管道", 'grep -E "INFO" info.log | head -5', "ok"),
    ("log", "正则锚点 $", "grep 'ERROR$' info.log | head -3", "ok"),
    ("log", "awk 字段", "awk '{print $1}' info.log | head -3", "ok"),
    ("log", "find -exec", "find . -name '*.log' -exec grep -l INFO {} \\; | head -3", "ok"),
    ("log", "ls log_dir", f"ls /data/logs/parking/{MODULE}", "ok"),
    ("log", "grep 无匹配退出码", "grep definitely_not_present_xyz info.log", "exit1"),
    ("log", "tail 缺文件退出码", "tail -n 5 missing-file-xyz.log", "exit1"),
    ("log", "重定向拒绝", "cat info.log > /tmp/x", "reject"),
    ("log", "目录外路径拒绝", "cat /etc/passwd", "reject"),
    ("log", "命令替换拒绝", 'grep "$(cat /etc/passwd)" info.log', "reject"),
    ("log", "变量展开拒绝", 'grep foo "$HOME/.ssh/id_rsa" info.log', "reject"),
    ("log", "串联拒绝", "grep foo info.log; ls", "reject"),
    ("log", "rm 拒绝", "rm -rf /data", "reject"),
    ("log", "sed -i 拒绝", "sed -i s/a/b/ info.log", "reject"),
    ("sys", "docker ps", "docker ps | head -5", "ok"),
    ("sys", "docker ps head 退出码", "docker ps | head -2", "ok0"),
    ("sys", "df -h", "df -h", "ok"),
    ("sys", "free -m", "free -m", "ok"),
    ("sys", "uptime", "uptime", "ok"),
    ("sys", "uname", "uname -a", "ok"),
    ("sys", "top 批量", "top -bn1 | head -5", "ok"),
    ("sys", "journalctl 只读", "journalctl -n 5", "ok"),
    ("sys", "kubectl get", "kubectl get pods", "ok"),
    ("sys", "docker rm 拒绝", "docker rm myapp", "reject"),
    ("sys", "kubectl secrets 拒绝", "kubectl get secrets -A", "reject"),
    ("sys", "kubectl raw 拒绝", "kubectl config view --raw", "reject"),
    ("sys", "grep 读文件拒绝", "grep root /etc/shadow", "reject"),
    ("sys", "head 读文件拒绝", "head -n 5 /etc/shadow", "reject"),
    ("sys", "awk 读文件拒绝", 'awk "{print}" /etc/passwd', "reject"),
    ("sys", "裸 top 拒绝", "top", "reject"),
    ("sys", "journalctl -f 拒绝", "journalctl -f", "reject"),
    ("sys", "重定向拒绝", "ps aux > /tmp/x", "reject"),
    ("sys", "串联拒绝", "ps aux; ls", "reject"),
    ("sys", "命令替换拒绝", 'ps "$(id)"', "reject"),
    ("sys", "未知 htop", "htop", "unknown"),
    ("sys", "未知 docker compose", "docker compose ls", "unknown"),
    ("sys", "未知 kubectl auth", "kubectl auth can-i get pods", "unknown"),
]


def ensure_connected() -> bool:
    state = "unknown"
    for _ in range(25):
        rows = CLIENT.get("/connections").json()["connections"]
        state = next((r["state"] for r in rows if r["server"] == SERVER), "unknown")
        if state == "connected":
            return True
        if state in ("disconnected", "needs_reauth", "unknown"):
            CLIENT.post("/connect", json={"server": SERVER})
            print(f"[auth] {SERVER} 状态 {state}，请在 gateway 终端完成密码/MFA，轮询中…")
        time.sleep(5)
    print(f"[auth] 未连接（state={state}），完成后重新运行本脚本。")
    return False


def run_case(kind: str, command: str) -> tuple[str, str]:
    if kind == "log":
        r = CLIENT.post("/execute-query", json={"servers": [SERVER], "module": MODULE, "command": command})
    else:
        r = CLIENT.post("/execute-system-query", json={"servers": [SERVER], "command": command})
    if r.status_code != 200:
        return "http", f"HTTP {r.status_code}: {r.text[:120]}"
    item = r.json()["results"][0]
    error = item.get("error") or ""
    if error.startswith("HTTP 400: UNKNOWN"):
        return "unknown", error
    if error.startswith("HTTP 400"):
        return "reject", error
    if error:
        return "error", error
    return "ok", f"exit={item.get('exit_code')} stdout={len(item.get('stdout', ''))}B"


def main() -> int:
    try:
        CLIENT.get("/health").raise_for_status()
    except Exception as exc:
        print(f"[fatal] gateway 不可用：{exc}")
        print("先在独立终端运行：uv run python gateway_host.py")
        return 2
    # 新代码不再要求 module；旧进程会返回 422
    probe = CLIENT.post("/execute-system-query", json={"servers": [SERVER], "command": "uptime"})
    if probe.status_code == 422:
        print("[fatal] gateway 仍是旧代码（system 端点要求 module）。请重启 gateway_host.py。")
        return 2
    if not ensure_connected():
        return 2

    failures = []
    for kind, label, command, expect in CASES:
        got, detail = run_case(kind, command)
        if expect == "ok":
            passed = got == "ok"
        elif expect == "ok0":
            passed = got == "ok" and "exit=0" in detail
        elif expect == "exit1":
            passed = got == "ok" and "exit=1" in detail
        elif expect == "reject":
            passed = got == "reject"
        else:
            passed = got == "unknown"
        print(f"{'PASS' if passed else 'FAIL'}  {kind:3s} {label:20s} expect={expect:7s} got={got:7s} {detail[:90] if not passed else ''}")
        if not passed:
            failures.append(label)

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    if failures:
        print("failed:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())