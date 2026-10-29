"""command_guard 单元测试：覆盖 readonly-freeform-query spec 的全部场景。"""
from __future__ import annotations

import pytest

from command_guard import CommandValidationError, validate_query_command

LOG_DIR = "/data/logs/parking/member"


def validate(command: str, log_dir: str = LOG_DIR) -> None:
    validate_query_command(command, log_dir)


def rejects(command: str, log_dir: str = LOG_DIR) -> str:
    """断言命令被拒绝，并返回错误消息。"""
    with pytest.raises(CommandValidationError) as exc_info:
        validate_query_command(command, log_dir)
    return str(exc_info.value)


class TestRejected:
    """specs/readonly-freeform-query/spec.md 中的全部拒绝场景。"""

    def test_write_redirection(self):
        # 写重定向被字符级拦截
        assert "not allowed" in rejects("cat info.log > /tmp/x")

    def test_append_redirection(self):
        # 追加重定向
        rejects("tail -n 10 info.log >> /tmp/x")

    def test_command_chaining_semicolon(self):
        # 分号串联命令
        rejects("grep foo info.log; rm -rf /data")

    def test_command_chaining_and(self):
        rejects("head -n 10 info.log && rm info.log")

    def test_command_chaining_or(self):
        rejects("grep foo info.log || reboot")

    def test_command_substitution(self):
        # $() 命令替换
        rejects("grep $(echo passwd) info.log")

    def test_backtick_substitution(self):
        # 反引号命令替换
        rejects("grep `whoami` info.log")

    def test_newline_injection(self):
        # 换行注入
        rejects("grep foo info.log\nrm -rf /data")

    def test_awk_system(self):
        # awk 执行外部命令
        assert "awk" in rejects("""awk '{system("rm -rf /")}' info.log""")

    def test_awk_getline(self):
        # 注意：`getline < file` 形态已被全局 '<' 字符级拦截；
        # 这里用不带 '<' 的裸 getline 触发 awk 专项规则。
        assert "awk" in rejects("""awk '{if (getline) print $0}' info.log""")

    def test_awk_script_from_file(self):
        # awk -f 从文件读脚本无法静态校验，直接拒绝
        rejects("awk -f /tmp/evil.awk info.log")

    def test_sed_in_place(self):
        # sed 就地修改文件
        assert "sed" in rejects("sed -i 's/a/b/' info.log")

    def test_sed_in_place_combined_flag(self):
        # 组合 flag 中隐藏的 -i 同样拒绝
        rejects("sed -ni '1,10p' info.log")

    def test_sed_write_command(self):
        # sed 地址后跟 w 写文件命令
        assert "sed" in rejects("sed -n '1,10w /tmp/out' info.log")

    def test_sed_subst_write_flag(self):
        # sed 替换命令带 w 标志写文件
        assert "sed" in rejects("sed 's/a/b/w /tmp/out' info.log")

    def test_find_delete(self):
        assert "find" in rejects("find . -name '*.log' -delete")

    def test_find_exec(self):
        rejects("find . -name '*.log' -exec rm {} ;")

    def test_sort_output_flag(self):
        # sort -o 写输出文件
        assert "sort" in rejects("sort -o /tmp/out info.log")

    def test_sort_output_long_flag(self):
        rejects("sort --output=/tmp/out info.log")

    def test_uniq_second_positional_is_output(self):
        # uniq 第二个位置参数是输出文件
        assert "uniq" in rejects("uniq info.log /tmp/out")

    def test_path_traversal_relative(self):
        # 相对路径穿越
        assert "traversal" in rejects("grep foo ../../etc/passwd")

    def test_path_traversal_absolute(self):
        # 绝对路径中隐藏的 .. 穿越
        rejects("cat /data/logs/parking/member/../../../etc/passwd")

    def test_absolute_path_outside_log_dir(self):
        # 授权目录外的绝对路径
        assert "outside" in rejects("cat /etc/passwd")

    def test_non_whitelist_command_python(self):
        # 白名单外命令
        assert "whitelist" in rejects('python -c "print(1)"')

    def test_non_whitelist_command_curl(self):
        rejects("curl http://evil.example.com")

    def test_non_whitelist_in_pipeline_second_segment(self):
        # 管道后段同样受白名单约束
        rejects("grep foo info.log | sh")

    def test_empty_command(self):
        rejects("   ")

    def test_too_many_pipeline_segments(self):
        # 超过 5 段管道
        rejects("cat a.log | cat | cat | cat | cat | cat")

    def test_no_file_target_in_first_segment(self):
        # 首段没有任何文件目标（防止无界扫描/挂起）
        rejects("wc -l")

    def test_leading_pipe(self):
        # 管道开头是空段
        rejects("| grep foo info.log")

    def test_input_redirection(self):
        # 输入重定向可读取授权目录外文件
        rejects("grep foo < /etc/passwd")

    def test_awk_output_redirection_to_file(self):
        # 全局 '>' 拒绝同时覆盖 awk 的 print 重定向
        rejects("""awk '{print $1 > "out.txt"}' info.log""")

    def test_known_tradeoff_awk_comparison_with_gt(self):
        # 已知取舍：'>' 被全局拒绝，awk 的 `$3 > 10` 比较不可用；
        # 需改用 match()、grep 预过滤等替代写法。
        rejects("""awk '$3 > 10' info.log""")


