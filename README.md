# Ledger Agent Skills

面向 Codex 的个人记账角色与四项操作 skill。通过已有的 zledger 处理真实账务，写后按交易 ID 或报销 case 回查。

此包只发布工作指引与 skill 元数据。**不包含 zledger 工具代码、真实账本、账户定义、财务数据库、私人记录或凭据。** 安装此包本身不能运行账务命令。

## 分工

| 文件 | 职责 |
|---|---|
| [roles/ROLE.md](roles/ROLE.md) | 泉府掌籍角色、口吻、权限与身份存续 |
| [ledger-query](skills/ledger-query/SKILL.md) | 查余额、流水、账户、类别和 Dashboard |
| [ledger-record](skills/ledger-record/SKILL.md) | 新增支出、收入、转账、换汇和平账 |
| [ledger-maintenance](skills/ledger-maintenance/SKILL.md) | 按 txid 删除或修正旧账 |
| [ledger-reimbursement](skills/ledger-reimbursement/SKILL.md) | 报销 case 与交易关联 |
| [AGENTS.md](AGENTS.md) | 本包的维护规则 |

`agents/openai.yaml` 只提供每项 skill 的名称、简介和默认提示词。ROLE.md 定义角色；SKILL.md 定义操作流程；实际财务仓库 AGENTS.md 定义工具契约。

## 运行依赖

- Windows PowerShell。
- 用户自行准备的 finances 仓库，以及兼容的 zledger。工具须支持 `balance`、`summary`、`transaction show`、`account`、`category`、`record` 和 `reimbursement`；使用前以本机 `--help` 核实支持情况。
- finances 仓库中维护入口账本、账户与类别别名，以及项目 AGENTS.md。
- 报销操作需要该工具所用的 SQLite 数据库。账本交易与报销 case 分别核对。

不提供 zledger 的下载地址或安装器，因为本包不发布其代码。没有兼容工具时只能查阅指引，不执行真实记账。

## 安装

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

配置实际 finances 仓库位置，例如在当前 PowerShell 会话中设置：

```powershell
$env:FINANCES_REPO = (Resolve-Path '.\finances').Path
Set-Location -LiteralPath $env:FINANCES_REPO
zledger --help
```

该变量供 agent 定位仓库，不是 zledger 原生的账本路径参数。zledger 如需覆盖账本入口，使用其支持的 `FINANCES_LEDGER_PATH`。不要把真实个人路径提交到本包。

角色文件需要显式接入本地全局或项目 AGENTS.md；只安装 skill 不会自动载入角色。把 [examples/AGENTS.fragment.md](examples/AGENTS.fragment.md) 中的路由片段按本地路径配置后，合并进现有 AGENTS.md，保留其他指引。

## 账务流程

1. 核对必要字段，账户和类别使用唯一精确别名，币种取账户定义。
2. 新增交易前查可能重复；字段或交易目标有歧义时澄清。
3. 仅通过 zledger 写入，取得 txid；报销关联另取 case ID。
4. 回查完整分录或 case，核验后才报告完成。
5. 修正简单旧账时先确认目标与新内容，再删除、核验、重记、回查。重记失败须明确报告已删除的事实。

账户结构、期初余额、数据库迁移和工具代码改动按角色权限先呈方案。禁止用虚假交易测试、直接编辑真实账本、以第二笔补救交易掩盖失败，或把 conversion 当成第二笔报销回款。

## 发布版整理范围

- 将硬编码的个人路径改为项目配置；保留四项 skill 的操作、确认和验证规则。
- 明确写前查重及输出异常后禁止盲目重试。
- 角色身份退出规则与全局路由保持一致。
- 将未随包提供的养老金刷新 skill 引用改为可选扩展说明。
- 保留 agents/openai.yaml 原有用途，不新增自动执行功能。

本仓库是公开的可移植发行版。本机既有角色和 skills 不会因发布而自动替换。

## 验证范围

发布时检查四项 skill 的结构、相互引用、个人信息和敏感文件排除，并进行流程语义核对。未在真实账本上执行写入或删除测试；工具端的兼容性须由使用者针对已安装版本确认。
