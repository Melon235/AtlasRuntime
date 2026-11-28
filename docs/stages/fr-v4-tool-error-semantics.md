# F-R v4 工具错误语义收尾

## C03 取证

v3 C03 第 19 轮的模型响应 `66fa49a1-60ae-4e78-acca-ab38cfd1daa8`
只提交了一个工具调用 `call_00_1Gksu3PzYsKY1rSsLTi57843`。工具为 `bash`，
参数为 `command="git status --porcelain && git diff --stat"`、`cwd="/home/user"`、
`timeout=null`、`max_output_bytes=null`。用原参数经 AnyCode 的 BashInput 验证后计算的
摘要与实际 Action Policy 轨迹相符。

Action Policy 的 `allow-shell.execute` 规则和 Ownership 外部策略均返回允许。
调用进入 AnyCode 与 SandboxWorkspaceTools，此时 Sandbox binding 为 READY，
工作区根是 `/workspace/project`。`_logical()` 在 `bash()` 进入 `_execute()`
前抛出 `ValueError`。这条路径既未取得 Provider 操作门，也未发出该命令、文件读取或
文件写入。同一响应内无其他调用。OpenSandbox 的秒级日志含同秒的命令请求，
日志没有模型调用 ID，无法仅凭秒级时间判断归属。随后留有导出 tar 和 Sandbox 销毁记录。

原始 ToolResult 由 AnyCode SQLite 缓存保留，`is_error=true`、`retry_safe=false`。
Action Ledger 写入 `UNCERTAIN / EXECUTED_ERROR`。AnyCode 保留显式 false，
Agent Loop 在收齐批次结果后以 `side_effect_unknown` 停止。
原 v3 判定继续保持 `EXECUTION_INTEGRITY_FAILED`。

## 修复与故障检查

| 场景 | 可观测结果 |
|---|---|
| bash、file_read、file_write、file_edit、grep、list_files 的非法路径 | 本地边界拒绝，Provider 调用数为零。模型可用合法参数重新发起不同调用。写入类账本记录 `REJECTED_BEFORE_EXECUTION`。 |
| edit 的目标字符串缺失或歧义 | 已完成读取且未写入。错误可继续，账本记录 `OBSERVED_NO_MUTATION`。 |
| 观察到 bash 非零退出 | 保留实际输出和退出码。Agent 可继续。账本仍保守保留命令的可能副作用。 |
| Provider transport、Sandbox lost、未观察到退出码 | 继续停止。去掉 corrected 模式中仅用于选择成功文案的写入前存在性探测。 |
| mkdir 故障、部分写入、写后异常、取消 | 保留可能已有副作用的语义。取消仍传播并触发现有 binding 不确定记录。 |
| 多工具批次 | 通过调用 ID 分别记录拒绝与成功。 |
| Ownership | 原先的策略判定仍在 Sandbox 工具执行之前。 |

复现使用真实 AnyCode Agent Loop、Atlas Tool Runtime、Action Ledger、SQLite Idempotency、
确定性模型与真实隔离的 OpenSandbox。v4 复用固定任务版本、顺序、资源配置和原
`fr-v3-cell-budget/1` 停止语义。三批使用新的 Claim 和外部授权身份。
