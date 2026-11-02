"""只读日志查询命令的静态校验模块 — 基于 safecmd AST 解析。

基于 safecmd 的 shfmt bash AST 解析器进行命令校验，替代手写正则浅层字符匹配。
safecmd 负责：AST 解析、命令白名单匹配、危险 flag 递归校验、输出重定向目标校验。
本模块负责：拒绝命令串联操作符（; && ||）、拒绝换行注入、逐命令参数规则
（awk/sed 内部写原语）、路径限定在授权 log_dir 内、管道段数与文件目标约束。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path, PurePosixPath

def ensure_shfmt_on_path() -> None:
    """shfmt 由 shfmt-py 提供；直接 python 调用时可能不在 PATH。"""
    if not shutil.which("shfmt"):
        venv_scripts = Path(sys.executable).parent
        os.environ["PATH"] = str(venv_scripts) + os.pathsep + os.environ.get("PATH", "")


ensure_shfmt_on_path()

from safecmd import CmdSpec, DisallowedCmd, DisallowedDest
from safecmd.bashxtract import extract_commands as safecmd_extract
from safecmd import validate as safecmd_validate

MAX_SEGMENTS = 5
MAX_PATH_TOKENS = 50

# 两个 guard 共用：串联操作符、管道切分、awk/sed 内部原语检查。
FORBIDDEN_OPS = frozenset({";", "&&", "||"})

# ---------------------------------------------------------------------------
# safecmd 自定义命令白名单（仅此 18 个只读/分析命令）
# ---------------------------------------------------------------------------
CUSTOM_CMDS: set[CmdSpec] = {
    # 纯只读：无参数限制
    CmdSpec("grep"), CmdSpec("egrep"), CmdSpec("fgrep"), CmdSpec("zgrep"),
    CmdSpec("head"), CmdSpec("tail"), CmdSpec("cat"), CmdSpec("zcat"),
    CmdSpec("wc"), CmdSpec("cut"), CmdSpec("tr"), CmdSpec("tac"),
    CmdSpec("ls"), CmdSpec("du"), CmdSpec("file"),
    # sort：-o/--output 写输出文件，设为 dest flag → dest 校验一律拒绝（EMPTY_DESTS）
    CmdSpec("sort", dest_flags={"-o", "--output"}),
    # uniq：无 dest_flags/dest_pos（输出文件由 _check_command_specific 手动校验）
    CmdSpec("uniq"),
    # awk：禁止 -f（从文件读脚本）
    CmdSpec("awk", denied={"-f"}),
    # sed：禁止 -i（就地编辑）；w/W/e 命令由本模块的正则规则补充校验
    CmdSpec("sed", denied={"-i"}),
    # find：禁止危险 flag；-exec/-execdir 的下一个参数递归校验
    CmdSpec("find", denied={"-delete", "-ok", "-okdir"},
            exec_flags={"-exec", "-execdir"}),
}
# 空目标集合：拒绝所有输出重定向
EMPTY_DESTS: set[str] = set()

# ---------------------------------------------------------------------------
# sed / awk 内部写原语 + sort --output= 单 token 形态
# （safecmd AST 看不到脚本内部的语义，dest_flags 不处理 --flag=value 格式）
# ---------------------------------------------------------------------------
SED_WRITE_COMMAND = re.compile(r"(?:^|[;}{]|\d|,|\$)\s*[wWe]\b")
SED_SUBST_WRITE = re.compile(r"s/(?:[^/\\]|\\.)*?/(?:[^/\\]|\\.)*?/[a-zA-Z]*[wWe]")

SORT_OUTPUT_FLAG = re.compile(r"^(?:-[a-zA-Z]*o|--output)")

AWK_FORBIDDEN = (
    re.compile(r"system\s*\("),
    re.compile(r"\bgetline\b"),
    re.compile(r"\bprint\b.*>"),  # awk 内部输出重定向（safecmd 的 AST 看不到）
)


# ---------------------------------------------------------------------------
class CommandValidationError(ValueError):
    """提交的查询命令未通过只读校验时抛出。"""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def is_flag(token: str) -> bool:
    return token.startswith("-") and token != "-"


def check_forbidden_ops(ops: set[str]) -> str | None:
    """返回命令串联操作符错误消息；没有则返回 None。"""
    found = FORBIDDEN_OPS & ops
    if not found:
        return None
    return (
        f"Command chaining operators ({', '.join(sorted(found))}) are not allowed; "
        "only read-only pipelines with '|' are permitted."
    )


def split_pipeline(tokens: list[str]) -> list[list[str]]:
    """按 '|' 切分 token 序列；空段由调用方判断。"""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "|":
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def check_awk_sed(cmd: str, tokens: list[str]) -> str | None:
    """检查 awk/sed 脚本字符串内部的写/执行原语（AST 层不可见）。"""
    if cmd == "sed":
        for token in tokens:
            if is_flag(token):
                continue
            if SED_WRITE_COMMAND.search(token) or SED_SUBST_WRITE.search(token):
                return "sed write/execute commands (w, W, e) are not allowed."
    elif cmd == "awk":
        for token in tokens:
            if is_flag(token):
                continue
            for pattern in AWK_FORBIDDEN:
                if pattern.search(token):
                    return f"awk script contains a forbidden primitive: {pattern.pattern}"
    return None


def _check_command_specific(cmd: str, tokens: list[str]) -> None:
    """针对 sort/sed/awk/uniq 内部写/执行原语的逐命令参数校验。"""
    err = check_awk_sed(cmd, tokens)
    if err:
        raise CommandValidationError(err)
    if cmd == "sort":
        for token in tokens:
            if SORT_OUTPUT_FLAG.match(token):
                raise CommandValidationError(
                    "sort output redirection (-o/--output) is not allowed."
                )
    elif cmd == "uniq":
        # 第二个非 flag 位置参数是输出文件（safecmd 的 dest_pos 会因缺少参数就报错，
        # 不适合 uniq -c 等可选场景；这里手动检查）
        positional = [token for token in tokens if not is_flag(token)]
        if len(positional) > 1:
            raise CommandValidationError(
                "uniq accepts at most one file argument; "
                "its second positional argument would be an output file."
            )


def _check_path_token(token: str, log_dir: str) -> None:
    """校验单个疑似路径 token 是否位于授权 log_dir 内。"""
    # shell 展开会绕过静态路径检查：$HOME/x、~/x 在校验时是相对路径，执行时变成绝对路径。
    if ("$" in token or token.startswith("~")) and "/" in token:
        raise CommandValidationError(
            f"Shell expansion in a path is not allowed: {token!r}. "
            "Use a literal path, or pass patterns containing '/' via grep -e 'PATTERN'."
        )
    parts = PurePosixPath(token).parts
    if ".." in parts:
        raise CommandValidationError(
            f"Path traversal ('..') is not allowed: {token!r}"
        )
    if not token.startswith("/"):
        # 相对路径以 cwd=log_dir 为基准解析，且已拒绝 '..'，无法逃逸。
        return
    normalized = PurePosixPath(token).as_posix()
    root = PurePosixPath(log_dir).as_posix().rstrip("/")
    if normalized != root and not normalized.startswith(root + "/"):
        raise CommandValidationError(
            f"Path {token!r} is outside the authorized log directory {log_dir!r}. "
            "Patterns containing '/' must be passed via grep -e 'PATTERN'."
        )


def _check_paths(tokens: list[str], log_dir: str) -> int:
    """校验 safecmd 提取的命令参数中疑似路径的 token；返回路径 token 数量。

    tokens 不应包含命令名（由调用方传入 cmd_tokens[1:]）。
    含 '/' 的搜索词必须写成 `grep -e 'PATTERN'`：`-e` 的值会被豁免。
    """
    path_tokens = 0
    previous: str | None = None
    for token in tokens:
        if previous == "-e":
            previous = token
            continue
        previous = token
        if is_flag(token):
            continue
        _check_path_token(token, log_dir)
        path_tokens += 1
    if path_tokens > MAX_PATH_TOKENS:
        raise CommandValidationError(
            f"Too many file arguments: at most {MAX_PATH_TOKENS} per query."
        )
    return path_tokens


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def validate_query_command(command: str, log_dir: str) -> None:
    """校验自由格式的只读查询命令。

    Args:
        command: 模型提交的原始命令字符串（允许管道 |）。
        log_dir: 目标模块授权的绝对日志目录；命令中的绝对路径必须位于其内。

    Raises:
        CommandValidationError: 任一校验规则拒绝该命令时抛出。
            调用方绝不能把被拒绝的命令下发到 SSH 会话。
    """
    # ── 第 0 关：空命令 & 换行注入 ────────────────────────────────────
    if not command or not command.strip():
        raise CommandValidationError("Empty command.")
    if "\n" in command or "\r" in command:
        raise CommandValidationError(
            "Newline characters are not allowed; only single-line queries are permitted."
        )
    # 命令替换会让内部命令在校验之外执行（且 shlex 看不到其中的绝对路径），一律拒绝。
    if "$(" in command or "`" in command:
        raise CommandValidationError(
            "Command substitution ('$(' or backticks) is not allowed."
        )

    # ── 第 1 关：safecmd AST 解析 + 命令白名单 + 输出目标校验 ────────
    # safecmd.validate() 完成：
    #   (a) shfmt 将命令解析为 AST
    #   (b) 递归提取所有命令（包括管道、子 shell、$()、反引号内的命令）
    #   (c) 逐命令匹配 CUSTOM_CMDS（含 denied/exec_flags 规则）
    #   (d) 校验所有输出重定向目标（EMPTY_DESTS → 任何重定向都被拒绝）
    try:
        safecmd_validate(command, cmds=CUSTOM_CMDS, dests=EMPTY_DESTS)
    except DisallowedCmd as exc:
        raise CommandValidationError(
            f"Command {exc.name!r} is not in the read-only whitelist or uses a forbidden flag. "
            f"Allowed commands: {' '.join(sorted({c.name[0] for c in CUSTOM_CMDS}))}"
        ) from exc
    except DisallowedDest as exc:
        raise CommandValidationError(
            f"Output redirection to {exc.args[0]!r} is not allowed; "
            "only read-only queries are permitted."
        ) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise CommandValidationError(
            f"Unable to parse command as valid bash syntax: {exc}"
        ) from exc

    # ── 第 2 关：拒绝命令串联操作符 ──────────────────────────────────
    # safecmd 允许管道(|)但不过滤串联符；这里补充拒绝对模型行为影响大的操作符。
    try:
        _, ops, _ = safecmd_extract(command)
    except (ValueError, FileNotFoundError) as exc:
        raise CommandValidationError(
            f"Unable to re-parse command for operator check: {exc}"
        ) from exc
    ops_error = check_forbidden_ops(ops)
    if ops_error:
        raise CommandValidationError(ops_error)

    # ── 第 3 关：shlex 解析 + 管道段数 + 逐命令参数规则 + 路径限定 ────
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandValidationError(
            f"Unable to parse command arguments: {exc}"
        ) from exc
    if not tokens:
        raise CommandValidationError("Empty command after tokenization.")

    # 管道分段
    segments = split_pipeline(tokens)
    if any(not seg for seg in segments):
        raise CommandValidationError("Empty pipeline segment; check the '|' placement.")
    if len(segments) > MAX_SEGMENTS:
        raise CommandValidationError(
            f"Too many pipeline segments: at most {MAX_SEGMENTS} are allowed."
        )

    total_path_tokens = 0
    for index, segment in enumerate(segments):
        cmd = segment[0]
        # 逐命令参数规则（awk/sed 内部写原语 — AST 层看不到）
        _check_command_specific(cmd, segment[1:])
        # 路径限定在授权 log_dir 内
        found = _check_paths(segment[1:], log_dir)
        total_path_tokens += found
        if index == 0 and found == 0:
            raise CommandValidationError(
                "The first pipeline segment must reference at least "
                "one log file or directory."
            )
    if total_path_tokens == 0:
        raise CommandValidationError("No file target found in the command.")