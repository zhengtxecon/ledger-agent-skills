# Package maintenance

本仓库提供 zledger 工具、本机看板、记账角色与四项 skills；不包含真实账务数据。

- 保留 roles/ROLE.md 的角色边界和 skills/ 中各流程的授权、验证与失败报告规则。
- 命令字段以本包 zledger 的 --help 为准；用户的私有数据项目指引和账户定义亦须遵循。
- 不加入个人绝对路径、账本、账户定义、报销数据库、凭据、日志或私人沿革记录。
- 不把真实交易、金额、余额、txid、case ID、商家或交易对方、账户名称、账户别名、账号写入本仓库的任何文件，包括角色、skill、示例及调试材料。账务数据只在使用者独立的私有数据目录中处理。
- agents/openai.yaml 的展示内容须与对应 SKILL.md 一致。
- 变更工作流时同步 README；检查引用和必要的前置依赖。
- 不为验证本包向真实账本写入测试交易，不自动迁移使用者已有 skill 或角色配置。

## 工具契约

- 所有账本写入经 zledger；写后按 txid 回查。查询账户为子串匹配；写入使用唯一精确 alias。
- expense 必需 amount、account、category、payee、memo；income 用 payer；transfer 需 from、to、amount、memo，跨币种再传 received；adjust 用 delta 或 actual-balance 二选一。
- init 创建空私有数据；account add 新增用户提供的账户及可选 alias；year --add 新增空年度文件。账户结构变更需用户确认，已有数据不得覆盖。
- 数据路径由 FINANCES_LEDGER_PATH / FINANCES_REPO 或平台默认目录决定；报销数据库必须在入口账本所在的私有目录内。
- 账本分录和 SQLite case 是两个事实来源。不要把 conversion 重复当作回款。
- 不确定调用时看 --help，不直接编辑用户账本，不盲目重试异常的写入。
- 默认看板只绑定本机地址；测试不得停掉或复用用户正在运行的看板。
- 重新验证统一运行 `python -m unittest discover -s tests -v`；常规功能统一走 zledger CLI。不要新增平行运行框架。
