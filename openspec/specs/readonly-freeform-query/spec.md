# readonly-freeform-query Specification

## Purpose
TBD - created by archiving change enhance-log-query-mcp. Update Purpose after archive.
## Requirements
### Requirement: 受限自由只读查询工具
系统 SHALL 提供 `execute_query` MCP 工具及对应 gateway 端点，允许模型在已登记模块的 `log_dir` 范围内提交只读命令组合（支持管道），用于 grep 正则、zgrep 压缩日志、head、tail、cat、zcat、wc、sort、uniq、awk 只读处理等日志排查场景。

#### Scenario: 正则搜索
- **WHEN** 提交 `grep -E "ERROR.*timeout" info.log`，模块与资产均已登记
- **THEN** 命令在该模块 log_dir 下执行并返回匹配行

#### Scenario: 管道组合查询
- **WHEN** 提交 `grep "orderId=123" info.log | tail -n 50`
- **THEN** 命令正常执行，返回最后 50 条匹配

#### Scenario: 压缩日志查询
- **WHEN** 提交 `zgrep "OutOfMemory" info.log.2026-10-27.gz`
- **THEN** 命令正常执行并返回匹配行

### Requirement: 命令白名单校验
gateway MUST 解析提交的命令，管道中每个命令段的首个词 MUST 属于只读白名单（grep、egrep、fgrep、zgrep、head、tail、cat、zcat、wc、sort、uniq、awk、sed 只读模式、ls、find、du、file、cut、tr、tac、less 非交互参数），否则拒绝执行。

#### Scenario: 白名单外命令被拒绝
- **WHEN** 提交 `python -c "..."` 或 `curl http://...`
- **THEN** 返回 400 错误，说明命令不在只读白名单内，不执行任何远程命令

### Requirement: 危险模式黑名单拦截
命令 MUST 拒绝以下模式：写/追加重定向（`>`、`>>`）、破坏性或提权命令（rm、mv、cp、chmod、chown、chgrp、tee、dd、mkfs、kill、sudo、su、sh、bash、eval、exec、python、perl、wget、curl、nc、ssh、scp、systemctl、service、reboot、shutdown）、命令串联符（`;`、`&&`、`||`、反引号、`$(...)`、换行符）、sed 就地修改（`-i`）与写命令（`w`）、awk 写/执行原语（`system(`、`print ... >`、`getline <`、`cmd | getline`）。校验在命令下发远程之前完成，被拒绝的命令 MUST NOT 到达 SSH 会话。

#### Scenario: 重定向被拒绝
- **WHEN** 提交 `cat info.log > /tmp/x`
- **THEN** 返回 400 错误，说明只允许只读查询，不执行远程命令

#### Scenario: 命令串联被拒绝
- **WHEN** 提交 `grep foo info.log; rm -rf /data`
- **THEN** 返回 400 错误，命令被整体拒绝

#### Scenario: awk 写原语被拒绝
- **WHEN** 提交 `awk '{system("rm -rf /")}' info.log`
- **THEN** 返回 400 错误，awk 脚本含被禁止的写/执行原语

#### Scenario: sed 就地修改被拒绝
- **WHEN** 提交 `sed -i 's/a/b/' info.log`
- **THEN** 返回 400 错误，sed 仅允许只读过滤

### Requirement: 路径限定在授权目录内
命令中出现的每个文件路径参数 MUST 解析到该资产已登记模块的 `log_dir` 之下（相对路径以 log_dir 为基准，绝对路径须以 log_dir 为前缀）；包含 `..` 路径分量或解析后越界的路径 MUST 被拒绝。若命令未指定路径，服务端 SHALL 拒绝（不允许无目标的全目录扫描超过既定发现限制）。

#### Scenario: 相对路径限定
- **WHEN** 提交 `grep foo ../../etc/passwd`
- **THEN** 返回 400 错误，路径超出授权 log_dir

#### Scenario: 绝对路径越界被拒绝
- **WHEN** 提交 `cat /etc/passwd`（log_dir 为 /data/logs/parking/member）
- **THEN** 返回 400 错误，路径不在授权目录内

#### Scenario: 授权目录内绝对路径放行
- **WHEN** 提交 `tail -n 100 /data/logs/parking/member/info.log` 且该路径在登记 log_dir 下
- **THEN** 命令正常执行

### Requirement: 输出与资源限制
自由查询 MUST 施加与现有工具一致的资源上限：输出字节数（默认 100KB）、执行超时（默认 60 秒）、单次查询涉及的文件数上限（50）。超限时 SHALL 截断输出并在结果中标注截断标志。

#### Scenario: 输出超限截断
- **WHEN** 查询结果超过 100KB
- **THEN** 返回前 100KB 内容且 `possibly_truncated_by_bytes=true`

#### Scenario: 超时中断
- **WHEN** 远程命令执行超过 60 秒
- **THEN** 命令被终止，返回超时错误说明

### Requirement: 查询结果可审计
`execute_query` 的返回 SHALL 包含：实际执行的命令、目标资产与模块、log_dir、退出码、输出文本、截断标志，便于模型与用户核对执行内容。

#### Scenario: 返回结构完整
- **WHEN** 一次自由查询成功执行
- **THEN** 返回体包含 server、module、log_dir、executed_command、exit_code、stdout、possibly_truncated_by_bytes 字段

