# 分享与安装配置说明（SETUP）

本文件回答三件事：

1. 分享给别人前，**分享者需要先清理什么**；
2. 别人拿到技能后，**怎么安装**；
3. 别人第一次用之前，**要配置什么、怎么配置**。

---

## 一、分享前：分享者必须清理的内容

技能目录里目前还残留了作者的个人数据，直接原样发给别人会导致：

- 别人无法用自己的 GitHub 建仓库（凭据路径指向作者机器才有用，但更关键是注册表历史污染）；
- 别人的去重清单里混入作者已用的仓库，导致选题判重错误；
- COS 上传账号密码被硬编码在文档里（已移除，见下文）。

分享前按顺序做这几步：

### 1. 已用仓库清单：无需清理（已移到个人目录）

全局已用仓库清单存放在各自的个人目录，**不在技能包内**：

- `~/.codex/go-annotation-pipeline/used-repositories.json`
- `~/.codex/go-annotation-pipeline/used-repositories.md`

直接分享技能目录不会带上你的使用记录，对方也天然从空清单开始，**不需要再执行清空操作**。

旧版技能把清单存在 `references/used-repositories.*`；如果你从旧版升级，首次运行 `repo_registry.py` 时会自动把旧记录迁移到个人目录（原文件保留为 `*.migrated` 备份，不丢数据）。想清零自己的清单时执行：

```bash
python3 <skill>/scripts/configure.py reset-registry --yes
```

### 2. 确认没有把个人凭据打进技能目录

下面这些文件**必须在技能目录之外**（在用户家目录），不属于技能内容，永远不要复制进分享包：

- `~/.codex/pg-code/github-context.json`（GitHub token）
- `~/.codex/go-annotation-pipeline/config.json`（COS cookie）

检查命令：

```bash
grep -RniE "ghp_|cos_uploader_sid|Bz123456|benzhi&password" <skill>/ --include="*.json" --include="*.md" --include="*.py"
```

如果还搜到 token 或账号密码，删掉并重新生成。

### 3. 检查 VERSION / 文档里的署名与个人笔记

`VERSION` 里只有版本记录，一般没问题；但如果你在 `SKILL.md`、`references/*.md` 里写过个人账号、路径、备注，随手删掉。

### 4. 打包分享

推荐直接压缩整个技能目录（一个文件夹）：

```bash
cd ~/.codex/skills
zip -r yelM5-红绿轨迹-最新-go-annotation-pipeline-0814.zip yelM5-红绿轨迹-最新-go-annotation-pipeline-0814
```

对方解压到 `~/.codex/skills/` 下即可。

> 注意：技能目录名要保持一致或让对方自行重命名，SKILL.md 里的 `<skill>` 占位符在运行时会被解析为技能根目录，不受目录名影响。

---

## 二、拿到手：怎么安装

### 方式 A：放到 Codex 技能目录（推荐）

1. 解压/复制技能目录到 `~/.codex/skills/`：

```bash
mkdir -p ~/.codex/skills
cp -R yelM5-红绿轨迹-最新-go-annotation-pipeline-0814 ~/.codex/skills/
```

2. 确认能读到 SKILL.md 和 scripts：

```bash
ls ~/.codex/skills/yelM5-红绿轨迹-最新-go-annotation-pipeline-0814
```

### 方式 B：放任意目录，手动指定脚本路径

脚本都支持绝对路径调用，例如：

```bash
python3 /path/to/技能/scripts/configure.py check
```

但在 Codex 中要触发技能，还是建议放 `~/.codex/skills/` 下。

---

## 三、第一次用之前：需要配置什么

在 Codex 里直接说下面任意一句，触发配置：

- “配置技能”
- “初始化配置”
- “首次配置”
- “帮我配置流水线”

配置器会引导你填写，本质就是下面的命令：

```bash
# 自检：先看缺什么
python3 <skill>/scripts/configure.py check
```

### 必填项（4 个）

