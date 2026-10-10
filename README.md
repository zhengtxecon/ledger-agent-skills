# Ledger Agent Skills

一个可独立安装运行的个人记账项目：**zledger 工具代码、本机看板、泉府掌籍角色与四个 Codex skills 均在本仓库。** 克隆后安装 Python 依赖即可初始化空账本，不依赖作者的私人项目、账本或未发布工具。

Agent 根据角色和 skill 调用本包提供的 zledger。账务计算与读写由 zledger 完成，底层使用 Beancount 与 SQLite。Python 3.12+ 和公开 Python 包仍是运行依赖；安装时需下载这些依赖。

## 结构

| 目录或文件 | 职责 |
|---|---|
| `zledger/` | 查询、收支、转账、换汇、平账、交易 ID、账户配置、报销与看板命令 |
| `app/` | 本机 FastAPI 服务、报销数据库操作和无需 CDN 的看板页面 |
| `pyproject.toml` / `requirements.txt` | 安装入口与依赖 |
| [roles/ROLE.md](roles/ROLE.md) | 泉府掌籍角色与权限 |
| [skills/](skills/) | 查账、新增、修旧账、报销四项 skill 与展示元数据 |
| [AGENTS.md](AGENTS.md) | 项目维护规则与工具契约 |
| [examples/AGENTS.fragment.md](examples/AGENTS.fragment.md) | 将角色和 skills 接入本地 agent 的路由片段 |
| `tests/` | 临时隔离数据上的工具与安装验证 |

## 数据与隐私边界

本仓库不包含作者的真实交易、金额、余额、账户名称、账户别名、账号、交易对方、交易编号、报销编号、凭据或个人路径。工具中没有内置个人账户清单、卡号尾号、银行图标或个人币种分组。

- 实际账户由使用者自行配置，数据存放在本项目之外的私有目录。
- 默认位置为 Windows `%LOCALAPPDATA%\zledger`；其他系统为 `$XDG_DATA_HOME/zledger` 或 `~/.local/share/zledger`。
- `FINANCES_REPO` 可指定另一个私有数据目录；`FINANCES_LEDGER_PATH` 可指定其入口账本，并优先于 `FINANCES_REPO`。报销数据库默认位于入口账本旁的 `state/finance-reimbursements.db`。
- `FINANCES_REIMBURSEMENTS_DB` 可覆盖数据库路径，但必须位于该私有数据目录内。相对路径按私有数据目录解析。
- 禁止把真实值写入源代码、角色、skills、说明、示例、日志、截图或测试材料，再提交到本仓库。运行日志、数据库和配置写到私有数据目录。
- `account`、`--account TEXT`、`TXID`、`ID` 是通用命令或占位符。自动化测试中的数据均临时生成，测试结束清理；不读取真实账本。
- `.gitignore` 排除账本、数据库、导出及本地配置；提交前仍须检查拟发布内容与历史，避免真实值进入 Markdown 或代码。

对话历史由对话环境管理。报销中的跨币种历史汇率查询可访问 Frankfurter，仅发送日期和币种；它不上传账本、账户或交易对方。基础记账、同币种报销和看板不需要该服务。

## 安装工具并初始化

