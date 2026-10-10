# Package maintenance

本仓库只维护记账角色与四项 skills，不包含工具代码或真实账务数据。

- 保留 roles/ROLE.md 的角色边界和 skills/ 中各流程的授权、验证与失败报告规则。
- 命令字段以使用者 finances 仓库 AGENTS.md 及已安装 zledger 的 --help 为准。
- 不加入个人绝对路径、账本、账户定义、报销数据库、凭据、日志或私人沿革记录。
- 不把真实交易、金额、余额、txid、case ID、商家或交易对方、账户名称、账户别名、账号写入本仓库的任何文件，包括角色、skill、示例及调试材料。账务数据只在使用者外部 finances 项目中处理。
- agents/openai.yaml 的展示内容须与对应 SKILL.md 一致。
- 变更工作流时同步 README；检查引用和必要的前置依赖。
- 不为验证本包向真实账本写入测试交易，不自动迁移使用者已有 skill 或角色配置。
