# F-R v3 任务合同审计

状态 `STAGE_FR_TASK_CONTRACT_REVIEW_REQUIRED`。C1 和 X2 revision 2 已完成独立资格验证。
X1 的 Oracle 会拒绝合同允许的实现，P2 的公开解码规则互相冲突。这两项需要 Owner 决定，正式冻结停在准入前。

## 需求与 Oracle 对照

| 任务 | 高风险规则及依据 | 审计结果 |
| --- | --- | --- |
| S1 pipeline migration | 公开约定字段规范化、非数字计数回退、排序、空输入和 ValueError。Oracle 检查同一输入输出边界。 | 保留 revision 1。 |
| S2 state-machine refactor | 共享任务描述公布完整状态表、终态、ValueError、共享 transition 和失败不记审计。Oracle 检查调用路径和审计顺序。 | 保留 revision 1。 |
| P1 connector migration | 公开接口约定记录物理行号、空行与游标、错误类型、规范 JSON 字节、批切分、单调 checkpoint 和排序。Oracle 覆盖这些规则。 | 保留 revision 1。 |
| P2 protocol adapter | 公开线格式及 CRC、版本、异常和流缓冲大体可追溯。v1 的 id 在同一文档分别被描述为可选字段和始终为空的解码字段。 | 已复现合同冲突，保留 revision 1，待 Owner 明确旧 id 的兼容语义。 |
| X1 API compatibility audit | 公布真实 `record` 调用须使用关键字、settlement 须经过 ledger，以及返回流程和库存原子性。Oracle 单按名称识别疑似审计调用，连内部普通 helper 也会命中。 | 已复现误判，保留 revision 1，待 Owner 决定 Oracle 修订。 |
| X2 distributed diagnosis | `publish` 的 Event 边界和 `nack` 重试对恢复的持久化时点进入公开接口。Oracle 检查异常、状态和实际重开恢复数据。 | revision 2。 |
| C1 config lifecycle | 命名预设的完整字段表、TLS 缺失字段顺序和环境输入的错误类别、code、path 进入公开接口。Oracle 逐项覆盖。 | revision 2。 |
| C2 scheduler retry | 公布状态表、退避参数、生命周期事件格式、持久化次序和恢复顺序。Oracle 以这些规则检查恢复、重试与取消。 | 保留 revision 1。 |

### 待决项

- **X1** 的 `test_no_audit_call_in_a_repaired_module_is_positional` 按调用名匹配 `record`、
  `write_audit`、`_emit` 和 `_sink`，未解析真实绑定。独立副本把 `tax_for(amount)` 提取进
  `_emit(amount)`，真实 `record` 调用仍只传关键字。Reference 公测 15/15、Oracle 18/18。
  等价副本公测 15/15，Oracle 17/18，唯一失败正是同名 helper。建议保留关键字要求，用可观测的
  调用参数和真实 API 绑定检查。`settle` 的 ledger 路径也可用对公开入口的调用观测验证。
- **P2** 的公开 JSON 说明先称 v1 `id` 可选、缺省为空，后写解码的 `identifier=""`。
  Reference 和 Oracle 在字段存在时保留它。独立副本把 v1 解码值固定为空，缺省 ID 公测通过，
  对应隐藏测试失败。建议明确字符串 ID 存在时保留、缺省时为空，并公开错误类别。

## 已修订的合同

### C1 revision 2

- 核实旧 Reference 与 Owner 的三套预设表一致。旧隐藏测试要求 prod 超时 60.0，旧公开 Seed
  未提供该预设值。新接口公布完整表、缺项继承、优先级、`profile` 来源、未知名错误和独立副本。
- 旧 Oracle 在只有 cert 时仍要求报 `tls.cert`。新规则对缺 cert 报 `tls.cert`，
  对已有 cert 但缺 key 报 `tls.key`，两项都缺时先报 cert。禁用 TLS 时跳过跨字段检查，字段
  自身类型验证继续执行。
- C02 和 C03 的旧 Oracle 均捕捉到匹配前缀的非字符串未抛 `ParseError`。revision 2 明确
  前缀区分大小写，空路径、空段和非字符串统一为 `ParseError`，`code="parse"`，`path` 保留
  原始环境键。Reference 的环境实现符合该合同。