| 配置项 | 是什么 | 怎么填 |
|---|---|---|
| `--github-username` | GitHub 用户名 | 你的 GitHub 登录名 |
| `--github-token` | GitHub Personal Access Token | 在 GitHub → Settings → Developer settings → Personal access tokens 生成，勾选 `repo`（含 `delete_repo`，脚本要建/删 public 仓库） |
| `--git-name` | git 提交作者名 | 任意真实姓名/昵称，**不能是 `PINRU Local`** |
| `--git-email` | git 提交作者邮箱 | 你的邮箱 |

### 可选但建议填（1 个）

| 配置项 | 是什么 | 怎么填 |
|---|---|---|
| `--cos-cookie` | COS 上传 cookie | 浏览器打开 `https://upload.jzxhnh.com/` 登录后，从开发者工具 Network 里复制 `cos_uploader_sid=...`；**或者**用 `--cos-username/--cos-password` 让脚本自动登录获取 |

### 其他可选

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `--claude` | `claude` | Claude Code CLI 路径；不在 PATH 时填绝对路径 |
| `--cos-base-url` | `https://upload.jzxhnh.com` | COS 上传站地址，一般不用改 |

### 三种配置方式（任选其一）

#### 方式 1：交互式（本地终端）

```bash
python3 <skill>/scripts/configure.py
```

按提示回车填写即可。

#### 方式 2：非交互式（Codex 帮你代跑）

你先把值告诉 Codex，Codex 会执行：

```bash
python3 <skill>/scripts/configure.py setup \
  --github-username <你的GitHub用户名> \
  --github-token <ghp_xxx> \
  --git-name <作者名> \
  --git-email <作者邮箱> \
  --cos-cookie <cos_uploader_sid>
```

#### 方式 3：环境变量

```bash
export GITHUB_USERNAME=<...>
export GITHUB_TOKEN=<...>
export GIT_AUTHOR_NAME=<...>
export GIT_AUTHOR_EMAIL=<...>
export COS_UPLOADER_SID=<...>     # 可选
export CLAUDE_BIN=<...>           # 可选

python3 <skill>/scripts/configure.py setup
```

---

## 四、配置后怎么确认能用

```bash
python3 <skill>/scripts/configure.py check
```

输出全绿（`docker` 是灰色可选项，不影响）即表示可以开始生产数据。

```bash
python3 <skill>/scripts/configure.py show
```

可查看脱敏后的配置。

---

## 五、必备外部依赖

配置器会自动检测，缺了会提示。清单如下：

| 依赖 | 用途 | 安装 |
|---|---|---|
| git | 仓库/分支管理 | 系统自带或 `xcode-select --install` |
| curl | COS 上传 | 系统自带 |
| go | 编译/测试 Go 项目 | `brew install go` 或 https://go.dev/dl/ |
| rsync | 轨迹失败回滚快照 | `brew install rsync` |
| claude | 跑 Claude Code 轨迹 | `npm install -g @anthropic-ai/claude-code` |
| Python openpyxl | 生成收集表 xlsx | `python3 -m pip install openpyxl` |
| docker（可选） | 本机容器验证 | Docker Desktop |

---

## 六、常见问题

**Q：`configure.py setup` 报“缺少必填项”？**
A：GitHub 用户名/token、git 作者名/邮箱这四项必须给全；COS cookie 和 claude 可选。

**Q：`github_project.py ensure` 报 401？**
A：GitHub token 无效或权限不足，重新生成 token（勾 `repo`）后重跑 `configure.py setup`。

**Q：上传轨迹报 cookie 过期？**
A：重新跑 `configure.py setup --cos-cookie <新sid>`，或 `--cos-username/--cos-password` 自动登录。

**Q：别人发来的（旧版）技能里 `references/used-repositories.*` 有数据？**
A：那是发送者的使用记录。在第一次运行 `repo_registry.py` **之前**先跑 `configure.py reset-registry --yes`——它会把这两个文件重命名为 `*.migrated` 备份，防止别人的记录被自动迁移进你自己的清单。新版技能不再把清单放进技能包，不会有这个问题。

**Q：能把配置写进技能目录一起带走吗？**
A：不能。配置必须落在 `~/.codex/...`，否则会泄露 token，且 git 同步/分享时会带出去。