在克隆后的项目目录运行以下 PowerShell 命令。独立环境的入口不会替换已有的全局 zledger。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\zledger.exe --help
.\.venv\Scripts\zledger.exe init
.\.venv\Scripts\zledger.exe balance
```

初始化只生成空账本、当前年度文件和技术平账账户 `Equity:Adjustments`，不生成个人账户、交易或余额。已有非空目录时拒绝覆盖。

如要指定目录，使用 `init --directory PRIVATE_DIRECTORY`，并在后续会话将 `FINANCES_REPO` 设为同一路径。源代码或安装目录不能用于保存私有账务数据。

账户配置命令是 `zledger account add --name ACCOUNT --currency CURRENCY [--alias ALIAS] [--date DATE]`。`ACCOUNT` 必须是用户自行指定的完整 Beancount 账户名；消费、收入及平账涉及的类别也需要先定义。缺少币种或存在多个币种时，单币种写入拒绝猜测。新增账户属于结构变更，agent 执行前应确认。

年度切换使用 `zledger year --add YEAR` 生成空文件并更新 include；已有年度文件不会被覆盖。可选显示名、图标、归档标记与额度配置保存在私有数据目录的 `accounts.local.json`，按完整账户名映射到 `name`、`icon`、`archived`、`limit` 等字段。没有该文件时自动显示账本账户名，不内置任何私人账户。账户别名由 `account add --alias` 写到私有 `docs/accounts.md`。

## 使用

下文的 `zledger` 指当前项目独立环境中的入口；可激活此环境，或使用上面的完整入口路径。CLI 以 `--help` 展示全部字段。

| 命令 | 用途 |
|---|---|
| `balance` / `summary` | 余额与分币种净额 |
| `transaction` / `transaction show --txid TXID` | 流水和源分录 |
| `account` / `category` | 用户自己的账户与类别 |
| `record expense/income/transfer/adjust/delete` | 新增或按 ID 删除交易 |
| `reimbursement` | case、候选交易、关联及剩余金额 |
| `dashboard --no-open` / `dashboard status` / `dashboard stop` | 本机看板启停 |

看板默认只绑定 `127.0.0.1:8080`，可用 `ZLEDGER_DASHBOARD_PORT` 配置其他本机端口。显示账户、流水、归档与报销事项；账务操作使用 CLI。金额按币种显示，不使用预设汇率合并净资产。看板汇总沿用浮点显示；正式写入使用 Decimal 并由 Beancount 校验，账本为事实来源。

修正简单旧账时确认完整新内容，删除旧交易并核验，再重记并回查新 txid。重记失败必须明确报告已删除的事实。跨币种 transfer 需要实际到账金额；报销 conversion 不计为第二笔回款。

## 安装角色与 skills

将本仓库克隆到自己选择的维护目录。四项 skill 可分别安装，也可一起安装；工作流相互转交时，需要对应 skill 可用。

以下 PowerShell 例子将四项 skill 以目录联接暴露给 Codex。请先将 `$packagePath` 设为实际克隆路径。已有同名目录时停止，不覆盖、不移动原配置。

```powershell
$packagePath = (Resolve-Path '.\ledger-agent-skills').Path
$codexSkillRoot = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME 'skills'
} else {
    Join-Path $env:USERPROFILE '.codex\skills'
}
$skillNames = @('ledger-query', 'ledger-record', 'ledger-maintenance', 'ledger-reimbursement')
foreach ($name in $skillNames) {
    $entry = Join-Path $codexSkillRoot $name
    $sourcePath = Join-Path $packagePath "skills\$name"
    if (Test-Path -LiteralPath $entry) { throw "已有 skill，先核实现状：$entry" }
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'SKILL.md'))) { throw "缺少源 skill：$sourcePath" }
}
New-Item -ItemType Directory -Path $codexSkillRoot -Force | Out-Null
foreach ($name in $skillNames) {
    New-Item -ItemType Junction -Path (Join-Path $codexSkillRoot $name) -Target (Join-Path $packagePath "skills\$name")
}
```

角色和 skill 使用同一套私有数据位置配置。当前版本的 zledger 原生支持 `FINANCES_REPO` 与 `FINANCES_LEDGER_PATH`；使用默认数据目录时不用设置变量。不要把个人路径写入公开配置。

角色文件需要显式接入本地全局或项目 AGENTS.md；只安装 skill 不会自动载入角色。把 [examples/AGENTS.fragment.md](examples/AGENTS.fragment.md) 中的路由片段按本地路径配置后，合并进现有 AGENTS.md，保留其他指引。

## 验证与重新运行

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试只使用自动生成的临时空账本和合成数据，覆盖初始化、账户配置、交易读写、失败保留原数据、报销关联、API 与页面，以及独立安装后的 CLI。临时文件测试结束后删除，不接触用户现有 finances 数据。

## 维护

本仓库提供通用发行版。本机原有角色、skills、全局命令和真实 finances 项目不会因更新本仓库而自动替换。安装后的代码更新需在独立环境重新运行 `python -m pip install .`。

MIT 许可证。
