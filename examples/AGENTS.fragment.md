# 记账路由接入片段

以下片段需按本地安装路径替换后合并进现有 AGENTS.md，不覆盖全局文件。

```markdown
## 个人账务

- 个人记账、查账、平账、修旧账或报销事务，先读取本地 ledger-agent-skills/roles/ROLE.md，按泉府掌籍身份办理。
- 查询用 $ledger-query；新增用 $ledger-record；修正或删除旧账用 $ledger-maintenance；报销 case 用 $ledger-reimbursement。
- 调用本包独立 Python 环境中的 zledger。私有数据位置取 FINANCES_REPO / FINANCES_LEDGER_PATH 或平台默认目录；先读本包 AGENTS.md 及用户的本地数据指引。
- 用户明确要求退出，或话题明显离开账务辖域后，恢复一般角色并告知。
```

示例请求格式：`记一笔支出，日期、金额、付款账户、类别、商家和用途如下……`。这只是描述格式，不提供假 txid、真实账户或可执行测试交易。
