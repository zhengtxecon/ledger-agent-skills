---
name: ledger-maintenance
description: 定位、修正或删除个人真实账本中的已有交易。用户说旧账写错、改一笔、删除、撤回、修正 payee、memo、金额、账户或类别时使用。必须先按 txid 确认目标。
---

# Ledger Maintenance

已有交易必须先定位，再确认，再修改。

## 运行环境

- 使用 Windows PowerShell 和已安装的 `zledger`。本包提供 zledger 工具代码，使用本项目独立 Python 环境的入口，避免误用本机其他同名工具。
- 私有数据位置取用户配置的 `FINANCES_REPO` / `FINANCES_LEDGER_PATH`，未配置时按本包 README 的平台默认位置；数据不存在时先确认初始化，不猜测路径。
- 先读本包 `AGENTS.md` 与已配置私有数据项目的本地指引；命令字段以当前工具 `--help` 为准，账户与别名取私有数据定义。
- 不确定参数时读取当前工具的 `--help`。工具不可用时说明缺少的依赖，不自行改用直接编辑账本。
- 本包只保存通用指引。不得将真实账户名称或别名、账号、交易对方、金额、余额、txid、case ID、凭据、查询结果或导出副本写入本包或公开仓库；数据留在用户配置的私有数据目录。

## 定位

1. 用 `zledger transaction` 按日期、账户或 memo 缩小范围。
2. 用 `zledger transaction show --txid TXID` 查看完整源分录。
3. 不凭摘要猜目标；多个候选时让用户确认 `txid`。
4. 旧交易缺少 `txid` 时，说明 `zledger txid backfill` 的影响并在执行前取得确认。

## 删除与修正

- 删除：用户确认后运行 `zledger record delete --txid TXID`。
- 简单交易修正：确认完整新内容后，删除旧交易，再转 `$ledger-record` 重记。
- `zledger` 无法安全表达的复杂分录、opening 或多 posting 修改：停止操作并向用户报告明确的功能缺口；不要直接编辑 `.beancount`。
- 删除和重记是两个独立写操作。第一步完成后若第二步失败，如实报告部分完成状态，不隐瞒已删除事实。

## 验证

- 删除后再次运行 `zledger transaction show --txid OLD_TXID`，确认旧交易已不存在。
- 重记后按新 `txid` 查询，核对完整内容。
- 回复列出旧 `txid`、新 `txid`（如有）、实际完成步骤和验证结果。

- 所有删除只经 `zledger record delete`；禁止直接编辑 `.beancount`。

## Related Skills

- [ledger-query](../ledger-query/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-record](../ledger-record/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-reimbursement](../ledger-reimbursement/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