### X2 revision 2

C05 的两项失败都归入 B 类。公开签名接受 Event，旧接口和可见测试没有定义非法实参的
异常类别。导出实现读取传入字典的 `.id`，触发 `AttributeError`。Reference 和旧 Oracle
使用 `ValueError`。新合同明确先拒绝非 Event 并保留队列和 Journal 状态。

旧接口写明内存 `nack` 增加尝试数、恢复按 Journal 重建。旧合同未规定尚未再次 `take` 的
持久化时点。C05 导出只更新内存，重开 Journal 仍读到 0。新合同要求 `nack` 返回时重开
Journal 可读到新次数，且真正恢复后仍能读到，保留已公布的 JSONL 记录形状和首次取件顺序。
Oracle 比较重开的数据和恢复顺序，允许等价的入队与持久化次序。Reference 已满足这两项，
其 digest 保持相同。

## 资格验证

| revision | Seed 公测通过 | Seed Oracle 通过 | Reference 公测 | Reference Oracle | 旧 Mutation 检出 | 新 Mutation 检出 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| C1 r2 | 0/12 | 0/25 | 12/12 | 25/25 | 3/3 | 9/9 |
| X2 r2 | 1/11 | 5/20 | 11/11 | 20/20 | 3/3 | 2/2 |

等价实现在各自两套测试全部通过。生产共用 fixture 构建器在单节点、多节点和实际工作区
给出相同公开文件，未出现 Oracle 或 Reference 内容。静态 A2 说明引用公开的新增规则，
原依赖和授权路径保持有效。完整新旧 task、seed、reference、oracle、public specification、
fixture 和 qualification digest 记录在离线资格 JSON。

复现命令为 `python -m experiments.stage_f.revision_qualification --evidence-root PATH`，
其中 `PATH` 为新的本地证据目录。历史默认查询保持 revision 1。revision 2 通过
`task_at_revision` 显式选择，生产 Dispatcher 的版本绑定留待解除冻结阻断后交付。

## A0/A2 零改动及诊断指标

C01 A0 的四次 bash 和 C04 A2 两个节点的六次及四次 bash 都是只读。
注册路径提供写入和编辑工具。同路由的既有确定性传输证据含成功的实际写入。
对应两格的历史证据未保存每次真实请求发出的完整工具 schema 或原始 Provider finish reason，
也无写入调用和写入拒绝。C01 的 Agent 汇总以及 C04 的两节点汇总均给出成功，且输出为空。
C04 的原始节点终止代码为框架的 `success`。Oracle 仍失败，保留原执行分类。
现有轨迹未证实 Runner 写入故障，也无法确定模型为何停止。

| v2 格 | 公测用例通过 | 隐藏用例通过 | 改动文件数 | 必须修改却零改动 |
| --- | ---: | ---: | ---: | --- |
| C01 | 0/12 | 0/15 | 0 | 是 |
| C02 | 12/12 | 12/15 | 9 | 否 |
| C03 | 12/12 | 13/15 | 9 | 否 |
| C04 | 0/12 | 0/15 | 0 | 是 |
| C05 | 11/11 | 18/20 | 9 | 否 |

表中用例数来自旧版本导出副本复现。全部格的 public/hidden assertion count 与 assertion passed
均为 `UNKNOWN`。原证据未记录实际运行到的 Python 断言，不能计算 Assertion Pass Ratio。
严格 Oracle verdict、Effective completion、Delivered success 与 Execution integrity 继续使用原值。

C05 的上下文证据继续保留。33 条完成请求，首次输入 4,169，末次 54,581，系统 token 982,810。
59 次工具调用，28 次读文件，5 次重复读路径，3 次策略拒绝。未观察到历史压缩或工具结果卸载。

## 冻结及历史状态

本次先交付两份已获批的修订。正式 v3 未生成新的执行身份、Executor、Manifest、价格与停止
策略冻结摘要、三批 Plan、三份 Authorization 或启动命令，未执行本轮正式生产准入。
旧 Stage F、H4、v1 及 v2 证据继续保留，v2 为 `TOKEN_STOP_REACHED`，三批 Claim 仍为
已消费、未消费、未消费。本轮真实模型请求、新生产 Claim 和 Sandbox 启动均为 0。
