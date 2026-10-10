---
name: ledger-reimbursement
description: 管理个人报销 workflow，包括创建、查看和更新 reimbursement case，查询候选交易，以及按 txid link 或 unlink advance、repayment、conversion。用户提到报销、代付、回款挂账、case、剩余金额或换汇关联时使用。
---

# Ledger Reimbursement

报销 case 状态在 SQLite，资金事实在 Beancount，不混为一套数据。

## 运行环境

- 使用 Windows PowerShell 和已安装的 `zledger`。本包提供 zledger 工具代码，使用本项目独立 Python 环境的入口，避免误用本机其他同名工具。
- 私有数据位置取用户配置的 `FINANCES_REPO` / `FINANCES_LEDGER_PATH`，未配置时按本包 README 的平台默认位置；数据不存在时先确认初始化，不猜测路径。
- 先读本包 `AGENTS.md` 与已配置私有数据项目的本地指引；命令字段以当前工具 `--help` 为准，账户与别名取私有数据定义。
- 不确定参数时读取当前工具的 `--help`。工具不可用时说明缺少的依赖，不自行改用直接编辑账本。
- 本包只保存通用指引。不得将真实账户名称或别名、账号、交易对方、金额、余额、txid、case ID、凭据、查询结果或导出副本写入本包或公开仓库；数据留在用户配置的私有数据目录。

## 工作流

1. 用 `zledger reimbursement list [--all]` 定位 case。
2. 写操作前用 `zledger reimbursement show --case ID` 读取当前明细和汇总。
3. 缺少账本交易时，新增交易转 `$ledger-record`；修改旧交易转 `$ledger-maintenance`。
4. 用 `zledger reimbursement candidates --case ID [--q TEXT] [--limit N]` 核对候选、`txid`、推断角色、金额和账户。
5. 用户确认后执行：
   - `zledger reimbursement link --case ID --txid TXID --role ROLE`
   - `zledger reimbursement unlink --case ID --txid TXID`
   - `zledger reimbursement create ...`
   - `zledger reimbursement update ...`
6. 操作后再次 `show --case ID`，核验 `status`、`remaining`、linked entries 和适用的 conversion entries。

## 规则

- `role` 只用 `advance`、`repayment`、`conversion`。
- `conversion` 是回款后的换汇，不是第二笔回款；不得重复计数。
- 同一 `txid` 不应挂到两个 case。
- `unlink` 只删除映射，不删除账本交易。
- `create` 只创建 case，不自动 link 交易。
- 缺少 case id、txid、角色或目标范围时先澄清，不猜测。
- 回复说明 case id、操作、相关 txid、操作后状态、剩余金额和验证结果。

## Related Skills

- [ledger-query](../ledger-query/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-record](../ledger-record/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-maintenance](../ledger-maintenance/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