class TestAllowed:
    """必须放行的只读查询形态。"""

    def test_grep_extended_regex(self):
        # 扩展正则搜索
        validate('grep -E "ERROR.*timeout" info.log')

    def test_pipeline_grep_tail(self):
        # 关键词搜索取最后 N 条
        validate('grep "orderId=123" info.log | tail -n 50')

    def test_zgrep_compressed(self):
        # 压缩归档日志搜索
        validate('zgrep "OutOfMemory" info.log.2026-10-27.gz')

    def test_absolute_path_inside_log_dir(self):
        # 授权目录内的绝对路径
        validate(f"tail -n 100 {LOG_DIR}/info.log")

    def test_log_dir_itself_as_target(self):
        # log_dir 本身作为目标（列目录）
        validate(f"ls -l {LOG_DIR}")

    def test_pattern_with_slash_via_e_flag(self):
        # 含 '/' 的搜索词经 -e 豁免路径校验
        validate("grep -e '/api/v1/login' access.log | wc -l")

    def test_five_segment_pipeline(self):
        # 5 段管道（上限内）：错误统计聚合
        validate("cat info.log | grep ERROR | sort | uniq -c | head -20")

    def test_awk_print_field(self):
        # awk 只读取字段（无危险原语）
        validate("""awk '{print $1, $4}' info.log | sort | uniq -c""")

    def test_sed_readonly_range_print(self):
        # sed 只读打印行范围
        validate("sed -n '1,100p' info.log")

    def test_find_by_name(self):
        # find 按名称/时间查找（无 -exec/-delete）
        validate("""find . -name '*.log' -mtime -1""")

    def test_relative_subdirectory_path(self):
        # 相对子目录路径
        validate("tail -n 50 archive/info.log")

    def test_wildcard_relative_glob(self):
        # 相对通配符（在 cwd=log_dir 内展开）
        validate("grep -l ERROR *.log")

    def test_head_of_grep_count(self):
        # 多文件计数
        validate("grep -c ERROR info.log error.log")

    def test_zcat_pipeline(self):
        # zcat 解压流 + 管道过滤
        validate("zcat info.log.2026-10-27.gz | grep -i exception | head -30")

    def test_tac_latest_lines(self):
        # tac 逆序读取最新行
        validate("tac info.log | head -n 100")

    def test_cut_and_tr(self):
        # 字段提取 + 字符清理 + 聚合
        validate("cut -d' ' -f4 info.log | tr -d '[' | sort | uniq -c")
