# yelM5-红绿轨迹-最新-go-annotation-pipeline-0814

生产「Go 语言 × Bugfix / 问题排查」模型训练数据的完整流水线。选题统一来自自己 0-1 生成的 Go 项目；每个 bug 用互不相连的 orphan green/red 分支交付，模型只拿到 G1 单分支、单提交、无测试快照。一个 repo 最多 30 条数据，同一个 bug 只能出 bugfix 或 diagnosis 二选一。

> 这是分享给标注/出题同事使用的版本。首次使用前必须先做一次配置，见下文。

## 快速开始

### 1. 安装

把整个技能目录放到 Codex 技能目录：

```bash
mkdir -p ~/.codex/skills
cp -R yelM5-红绿轨迹-最新-go-annotation-pipeline-0814 ~/.codex/skills/
```

### 2. 配置（只做一次）

在 Codex 里说任意一句即可触发：

- 「配置技能」
- 「初始化配置」
- 「首次配置」
- 「帮我配置流水线」

本质是运行：

```bash
python3 <skill>/scripts/configure.py check    # 先看缺什么
python3 <skill>/scripts/configure.py setup    # 交互式填写
```

### 3. 自检通过即可开始

```bash
python3 <skill>/scripts/configure.py check
```

全绿即表示可以开始生产数据。

## 需要配置什么

| 配置项 | 是否必填 | 说明 |
|---|---|---|
| GitHub 用户名 | 必填 | 用于自动创建 public repo |
| GitHub Token | 必填 | Personal Access Token，勾选 `repo`（含 delete_repo） |
| git 作者名 | 必填 | 提交作者，不能是 `PINRU Local` |
| git 作者邮箱 | 必填 | 提交邮箱 |
| COS 上传 cookie | 可选 | 上传轨迹用，`cos_uploader_sid`；也可账号密码自动登录 |
| claude 路径 | 可选 | Claude Code CLI，默认 `claude` |

配置落盘在用户家目录（**不会**打进分享包、不会泄露）：
- `~/.codex/pg-code/github-context.json`
- `~/.codex/go-annotation-pipeline/config.json`

### 配置文件样例（脱敏）

`configure.py setup` 会自动生成 `~/.codex/pg-code/github-context.json`，无需手工编辑；下面只是脱敏样例，便于对照排查：

```json
{
  "defaultBranch": "main",
  "exportedAt": "2026-07-25T03:35:49+08:00",
  "gitAuthor": {
    "name": "tiezhu996",
    "email": "tiezhu996@users.noreply.github.com"
  },
  "github": {
    "accountId": "github-1778332919264",
    "accountName": "tiezhu996",
    "token": "ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "username": "tiezhu996"
  },
  "source": "PINRU",
  "version": 1
}
```

> `defaultBranch` 是共享凭据文件的兼容字段；本流水线不向交付仓库推送 `main` 或干净基座。

> 样例中除 `token` 用 `ghp_XXX...` 脱敏外，其余字段为真实值；使用时把 `token` 换成你新生成的 Personal Access Token（勾选 `repo` 权限）。

## 必备依赖

`configure.py check` 会自动检测。清单：

- git、curl、go、rsync、claude（Claude Code CLI）
- Python 3 + openpyxl（`pip install openpyxl`）
- docker（可选，仅本机容器验证）

## 目录结构

```text
yelM5-红绿轨迹-最新-go-annotation-pipeline-0814/
  SKILL.md            技能主说明（流程/红线/命令）
  README.md           本文件
  SETUP.md            分享与安装配置详细说明
  VERSION             版本记录
  references/         规则、收集表口径、热门仓库黑名单、全局已用清单
  scripts/            流水线脚本（configure / workspace / github_project / ...）
```

## 详细文档

- 完整流程与硬性规则：见 `SKILL.md`
- 分享前清理、安装、配置、常见问题：见 `SETUP.md`
- 收集表 21 字段口径：见 `references/collection-table.md`
- 红线与判定标准：见 `references/rules.md`

## 分享给别人前的注意事项

已用仓库清单存放在各自的个人目录 `~/.codex/go-annotation-pipeline/used-repositories.json`，**不在技能包内**：直接把技能目录发给别人即可，不会带上你的使用记录，对方也天然从空清单开始。

想清零自己的清单时才需要：

```bash
python3 <skill>/scripts/configure.py reset-registry --yes
```

详见 `SETUP.md` 第一节。
