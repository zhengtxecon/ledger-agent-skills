---
name: ledger-query
description: 查询个人真实账本、账户余额、总余额、交易流水、账户或类别定义，以及 Finance Dashboard 状态。用户询问余额、最近交易、账户、流水、归档资产或看板时使用。只读，不写账。
---

# Ledger Query

## 运行环境

- 使用 Windows PowerShell 和已安装的 `zledger`。本包提供 zledger 工具代码，使用本项目独立 Python 环境的入口，避免误用本机其他同名工具。
- 私有数据位置取用户配置的 `FINANCES_REPO` / `FINANCES_LEDGER_PATH`，未配置时按本包 README 的平台默认位置；数据不存在时先确认初始化，不猜测路径。
- 先读本包 `AGENTS.md` 与已配置私有数据项目的本地指引；命令字段以当前工具 `--help` 为准，账户与别名取私有数据定义。
- 不确定参数时读取当前工具的 `--help`。工具不可用时说明缺少的依赖，不自行改用直接编辑账本。
- 本包只保存通用指引。不得将真实账户名称或别名、账号、交易对方、金额、余额、txid、case ID、凭据、查询结果或导出副本写入本包或公开仓库；数据留在用户配置的私有数据目录。

- 不确定参数时依次查看 `zledger --help` 和对应子命令的 `--help`，不编造命令。
- 常用命令：
  - `zledger balance [--account TEXT] [--archived]`
  - `zledger summary [--archived]`
  - `zledger transaction [--limit N] [--date YYYY-MM-DD] [--account TEXT] [--memo TEXT]`
  - `zledger transaction show --txid TXID`
  - `zledger account list [--match TEXT] [--archived]`
  - `zledger account show --account TEXT [--archived]`
  - `zledger category list --type expense|income [--match TEXT]`
  - `zledger category show --type expense|income --category TEXT`
  - `zledger dashboard`、`zledger dashboard status`、`zledger dashboard stop`
- 问总余额或总资产时，先用 `summary` 取得各币种余额，再查询当时的可靠汇率，将非人民币余额统一折算为人民币；先报告折算后的人民币总资产，再列各币种余额、采用的汇率与估值时间。不要只分币种列示，也不要手工加总 `balance`。
- 启动 Dashboard 后返回可点击链接。
- 先给结论，再补必要明细；不得修改账本、SQLite 或项目文件。
- 新增交易转 `$ledger-record`；修改或删除旧账转 `$ledger-maintenance`；报销 case 转 `$ledger-reimbursement`。

## Related Skills

- [ledger-record](../ledger-record/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-maintenance](../ledger-maintenance/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
- [ledger-reimbursement](../ledger-reimbursement/SKILL.md)：相关账务转交该 skill，保留其确认与验证要求。
