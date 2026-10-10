---
name: ledger-record
description: 通过 zledger 新增个人真实账本交易。用户说花了、买了、收到收入、转账、换汇、报销入账、平账、余额校准或记一笔时使用。仅新增交易，不修改既有分录。
---

# Ledger Record

## 运行环境

- 使用 Windows PowerShell 和已安装的 `zledger`。本包提供 zledger 工具代码，使用本项目独立 Python 环境的入口，避免误用本机其他同名工具。
- 私有数据位置取用户配置的 `FINANCES_REPO` / `FINANCES_LEDGER_PATH`，未配置时按本包 README 的平台默认位置；数据不存在时先确认初始化，不猜测路径。
- 先读本包 `AGENTS.md` 与已配置私有数据项目的本地指引；命令字段以当前工具 `--help` 为准，账户与别名取私有数据定义。
- 不确定参数时读取当前工具的 `--help`。工具不可用时说明缺少的依赖，不自行改用直接编辑账本。
- 本包只保存通用指引。不得将真实账户名称或别名、账号、交易对方、金额、余额、txid、case ID、凭据、查询结果或导出副本写入本包或公开仓库；数据留在用户配置的私有数据目录。

## 路由

| 类型 | 场景 |
|---|---|
| `expense` | 消费、付款、刷卡、购买 |
| `income` | 工资、退款、报销回款、收到钱 |
| `transfer` | 自有账户间转账或换汇 |
| `adjust` | 新增平账分录、余额校准 |

修改或删除旧账转 `$ledger-maintenance`；查询转 `$ledger-query`；报销 case 关联转 `$ledger-reimbursement`。

## 必要信息

- `expense`：金额、付款账户、支出类别、payee、memo；日期默认今天。
- `income`：金额、收款账户、收入类别、payer、memo；日期默认今天。
- `transfer`：转出金额、from、to、memo；跨币种还需实际到账金额 `received`。
- `adjust`：账户、memo，以及 `delta` 或 `actual-balance` 二选一。
- 币种由账户定义决定；账户或类别使用唯一精确 alias，大小写不敏感，不做子串猜测。
- 信息不足时一次询问全部缺失的必要字段。`Unknown` 或 `Ambiguous` 时停止并让用户澄清。

## 执行与验证

1. 不确定参数时查看 `zledger record <subcommand> --help`。
2. 写入前按日期、账户、金额和 memo 查找可能的重复交易；明确重复发生的真实付款可以另记，不因内容相似直接拒绝。目标不明时先澄清。
3. 执行对应 `zledger record` 命令；不要用假交易测试。
4. 从输出取得 `txid`，再运行 `zledger transaction show --txid TXID`。
5. 核对日期、金额、币种、账户、方向、payee/payer 和 memo。
6. 只有查询结果与用户确认内容一致时才报告完成；否则报告实际状态，不追加第二笔补救交易。
7. 输出异常或中文乱码不等于写入失败。先查询 txid 和 UTF-8 源分录，禁止盲目重试写入。
8. 回复只保留操作结果、`txid` 和核验状态，不重复长命令或内部推理。

- 所有新增账本交易只经 `zledger record`；禁止直接编辑 `.beancount`。

## Related Skills

- [ledger-query](../ledger-query/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-maintenance](../ledger-maintenance/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-reimbursement](../ledger-reimbursement/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
