"""只读日志查询命令的静态校验模块。

默认拒绝架构：仅允许白名单内的只读命令通过管道（|）组合。任何 shell
控制符、重定向、写入或执行原语都在此被拒绝——发生在命令到达 SSH 会话
之前。校验通过后，gateway 会将命令的工作目录锁定在模块授权的 log_dir
下执行。
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

MAX_SEGMENTS = 5
MAX_PATH_TOKENS = 50

# 只读命令白名单：任何不在此列表中的命令一律拒绝。
ALLOWED_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "zgrep",
    "head", "tail", "cat", "zcat",
    "wc", "sort", "uniq", "awk", "sed",
    "cut", "tr", "tac",
    "ls", "find", "du", "file",
})

# 原始命令字符串中任何位置都不允许出现的 shell 控制符与重定向符。
# 注意：这也会拒绝含这些字符的合法 awk/grep 表达式；搜索词请改用
# `grep -e '...'`，并接受 awk 中 `>` 比较不可用（安全优先的取舍）。
FORBIDDEN_CHARACTERS = (";", "&&", "||", "`", "$(", ">", "<", "\n", "\r")

# find 的危险动作：执行外部命令或删除文件。
FIND_FORBIDDEN_FLAGS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprintf", "-fls",
})

# sed：禁止就地编辑，禁止写文件/执行命令的脚本指令。
# w 命令可跟在地址之后（如 `1,10w file`、`$w file`），
# 因此前置上下文允许数字/逗号/$/花括号。
SED_INPLACE_FLAG = re.compile(r"^-[a-zA-Z]*i")
SED_WRITE_COMMAND = re.compile(r"(?:^|[;}{]|\d|,|\$)\s*[wWe]\b")
SED_SUBST_WRITE = re.compile(r"s/(?:[^/\\]|\\.)*?/(?:[^/\\]|\\.)*?/[a-zA-Z]*[wWe]")

# awk：禁止执行命令与读取外部输入的原子能力。
# （脚本内的 `>` 与 `|` 已被 FORBIDDEN_CHARACTERS / 管道分段拦截。）
AWK_FORBIDDEN = (
    re.compile(r"system\s*\("),
    re.compile(r"getline"),
)

# sort -o / --output 会写文件，禁止。
SORT_OUTPUT_FLAG = re.compile(r"^(?:-[a-zA-Z]*o|--output)")


class CommandValidationError(ValueError):
    """提交的查询命令未通过只读校验时抛出。"""


def _is_flag(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _split_pipeline(tokens: list[str]) -> list[list[str]]:
    """按 '|' 把 token 序列切分为管道段；拒绝空段与过多段。"""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "|":
            segments.append([])
        else:
            segments[-1].append(token)
    if any(not segment for segment in segments):
        raise CommandValidationError("Empty pipeline segment; check the '|' placement.")
    if len(segments) > MAX_SEGMENTS:
        raise CommandValidationError(f"Too many pipeline segments: at most {MAX_SEGMENTS} are allowed.")
    return segments


def _check_command_specific(cmd: str, tokens: list[str]) -> None:
    """针对具备写入/执行能力工具的逐命令参数校验。"""
    if cmd == "sed":
        for token in tokens:
            if SED_INPLACE_FLAG.match(token):
                raise CommandValidationError("sed in-place editing (-i) is not allowed; sed is read-only here.")
        for token in tokens:
            if _is_flag(token):
                continue
            if SED_WRITE_COMMAND.search(token) or SED_SUBST_WRITE.search(token):
                raise CommandValidationError("sed write/execute commands (w, W, e) are not allowed.")
    elif cmd == "awk":
        for token in tokens:
            if token == "-f":
                raise CommandValidationError("awk -f (script from file) is not allowed; pass the script inline.")
            if _is_flag(token):
                continue
            for pattern in AWK_FORBIDDEN:
                if pattern.search(token):
                    raise CommandValidationError(f"awk script contains a forbidden primitive: {pattern.pattern}")
    elif cmd == "find":
        for token in tokens:
            if token in FIND_FORBIDDEN_FLAGS:
                raise CommandValidationError(f"find {token} is not allowed in read-only queries.")
    elif cmd == "sort":
        for token in tokens:
            if SORT_OUTPUT_FLAG.match(token):
                raise CommandValidationError("sort output redirection (-o/--output) is not allowed.")
    elif cmd == "uniq":
        positional = [token for token in tokens if not _is_flag(token)]
        if len(positional) > 1:
            raise CommandValidationError("uniq accepts at most one file argument; its second positional argument would be an output file.")


def _check_path_token(token: str, log_dir: str) -> None:
    """校验单个疑似路径 token 是否位于授权 log_dir 内。"""
    parts = PurePosixPath(token).parts
    if ".." in parts:
        raise CommandValidationError(f"Path traversal ('..') is not allowed: {token!r}")
    if not token.startswith("/"):
        # 相对路径在远程以 cwd=log_dir 为基准解析，且已拒绝 '..'，
        # 无法逃出授权目录。
        return
    normalized = PurePosixPath(token).as_posix()
    root = PurePosixPath(log_dir).as_posix().rstrip("/")
    if normalized != root and not normalized.startswith(root + "/"):
        raise CommandValidationError(
            f"Path {token!r} is outside the authorized log directory {log_dir!r}. "
            "Patterns containing '/' must be passed via grep -e 'PATTERN'."
        )


def _check_paths(cmd: str, tokens: list[str], log_dir: str) -> int:
    """校验一个管道段内所有疑似路径的 token；返回路径 token 数量。"""
    path_tokens = 0
    previous: str | None = None
    for token in tokens:
        if previous == "-e":
            # -e 的值是搜索词/脚本而非路径（允许其中含 '/'）。
            previous = token
            continue
        previous = token
        if _is_flag(token):
            continue
        _check_path_token(token, log_dir)
        path_tokens += 1
    if path_tokens > MAX_PATH_TOKENS:
        raise CommandValidationError(f"Too many file arguments: at most {MAX_PATH_TOKENS} per query.")
    return path_tokens


def validate_query_command(command: str, log_dir: str) -> None:
    """校验自由格式的只读查询命令。

    Args:
        command: 模型提交的原始命令字符串（允许管道）。
        log_dir: 目标模块授权的绝对日志目录；命令中的绝对路径必须位于其内。

    Raises:
        CommandValidationError: 任一校验规则拒绝该命令时抛出。
            调用方绝不能把被拒绝的命令下发到 SSH 会话。
    """
    if not command or not command.strip():
        raise CommandValidationError("Empty command.")
    # 第一关：字符级拒绝（先于任何解析，宁可误拒不可漏放）。
    for forbidden in FORBIDDEN_CHARACTERS:
        if forbidden in command:
            raise CommandValidationError(
                f"Character sequence {forbidden!r} is not allowed; only read-only pipelines with '|' are permitted."
            )
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandValidationError(f"Unable to parse command: {exc}") from exc
    if not tokens:
        raise CommandValidationError("Empty command.")

    # 第二关：管道分段 + 每段首 token 白名单。
    segments = _split_pipeline(tokens)
    total_path_tokens = 0
    for index, segment in enumerate(segments):
        cmd = segment[0]
        if cmd not in ALLOWED_COMMANDS:
            raise CommandValidationError(
                f"Command {cmd!r} is not in the read-only whitelist: {', '.join(sorted(ALLOWED_COMMANDS))}"
            )
        # 第三关：逐命令参数规则（sed/awk/find/sort/uniq）。
        _check_command_specific(cmd, segment[1:])
        # 第四关：路径限定在授权 log_dir 内。
        found = _check_paths(cmd, segment[1:], log_dir)
        total_path_tokens += found
        if index == 0 and found == 0:
            raise CommandValidationError(
                "The first pipeline segment must reference at least one log file or directory."
            )
    if total_path_tokens == 0:
        raise CommandValidationError("No file target found in the command.")
