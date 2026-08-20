---
name: yelM5-红绿轨迹-最新-go-annotation-pipeline-0814
description: 生产「Go 语言 × Bugfix/问题排查」模型训练数据的完整流水线（0-1 自建项目埋 bug，GitHub repo_url 分支交付）。当用户需要出题、埋 bug、写题面、红绿校准、跑 Claude Code 轨迹、质检轨迹或填写数据收集表时使用。首次使用或说「配置技能/初始化配置」时，先运行 scripts/configure.py。
---

# Go 标注数据生产流水线（0-1 自建项目版）

一条合格数据 = 可还原的题目 + 一条验证通过的好轨迹（单轮对话）。

硬性规则（红线、防泄漏清单、缺陷分类、判定标准）见 [references/rules.md](references/rules.md)，出题和质检前必读。
收集表 21 字段口径见 [references/collection-table.md](references/collection-table.md)。

## 首次配置（拿到技能后必须先做一次）

> 触发词：配置技能 / 初始化配置 / 首次配置 / 帮我配置流水线。此后本技能所有脚本会自动读取已配置项。

```bash
# 1) 自检：看缺什么（依赖 + GitHub 凭据 + COS cookie）
python3 <skill>/scripts/configure.py check

# 2) 写入配置（交互式：在本地终端直接运行）
python3 <skill>/scripts/configure.py

# 2') 非交互式（Codex 引导填好后代跑）
python3 <skill>/scripts/configure.py setup \
  --github-username <GitHub用户名> \
  --github-token <ghp_xxx> \
  --git-name <提交作者名> \
  --git-email <提交作者邮箱> \
  --cos-cookie <cos_uploader_sid> \
  # 没有 cookie 时也可自动登录：--cos-username <上传站账号> --cos-password <上传站密码>

# 3) 查看配置（token/cookie 脱敏显示）
python3 <skill>/scripts/configure.py show

# 4) 想清零自己的已用仓库清单时执行（注册表存于 ~/.codex/go-annotation-pipeline/ 个人目录，
#    不在技能包内：分享技能、覆盖升级都不需要此步）
python3 <skill>/scripts/configure.py reset-registry --yes
```

配置项与落盘位置：

| 配置项 | 说明 | 落盘位置 |
|---|---|---|
| GitHub 用户名 / Token | `github_project.py` 据此自动创建 **public** repo；Token 需勾选 `repo`（含 delete_repo）权限 | `~/.codex/pg-code/github-context.json`（与 pg-code 技能共用） |
| git 作者名 / 邮箱 | 提交时 author/committer；**名不能是 `PINRU Local`** | 同上 |
| COS 上传 cookie | 上传轨迹到 `upload.jzxhnh.com`；用 `--cos-cookie` 或账号密码自动登录获取；保存账号密码后，上传遇到 cookie 过期会自动刷新并重试一次 | `~/.codex/go-annotation-pipeline/config.json` |
| claude 路径 | Claude Code CLI；默认 `claude`，自定义时用 `--claude` | 同上 |

必备外部依赖（`configure.py check` 会自动列出）：`git`、`curl`、`go`、`rsync`、`claude`、Python `openpyxl`；`docker` 可选（只影响本机容器验证）。

> 更多安装与分享说明见 [SETUP.md](SETUP.md)。

## 核心口径（先记死）

1. **选题全部用自己的 0-1 生成项目**，不再去 GitHub 找题。
2. **同一个 repo 最多 30 条数据**，每条一个不同 bug；同一个 bug 只能出 bugfix / diagnosis **二选一**。用户要求超过 30 条时，必须按每仓最多 30 条拆成多个不同的 0-1 项目和 GitHub 仓库，仓库数为 `ceil(总条数 / 30)`。
3. **repo_url = `bug-<record>` 分支地址**；**bugfix 题在轨迹质检通过后，要把 Claude Code 测试模型修复后的 `env/` 作为新 commit 推到 `bug-<record>`**，因此最终 `bug-<record>` HEAD（repo_url）包含测试模型的 fix；diagnosis 题测试模型零改动，`bug-<record>` 保持埋好 bug 的代码。当前 Codex 完成的修复只保存在本地 `_gold/`，用于校准和质检，不创建远程 gold 分支、不进入收集表。
4. GitHub 去重身份**优先用 GitHub 地址，其次用本地路径**。
5. 交付只提交 GitHub `repo_url` 分支地址；不再提交修复 commit 字段，不打 zip、不截图。
6. Dockerfile 要支持 arm64/amd64，但本流程**实际只验证当前机器平台**。
7. **BUG_REPRO.md 是每条记录的交付复现说明**，记录 Bug 是什么 / 如何触发 / 错误信息；只进 GitHub 交付分支，不进测试模型的 `env/`。
8. **GitHub 发布和 Docker 验证必须在跑轨迹之前完成**，因为跑轨迹会修改 `env/`；跑完轨迹后的 `env/` 只用于质检，不再提交。
9. **GitHub 仓库名用真实项目名，长度 3-5 个英文单词**：用「领域 + 用途 + 类型」拼成描述性名字（如 `renovation-budget-expense-service`），既具体又不至于重名；不加 `go-` 前缀、不加随机码、不出现 `test`/`fix` 等字样；本地项目名用领域命名，别用 `forex` 这类泛化名。
10. **`verify_cmds` 对 bugfix / diagnosis 都必填，且只能是目标 Bug 的定向复现命令**：明确写出唯一目标包、精确测试名和 `-count=1`（并发类加 `-race`），禁止 `go test ./...`、通配包、当前目录、多包或拼接全量回归；命令对应的测试必须完整覆盖 `user_query` 描述的全部现象与触发条件。红、绿证据轨迹都必须**只实际执行一次**这条命令，实际 Bash 调用、最终回复【命令】和正式填表的 `verify_cmds` 必须逐字符完全相同，空格、路径写法、引号、参数顺序均不得变化；bugfix 校验红+绿，diagnosis 校验红。
11. **Bug 难度与修复规模是双重硬门禁**：bugfix 的 gold 修复必须同时改动至少 4 个不同的功能代码文件，并且功能代码增删总行数至少 20 行；`_test.go`、README、文档、注释和交付文件不计数，禁止拆文件或堆无效代码凑数。
12. **改动规模只是必要条件，不代表题目够难**：核心缺陷必须由至少 1 个 Go 运行时机制与另 1 个不同机制耦合形成，跨至少 3 个模块/包，并依赖调用顺序、并发交错、请求生命周期或状态转换才能完整触发。纯索引/边界/容量计算、单字段映射、单比较符、单 `%w`、单 nil 判断、单状态漏边等局部错误，即使扩写到 4 文件和 20 行也不合格。

## 流程总览（编号与下文章节一一对应）

1. **选题准备** → 0-1 项目来源、全局去重、GitHub repo 基线
2. **创建记录 workspace** → `workspace.py init / new-project`，每仓建 001–030 记录目录
3. **环境构建** → 埋 bug（env）+ gold 修复（_gold）+ 本地量改动规模
4. **题面写作** → 产出 user_query、task_type（含 4.1 难度审查、4.2 去重自检）
5. **红绿校准** → 产出 verify_cmds、success_criteria、gold_root_cause
6. **Docker 验证 + 写 BUG_REPRO + 发布 GitHub**（跑轨迹前）→ 产出 repo_url
7. **跑轨迹**（Claude Code 干净 session）→ 产出 session_id、trajectory、harness、generator_model；harness 必须写「工具名 + 版本号」，如 `Claude Code CLI v2.1.233`
8. **轨迹质检四查** → 产出质检结论；四查通过 → 先过绿灯验收（附录）→ 再 8.1 补推测试模型 fix
9. **填收集表 + 上传轨迹 + 收尾登记** → 产出 collection

> 「附:红/绿证据轨迹」分两个阶段穿插执行：**红灯在第 6→7 步之间**（开考门禁：测试模型实测确认 bug 可复现，不过则重新埋错、不得开跑轨迹）；**绿灯在第 8 步四查通过后、8.1 push-fix 之前**（验收测试模型的修复成果——只有绿灯确认过的修复才推上 GitHub），产出 pre_fix / post_fix JSON 并回填 verify_result；详见文末附录。

## 工作区与目录约定（本期根目录 = 调用时的 cwd）

```text
<本期根目录>/
  _shared/                      # 本期全局共享
    used-repositories.json      #   全局已用仓库镜像（repo_registry.py sync 生成）
    used-repositories.md
    收集表_汇总.xlsx             #   全局汇总填表数据（一条一行）
  _gold/                        # 出题人私有答案区（gold 模型正确代码；不交付、不进 git）
    <name>__<record>/           #   每条记录一份 gold 修复后的代码
  _repos/                       # 每个 0-1 项目一个 central git repo（github_project.py 管理）
    <repo_name>/
  _rejected/                    # 已标记删除的项目统一移到这里
  YYYY-MM-DD/
    <name>__<record>/           # record=001…030（每个 repo 独立编号）
      status.json               #   项目状态卡（唯一状态事实源，不含答案线索）
      env/                      #   埋好 bug 的 workspace（测试模型在这里跑）
      prompt.txt                #   题面
      <session_id>.jsonl        #   轨迹（文件名 == session_id）
      collection.json           #   本项目 21 字段填表数据（唯一事实源）
      _failed_rounds/           #   失败轮次归档（重跑时自动迁入上一轮轨迹/绿灯产物，不污染本轮）
      收集表_<project>.xlsx      #   本项目独立填表数据
```

项目状态（status.json 的 `state`）：

| 状态 | 含义 |
|---|---|
| `candidate` | 已建项目，尚未选中出题 |
| `selected` | 已选中，正在构建数据 |
| `done` | 数据完成，仓库已登记进全局已用清单 |
| `rejected` | 不符合要求，已标记删除（移入 `_rejected/`） |

## 常用脚本（均在本期根目录下执行）

| 脚本 | 作用 |
|---|---|
| `python3 <skill>/scripts/repo_registry.py` | 全局已用仓库/项目注册表：`check` 查重 / `register` 登记 / `list` / `sync`；一个 repo 最多 30 条 |
| `python3 <skill>/scripts/workspace.py` | 工作区与项目状态：`init` / `new-project` / `list` / `set` / `reject` / `purge` |
| `python3 <skill>/scripts/pick_bug_pattern.py` | 随机抽取深度埋错模式（P1–P12）：`--category` 过滤 / `--exclude` 排除同 repo 已用 / `--list` |
| `python3 <skill>/scripts/difficulty_review.py` | 私有难度审查单：`init` 创建模板 / `check` 校验运行时机制、跨层触发、题面覆盖和逐文件回退证据 |
| `python3 <skill>/scripts/github_project.py` | GitHub public repo 创建与分支管理：`ensure` / `publish` / `push-fix` |
| `python3 <skill>/scripts/collection_table.py` | 收集表填表数据：`new` / `write` / `sync` / `list` |
| `python3 <skill>/scripts/run_trajectory.py` | 跑轨迹与失败回滚 |
| `python3 <skill>/scripts/run_evidence_trajectories.py` | 红/绿证据轨迹：`generate` 生成上传并回填 verify_result / `validate` 校验 |
| `python3 <skill>/scripts/analyze_trajectory.py` | 轨迹客观检查 |
| `python3 <skill>/scripts/upload_trajectory.py` | 上传轨迹到 COS 并回填链接 |
| `python3 <skill>/scripts/build_docker.py` | Docker 本机验证（不打包、不截图） |
| `python3 <skill>/scripts/configure.py` | 首次配置向导：`check` 自检 / `setup` 写配置 / `show` 查看 / `reset-registry` 清空已用清单 |
| `python3 <skill>/scripts/post_qc.py` | **后置质检**（交付前硬校验）：`--root .` 对整期所有记录做 build/scope/red/green/文件/字段/证据/诊断零改动/验证命令覆盖/难度审查 10 项检查 |

## 第 1 步：选题准备（0-1 项目 + 去重）

### 1.1 确定项目来源

0-1 项目来源由用户指定：可以是项目生成提示词，也可以是本地项目目录。若给的是提示词，先由本流程模型生成完整可运行的 Go 项目；若给的是本地目录，直接使用。

- **0-1 项目必须保留/补写 `README.md`**（项目说明、目录结构、运行与测试命令、环境变量），交付时随代码进 GitHub `main` 与每个 `bug-*` 分支，避免仓库/分支看着空荡荡。

生成/拿到项目后，先确认三件事：

```bash
cd <项目目录>
go build ./...
go test ./...
```

`go build ./...` 必须通过。此时是**干净、无 bug** 的基线。

### 1.2 全局去重（红线，先做）

```bash
# 优先用 GitHub 地址；没有 GitHub 地址时用本地绝对路径
python3 <skill>/scripts/repo_registry.py check <repo|url|local-path> --source auto \
  --github-url <github_url> --local-path <local_path>
```

- 全局注册表唯一事实源是 `~/.codex/go-annotation-pipeline/used-repositories.json`（个人目录，随技能升级/分享不受影响）；同目录 `used-repositories.md` 是自动生成的人类可读镜像，勿手改。旧版存在技能目录 `references/` 内的清单会在首次运行时自动迁移过来（原文件保留为 `*.migrated` 备份）。
- 去重身份按优先级：**GitHub 地址优先，本地路径其次**。
- 一个 repo 最多 30 条；达到 30 条即永久排除，不得换 bug / 换任务类型继续出。总需求超过 30 条时新建下一个独立 0-1 项目和 GitHub 仓库，不得复制同一本地项目或更换地址绕过上限。

### 1.3 建立 GitHub 仓库基线

```bash
python3 <skill>/scripts/github_project.py ensure \
  --root . --repo-name <repo_name> --local-path <项目目录>
```

脚本会：

- 用 `~/.codex/pg-code/github-context.json` 的 GitHub 凭据/作者自动创建 **public** repo（审核方需要能访问）；
- **GitHub 仓库名直接用真实项目名，3-5 个英文单词**（`--repo-name` 的 slug，如 `renovation-budget-expense-service`），避免重名；不加 `go-` 前缀、不加随机码、不出现 `test`/`fix` 等字样。
- 把干净 0-1 基线提交到 `main` 并 push；
- 生成多架构可用的 `benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md` 与 `.dockerignore`；**前三个文件必须且只允许位于 GitHub 仓库根目录**（即使 `go.mod` 在 `backend/` 等子目录）；
- 本地 central repo 保存在 `_repos/<repo_name>/`（与 GitHub 仓库同名）。

GitHub 分支模型：

```text
main                    0-1 干净基线（无 bug）
bug-<record>            埋好 bug；bugfix 题跑完轨迹后补推 Claude Code 测试模型修复 commit -> repo_url
```

当前 Codex 的正确修复只保存在本地 `_gold/<project>/`，不创建或推送远程 gold 分支。

## 第 2 步：创建记录 workspace

初始化工作区：

```bash
python3 <skill>/scripts/workspace.py init --root .
```

创建记录（`--count` 是单仓数量，范围 1–30）：

```bash
python3 <skill>/scripts/workspace.py new-project --root . --source local \
  --repo <repo_name> --local-path <项目目录> --count <1-30>
```

- 每条记录目录是 `YYYY-MM-DD/<name>__<record>/`，单仓 record=001–030。
- `env/` 是待埋 bug 的 workspace；`_gold/<name>__<record>/` 是 gold 答案区，模型不可达。
- 建完后对每条记录 `workspace.py set --root . --project <name>__<record> --state selected`。

### 2.1 超过 30 条时拆仓（硬规则）

- 收到总数 `N` 后，先计算仓库数 `ceil(N / 30)`，按顺序分片；前面的仓库各 30 条，最后一个仓库放余数。例如：10 条=`[10]`，30 条=`[30]`，31 条=`[30,1]`，60 条=`[30,30]`，65 条=`[30,30,5]`。
- 每个分片必须对应一个**不同的 0-1 项目、不同本地路径和不同 GitHub 仓库**；分别执行一次 `github_project.py ensure`，再对该仓执行 `workspace.py new-project --count <本仓条数>`。
- 每个仓库的记录编号都从 `001` 重新开始，最多到 `030`；不得向单次 `workspace.py new-project` 传入大于 30 的 `--count`。
- 仓库名仍按真实业务命名，不能只在同一名称后加序号，也不能复制同一份源码来规避每仓 30 条上限。

## 第 3 步：环境构建（埋 bug + gold 修复）

对每条记录：

1. **在 `<project>/env/` 里埋一个 bug**：
   - 按 bug_category 选型。bug_category 只允许以下取值：`concurrency并发问题` / `slice相关问题` / `error异常错误` / `nil相关问题` / `context相关问题` / `defer相关问题` / `其他问题`；优先 concurrency / nil / slice / error / context / defer 里多步定位、强模型也会栽的缺陷。
   - **埋法从深度模式库随机抽取**：`python3 <skill>/scripts/pick_bug_pattern.py`（P1–P12，详细埋法见 [references/bug-patterns.md](references/bug-patterns.md)）；单仓达到 13–30 条时允许轮换复用模式骨架，但同一模式必须换业务层次、埋点组合、触发条件和用户可见症状，不能复刻同一个 bug。优先轮完 P1–P12 后再复用，`--exclude` 只排除近期已用模式。
   - **硬性红线（机制 + 规模）**：核心缺陷必须是「主运行时机制 + 不同耦合机制」组成的一条不可拆故障链，跨至少 3 个模块/包，并依赖时序、生命周期或状态转换触发；bugfix 的 gold 修复还必须**同时满足**「至少改动 4 个不同的功能代码文件」和「功能代码增删总行数至少 20 行」。禁止纯索引/边界/容量计算、单字段映射、单比较符、单 `%w`、单 nil 判断、单状态漏边，以及拆文件或堆无效代码凑规模。详细门禁见 [references/rules.md](references/rules.md) 的「埋错复杂度红线」。
   - 埋完不得留任何「这里故意埋错」的注释或说明。
   - **埋错自检（本地、发布前）**：直接对比 `env/` 与 `_gold/` 统计功能代码改动规模，不需要等 GitHub 分支：

     ```bash
     git diff --no-index --numstat <project>/env _gold/<name>__<record> \
       | grep -Ev '(_test\.go|README|\.md$|BENZHI|benzhi\.Dockerfile|build_benzhi_docker\.sh)' \
       | awk '{add+=$1; del+=$2; n++} END {lines=add+del; print n" files, "lines" changed lines (+"add"/-"del")"; exit !(n>=4 && lines>=20)}'
     ```

     命令只有在候选功能文件数 ≥4 且增删总行数 ≥20 时退出码为 0；任一条件不达标就重新设计，不得进入红绿校准。该命令只能做机械统计，达标后仍要人工查看 diff，剔除注释、格式化、拆文件和无效代码后再次确认有效功能代码仍 ≥20 行。最终复核仍对比本地埋错基线与 `_gold/`；**不要在跑完轨迹后的 env 上量**（env 已被测试模型改回接近 gold，diff 会很小）。
   - **难度审查必须在题面完成后、红绿校准前通过**：先运行 `difficulty_review.py init` 创建私有审查单，填写主/次机制、触发顺序、跨层范围、题面症状与测试断言映射；bugfix 还要逐个回退至少 4 个修复文件的关键改动，并确认每次定向测试重新变红。审查单只放记录根目录，不进 `env/`、不交给测试模型。
2. **在 `_gold/<name>__<record>/` 里写 gold 修复**：
   - 这是**执行本流程的模型（当前 Codex）自己写的正确修复**。
   - 不是 Claude Code 测试模型后来生成的修复；测试模型的修复只存在于轨迹及最终 `bug-<record>` 的补推 commit 中。
3. 验证 bug 可复现、gold 修复后行为正确；红绿校准见第 4 步。
4. 确保 `env/` 和 `_gold/` 都没有 `.git` 历史、remote、补丁文件或答案线索；`env/` 内尤其不能有本技能相关文件（`SKILL.md`、`AGENTS.md`、`CLAUDE.md`、`.claude`）。

## 第 4 步：题面写作

> **执行顺序**：先确定评分测试与验收命令（即第 5 步 `verify_cmds` 字段的内容），再写题面。`verify_cmds` 是收集表的独立字段，**只进 collection.json，绝不写进 user_query**；第 5 步红绿校准用 `verify_cmds` 实跑验证红/绿，校准发现命令不合适就改 `verify_cmds` 本身，题面因为是纯提示词、不含命令，通常无需同步改动。

### 写法核心：一次真人求助，不是一份报告

- **先定人设再动笔**：每条题面给说话人想一个不同的「人设」——着急赶上线的、随口一提的、话很少的、稍微絮叨的、半懂不懂的……用人设带出措辞和语气差异。**禁止从任何现成句式池里挑句子**，包括本技能旧版文档里出现过的示例句（查重脚本已将它们列为禁句）。
- 长度以 40–150 字为主，2–5 个短句；各题长短要拉开，不要每条都写成三句半。
- 收敛到**唯一目标缺陷**；根因、源码位置、文件名、函数名、测试名、修复方案一律不进题面，从「症状」写起。
- **难度必须来自真实现象，不靠堆术语**：题面必须自然写出至少 2 步有顺序的触发过程、至少 2 个相关联的用户可见症状，以及正确行为预期；这些内容必须都被同一条定向测试覆盖。不得为了显难凭空增加未复现、未断言的现象。
- **task_type 指令必须明确写进题面（红线）**：bugfix 写「请修复 xxx / 帮我修好 xxx」；diagnosis 写「不要修改代码，帮我定位 xxx / 文件先不要改，帮我查清楚原因」——说法逐条换，意思不能含糊。

### 信息要素：要素齐全，写法自由

题面要让模型能直接开工：**出了什么事 + 要它干什么**（现象、背景、环境都是为把诉求说清楚服务）。以下要素都要覆盖，但组织方式必须逐条不同：

1. **触发过程**：口语化写清先发生什么、随后在什么状态下又做了什么，至少 2 步；不能只给一个输入值和一次函数调用。
2. **关联症状**：至少 2 个真实可见且属于同一故障链的现象，例如请求已返回但后台仍重试，随后请求又继承旧错误；报错只贴 1 行最关键的。
3. **正确预期**：说清取消后应停止、后续请求应隔离、失败不应留下副作用等公开行为，不写修复方案。
4. **一句自然的背景或情绪**（每条现编，不得复用任何见过的句子）。
5. **环境交代**（目录位置 / Go 版本 / 能直接跑）可以揉进其他句子，不必单独成句。
6. **任务指令**（红线，见上）：bugfix 说清「帮我修好」，diagnosis 说清「先别改代码、帮我定位原因」。
7. **纯提示词红线**：**不写任何验收/复现/运行指令，不贴命令代码块，也不要要求对方运行、执行、重跑或验证测试**；`verify_cmds`、复现命令只进 collection.json 与红/绿证据阶段，绝不进题面。只描述真实现象、触发过程、公开预期和修复/定位诉求。

三条自由度都要用起来，这是防结构性雷同的关键：**顺序自由**（不必按现象→环境→指令排，真人不按模板说话）、**可以合并**（要素揉进同一句）、**详略自由**（有的题环境多说半句、有的题一笔带过）。

### 简洁红线

- **不写任何命令**：`go test` / `go build` / `go run` 等验收、复现、运行命令一律不出现；纯提示词即可，命令交给模型自己去想或由 `verify_cmds` 独立维护。
- 默认**不贴源码**；确有必要展示关键症状的最小复现 ≤3 行（症状代码，不是命令），能一句话说清就不用代码。
- 报错只贴关键一行，禁止整段堆栈、整段日志。
- 不排版：不用标题、加粗、列表、编号——真人求助不排版；需要并列用「；」或逗号带过。

### 防雷同（红线）

- 任意两题 `user_query` 不得有 12 字以上完全相同的连续片段；现象可以相近（同一个项目），但表达必须换说法。
- 结构也要错开：长短、句序、要素组合逐条不同，不能全是同一个骨架换词。
- 写完必须运行第 4.2 步去重检查，有红就改，改到全绿再进第 5 步。

### 去 AI 味（红线）

- 语言按普通高中生口语：短句、直白，不书面腔、不堆术语、不总结升华。
- 禁用词清单见 [references/rules.md](references/rules.md)「收集表文案书写规范」：不写「根因 / 原因是 / 问题出在 / 关键在于」等归因词；不写「不是...而是... / 也就是说 / 综上所述 / 值得注意的是 / 此外 / 从而」等 AI 套话；不用 ①② 等序号字符。
- 自检方法：写完出声读一遍，像不像同事在工位上随口跟你说的一段话；不像就重写。

- 题面写入 `<project>/prompt.txt`。

### 4.1 难度审查（红绿校准前硬门禁）

```bash
python3 <skill>/scripts/difficulty_review.py init \
  --project <project> --pattern-id <P1-P12> --task-type <bugfix|diagnosis>
# 填写 <project>/difficulty_review.json 后：
python3 <skill>/scripts/difficulty_review.py check --project <project>
```

- `primary_runtime_mechanism` 必须是运行时机制，`coupled_runtime_mechanisms` 至少 1 项且不能与主机制相同。
- 必须填写 `core_defect_review`：核心缺陷必须是真实运行时机制失效，且必须依赖调用顺序/生命周期和跨层状态传导；同时明确证明它不是索引、边界、容量、字段映射、比较符/条件分支、单 `%w`、单 nil 判断、单状态漏边或单函数输入输出变换。`failure_chain` 与 `local_fix_rejection` 各至少 20 字，不能用“改了多个文件”替代故障链证据。
- `core_defect_review.minimum_function_files` 是“根本修复实际需要”的功能文件数：bugfix 至少 4，diagnosis 至少 3；`root_cause_locations` 必须逐项列出这些真实存在的功能 Go 文件，以及各自的运行时职责和对故障链的贡献。若只需一个局部逻辑改动，直接淘汰，不得靠拆文件、格式化、注释或防御性凑代码达标。
- `trigger_sequence` 至少 2 步，`affected_layers` 至少 3 个模块/包。
- `query_evidence` 的触发与预期片段、`symptom_coverage` 的至少 2 个症状片段必须逐字来自 `user_query`；每个症状都写明目标测试中的对应断言。
- bugfix 的 `repair_ablation_checks` 至少列 4 个不同功能 Go 文件。逐个暂时回退该文件的关键修复、执行原样 `verify_cmds`，只有每次都重新变红才能填 `result: "red"`；完成后恢复 gold。
- `manual_reviewed` 只能在真实审阅代码、diff 和复跑结果后设为 `true`。脚本只校验证据结构，不能代替人工判断机制是否真实。

### 4.2 题面去重自检（必做）

每写完一道或一批题面，在本期根目录执行：

```bash
python3 <skill>/scripts/check_prompt_duplicates.py --root .
```

脚本会递归扫描各 `YYYY-MM-DD/<record>/prompt.txt` 与 `collection.json` 里的 `user_query` / `success_criteria` / `verify_cmds` 三条文案，报告被禁模板句和任意两题同一字段 >= 12 字的连续重复片段；**有红就改，改到全绿再进入第 5 步**。

## 第 5 步：红绿校准（出题自检）

必须实际完成以下校准，缺一不可：

1. `env`（埋好 bug）+ verify_cmds → **必须红**。
2. 打上 gold 修复 + verify_cmds → **必须绿**（全量测试无回归）。
3. 逐个回退 `difficulty_review.json` 中至少 4 个功能文件的关键修复 → 每次都用原样 `verify_cmds` **重新变红**；每次检查后恢复该文件，最终完整 gold 再次为绿。

- `verify_cmds` 必须是单条定向命令，形如 `go test ./path/to/pkg -run '^TestTargetBug$' -count=1`；`concurrency并发问题` 必须显式加 `-race`，脚本会按 `bug_category` 强制校验。禁止 `go test ./...`、`.`、`...`、多包、多个测试或拼接回归命令。
- 并发 bugfix 的 `repro_determinism` 必须填 `deterministic`；具体的同步原语、交错控制、测试钩子或超时边界及稳定性验收事实写入 `success_criteria`，diagnosis 还必须在 `gold_root_cause` 说明同一方案。只写“稳定复现”“多跑几次”不合格。
- `verify_cmds` 对应测试必须完整覆盖 `user_query` 描述的问题：逐项核对题面里的每个用户可见现象、触发条件和结果；任一项没有断言或无法由该命令触发，都判不合格，不能只验证相邻行为或单个局部症状。
- 稳定性校准需要红/绿各验证 ≥20 次时，重复执行同一条 `-count=1` 定向命令；不得把字段改成 `-count=20`。并发题每次都必须保留 `-race`，即至少连续实跑 20 次原样的 `-race -count=1` 命令。
- 埋错自检必须 20/20 全红，修复后必须 20/20 全绿；连跑 ≥20 遍仍不稳的标 flaky，只做 diagnosis。

### 5.1 `success_criteria` 生成规则（业务场景硬门禁）

`success_criteria` 是**这条数据自己的验收摘要**，不能是换到任何项目都成立的流程说明。写之前先从 `user_query` 和目标测试断言中摘出本题的「业务对象/输入状态、可见异常、后续影响」；字段中必须原样复用至少一个来自 `user_query` 的 4 字以上业务短语，再把实跑事实写到这些对象上。不得只写“代码状态、定向命令、定位结论、公开现象、真实复现、无回归、工作区不变”等流程词。

- **bugfix**：写清具体业务触发在埋错态 20/20 出现什么错误，修复态 20/20 恢复什么公开行为；再写回退关键改动后哪个业务现象重新出现，以及全量回归结果。每个红/绿点都要点名本题的业务对象、状态或回执，不能只写“稳定变红 / 修复后全绿 / 回退再红”。
- **diagnosis**：写清具体业务触发 20/20 出现什么异常；定位结论必须串起本题的输入或接口值、恢复/状态路径与后续跳过、污染或错误回执；最后说明工作区零改动。不能拿 bugfix 的“修复后 20 绿、全量无回归”模板来填 diagnosis。
- **逐项可核对**：字段里的业务现象必须在 `user_query` 中出现，并由 `verify_cmds` 对应测试的断言或 diagnosis 结论实际覆盖；不许为显得具体而新编业务名词或结果。
- **禁止错误范式**：`出问题的代码状态下定向命令稳定变红；定位结论说清文件、符号和现象链路；全程不改项目文件，只看公开现象和真实复现。` 这类没有业务对象、触发条件和具体异常的描述直接判不合格。
- **合格形态**：`缺失地址的定向检查 20 遍都稳定出现异常回执污染；结论需解释接口值、恢复路径和后续跳过之间的联系；工作区保持原样。` 这里的“缺失地址 / 异常回执污染 / 后续跳过”必须替换为该条数据真实存在且已经验证的业务内容，禁止把本句当模板复用。

写完由 `collection_table.py write` 和 `post_qc.py` 校验业务短语锚点及已知空泛句；脚本通过只代表基础门禁通过，仍须人工逐项对照题面、断言和真实复跑结果。

## 第 6 步：Docker 验证 + 写 BUG_REPRO + 发布 GitHub（跑轨迹前）

> 必须在跑轨迹前完成初始发布（`bug-<record>`=埋好 bug）。跑轨迹会修改 `env/`；bugfix 题在轨迹质检通过后，要把 `env/`（测试模型修复）作为新 commit 推到 `bug-<record>`，让 repo_url 最终包含测试模型 fix；diagnosis 题保持 `bug-<record>` 不动。绝不能把本地 `_gold/` 修复内容混进初始 `bug-<record>`。

### 6.1 Docker 本机验证

```bash
python3 <skill>/scripts/build_docker.py verify --root . --project <name>__<record>
```

- 验证 `env/`（bug 环境）能构建，触发错误并拿到完整错误信息（写进 BUG_REPRO.md）。
- 验证 `_gold/<project>/`（gold 环境）`go build ./...` 与 `go test ./...` 全绿。
- Dockerfile 基于官方 golang 多架构基础镜像，支持 arm64/amd64；本流程只验证当前机器平台。
- 刻意包含构建失败 Bug 时，`build_docker.py verify` 的自动验证不适用；需人工改造**仓库根目录**的 `benzhi.Dockerfile`（删掉对应的 `RUN go build` / `npm run build`）后手动在容器内复现，并把复现结果写进 `BUG_REPRO.md`。

### 6.2 写 BUG_REPRO.md（交付复现说明，不进 env）

在 `<project>/BUG_REPRO.md` 写复现说明，内容只含三类信息：

1. **Bug 是什么**
2. **如何触发**
3. **错误信息**

> 红线：写 `<project>/BUG_REPRO.md`，**不要写进 `env/`**；`env/` 是测试模型跑轨迹的 workspace，放进去会泄题。

### 6.3 发布 Bug 分支，产出 repo_url

```bash
python3 <skill>/scripts/github_project.py publish \
  --root . --repo-name <repo_name> \
  --project <name>__<record> --bug-id <bug_id>
```

脚本会：

- 把 `env/`（埋好 bug）加上 `benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md`、`.dockerignore`、`BUG_REPRO.md` 提交到 `bug-<record>`；
- **强制校验** `benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md` 均在仓库根目录；若旧版在模块子目录留下同名副本，脚本会清理后再发布；
- 输出 `repoUrl`（填 `repo_url`）；本地 `_gold/` 不提交到 GitHub。

> 注意：`BUG_REPRO.md` 只进 GitHub 交付分支，测试模型的 `env/` 里没有这个文件。

## 第 7 步：跑轨迹

- 每个 session 只产一条数据；环境目录下**从未开过** claude session。
- **红灯门禁（红线）**：开跑修复轨迹之前，必须先完成附录的红灯证据（`run_evidence_trajectories.py generate --phase red`）且达标——测试模型实测确认 bug 在基线上可复现。红灯不过说明埋错质量不行，回滚重新埋错，不得开跑正式轨迹（避免白烧一场限流额度）。`run_trajectory.py run` 会自动检查：缺少 `_evidence/red_result.json` 即拒绝开跑（`--skip-red-gate` 可跳过，不推荐）。
- **交付轨迹必须是 Claude Code 原始 session 文件（红线）**：三条轨迹（修复轨迹、红灯、绿灯）都直接取 Claude Code 自己落盘的 `~/.claude/projects/<按 cwd 转换的目录>/<session_id>.jsonl` 原始文件，不再用捕获 stdout 拼装的 stream-json 交付。`run_trajectory.py run` 与 `run_evidence_trajectories.py generate` 成功后会按 session_id 自动取出保存；stdout 捕获只用于运行时校验，另存为 `*.stream.jsonl` 备查，不交付。
- **不能暴露本技能/答案线索（红线）**：`env/` 和 `prompt.txt` 中不得出现 `SKILL.md`、`AGENTS.md`、`CLAUDE.md`、`.claude`、`BUG_REPRO.md`，也不得出现 `repo_url`、本技能名等标记；`run_trajectory.py` 会在跑之前自动检查并拒绝。
- **单轮强制约定（红线）**：全程有且仅有**一条用户输入**，内容就是 `prompt.txt` 原样；不得追加任何说明、不得输入任何斜杠命令（`/model`、`/status`、`/clear`、`/help` 等），也不得有任何“运行命令式”的额外输入。出现第二条用户消息或任意斜杠命令即不合格，必须回滚重跑。
- 无头模式轨迹最干净，并且必须把 `prompt.txt` 原文作为唯一一条 user 消息回放进轨迹，保证轨迹文件内能看到题面原文（下面的 stdout 重定向只是运行时校验用，交付文件由脚本从 `~/.claude/projects/` 取原始 session 文件）：

```bash
cd <env_dir>
python3 -c 'import json,sys; p=sys.stdin.read().strip(); print(json.dumps({"type":"user","message":{"role":"user","content":[{"type":"text","text":p}]}}, ensure_ascii=False))' \
  < ../prompt.txt > ../user_msg.jsonl
claude -p --input-format stream-json --replay-user-messages \
  --dangerously-skip-permissions --verbose \
  --output-format stream-json < ../user_msg.jsonl > ../trajectory.jsonl
```

> 正式跑轨迹用 `run_trajectory.py run`：脚本会自动完成上面的 prompt 回放、校验轨迹内恰好有一条与 `prompt.txt` 一致的用户输入，并在成功后从 `~/.claude/projects/` 取出该 session 的原始轨迹文件保存为 `<session_id>.jsonl`。

### 失败回滚重试

一次没有「成功结束处理」就必须把环境代码回滚到 base 干净态再重跑，不要在上一次残留的改动上继续。

```bash
python3 <skill>/scripts/run_trajectory.py run \
  --env <env_dir> --prompt <project>/prompt.txt \
  --output <project>/trajectory.jsonl --max-attempts 3
```

- `env/` 没有 `.git` 时，脚本用 `.base_snapshot` 做回滚基线：**已有快照（红灯门禁阶段生成）一律复用、绝不覆盖**——重跑时 env 里是上一轮模型的改动，覆盖会把污染代码拍成基线；快照不存在才从当前 env 创建。
- **重跑自动归档**：`run_trajectory.py run` 开跑前会把上一轮的主轨迹（`<uuid>.jsonl` / `*.stream.jsonl`）、`trajectory.*` 临时/失败/日志文件和绿灯产物自动迁入 `<project>/_failed_rounds/<时间戳>-rerun/`，不污染本轮；红灯证据与基线快照仍然有效，保留原位。红灯/绿灯重跑时同样各自归档上一轮产物（`-red-retry` / `-green-retry`）。
- 失败的尝试留档为 `<project>/trajectory.failN.jsonl`。
- 跑完后项目目录里的交付轨迹**文件名必须是 `<session_id>.jsonl`**（Claude Code 原始 session 文件，脚本自动取出）；三处一致：文件名 == `collection.json` 的 `session_id` == 轨迹内的 `sessionId`。同名 `*.stream.jsonl` 是运行校验副本，不交付。
- 跑轨迹期间尽量断网；脚本默认禁 `WebFetch` / `WebSearch` / `Bash(git clone *)` / `Bash(curl *)` / `Bash(wget *)`。
- **测试模型限流，全局串行（红线）**：修复轨迹、红灯、绿灯都调同一个测试模型，`run_trajectory.py run` 与 `run_evidence_trajectories.py generate` 共用全局锁 `~/.codex/go-annotation-pipeline/test_model.lock`，跑之前会自动排队等待，同一时刻只允许一条测试模型任务在跑。

## 第 8 步：轨迹质检四查

```bash
python3 <skill>/scripts/analyze_trajectory.py <project>/trajectory.jsonl
```

然后对照四项人工判定：

| 检查项 | 通过标准 |
|--------|---------|
| 完整性 | 题面到最终回复无中断；只有一轮用户输入；无任何斜杠命令（/model /status 等） |
| 结果 | bugfix：质检人独立复跑 verify_cmds 从红到绿、全量无回归；diagnosis：结论命中 gold 根因三要素 |
| 过程 | 先定位再动手、改完验证；无盲目试错、无改测试凑绿 |
| 指令遵循 | bugfix 真正修好；diagnosis 全程零代码变更 |

- 模型修复写法与 gold 修复不同是常态，**只验证公开行为**。
- bugfix 的 `verify_cmds` 必须使用 `-count=1`；稳定性校准重复执行该定向命令 20 次并全部通过，再单独执行 `go test ./...` 检查全量无回归。全量命令不得写入 `verify_cmds`。
- diff 环境目录与 base 快照，确认只改了该改的文件。

### 8.1 推测试模型 fix 到 `bug-<record>`（bugfix 题）

bugfix 题在轨迹四查通过、**且附录绿灯验收通过后**，把测试模型改好的 `env/` 作为新 commit 推到 `bug-<record>`，让 `repo_url` 最终指向测试模型的修复。**只有绿灯确认过的修复才推上 GitHub**——脚本会自动校验 `_evidence/verify_green.jsonl` 与 `verify_result.json` 存在，缺失即拒绝推送（`--force` 可跳过，不推荐）；绿灯不过说明修复实际无效，回滚重跑第 7 步，不要推：

```bash
python3 <skill>/scripts/github_project.py push-fix \
  --root . --repo-name <repo_name> \
  --project <name>__<record> --bug-id <bug_id>
```

脚本会：

- checkout `bug-<record>`，把 `env/`（测试模型修复）同步进 central repo 工作区（自动排除 `.git`、`*.jsonl`、日志等）；
- **自动重建根目录交付文件**（`benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md`、`.dockerignore`、`BUG_REPRO.md`）并校验位置——不要手动 rsync，手动 `--delete` 会把这些根目录文件一并删掉；
- 提交 `fix: <bug_id>` 并推送，输出 `repoUrl` 与 `fixCommit`；
- `env/` 与 `bug-<record>` 无差异时报错（bugfix 题测试模型应有修复改动）；task_type 为 diagnosis 时直接拒绝。

- 只同步 `env/`（测试模型修复），**禁止把本地 `_gold/` 内容带进 `bug-<record>`**，否则泄题。
- diagnosis 题跳过本步：测试模型全程零代码变更，`bug-<record>` 保持埋好 bug 的代码。
- 补推后 `repo_url` 仍填 `bug-<record>` 分支地址（`/tree/bug-<record>`），其 HEAD 即为测试模型 fix。

## 第 9 步：填收集表 + 上传轨迹 + 收尾登记

### 9.1 填收集表（21 字段）

```bash
# 生成 collection.json 模板
python3 <skill>/scripts/collection_table.py new --root . --project <name>__<record>

# 写入 21 字段后生成全局汇总表 + 本项目独立表
python3 <skill>/scripts/collection_table.py write --root . --project <name>__<record> --json <data.json>

# 任何时候全量重建两份 xlsx
python3 <skill>/scripts/collection_table.py sync --root .
```

严格按 [references/collection-table.md](references/collection-table.md) 填写，注意：

- `user_query` / `success_criteria` 两条：高中生口语、只写现象与公开预期/验收事实；`user_query` 仍是纯提示词，禁止写任何验收、复现、运行指令（包括要求“跑/执行/验证测试”），不写根因、不写「不是...而是...」等 AI 词。`success_criteria` 必须按 5.1 写入本条数据独有的业务对象、触发状态、异常结果和后续影响，禁止通用流程描述。
- `verify_result`：**不再手写**，由附录「红/绿证据轨迹」自动生成并回填 JSON——bugfix 为 `pre_fix`+`post_fix`，diagnosis 只有 `pre_fix`。
- `harness`：必须写生成轨迹的工具名 + 版本号，例如 `Claude Code CLI v2.1.233`；禁止只写 `Claude Code CLI` 或只写模型名。
- `verify_cmds`：bugfix / diagnosis 都必填；必须与红灯证据轨迹实际执行的唯一 Bash 命令和最终回复【命令】逐字符一致，bugfix 还必须与绿灯的实际命令和最终回复逐字符一致。

- `repo_url`：填 `bug-<record>` 分支地址；bugfix 题在 8.1 补推测试模型 fix 后，该分支 HEAD 即测试模型修复；diagnosis 题该分支仍是埋好 bug 的代码。
- `是否同步飞书`：**本技能不填写**，留空。
- `做题人`、`创建人` 由用户本人填写；`质检结果`、`质检备注` 留给质检人。
- **必填对照**：bugfix 必填 `verify_cmds` + `verify_result`（`pre_fix`+`post_fix`）；diagnosis 必填 `verify_cmds` + `gold_root_cause` + `verify_result`（仅 `pre_fix`）。
- 同一个 bug 不能同时出 bugfix 和 diagnosis 两条。

### 9.2 上传轨迹到 COS 并回填 trajectory

上传接口为 `https://upload.jzxhnh.com/api/upload`，需带登录 cookie `cos_uploader_sid`。cookie 已在「首次配置」写入本地配置（`~/.codex/go-annotation-pipeline/config.json`），上传脚本会自动读取；也支持环境变量 `COS_UPLOADER_SID` 或 `--cookie` 临时覆盖。若本地配置或环境变量中有 `COS_USERNAME` / `COS_PASSWORD`（或通过 `configure.py setup --cos-username ... --cos-password ...` 保存过账号密码），上传返回登录失效时会自动登录刷新 `cos_uploader_sid`，写回本地配置，并重试一次。登录账号以收集表「注意事项」页为准。

```bash
# 上传并回填（cookie 已配置，无需每次手动 export）
python3 <skill>/scripts/upload_trajectory.py upload --root . --project <name>__<record> --sync
```

cookie 过期或未配置时，重新配置；传入账号密码会立即登录获取最新 `cos_uploader_sid`，并保存为后续自动刷新凭据：

```bash
# 直接给 cookie
python3 <skill>/scripts/configure.py setup --cos-cookie <新的cos_uploader_sid>

# 或用账号密码自动登录拿 cookie（账号见收集表注意事项页）
python3 <skill>/scripts/configure.py setup --cos-username <账号> --cos-password <密码>
```

上传完成后再 `collection_table.py sync --root .`，保证汇总表与项目独立表里的 `trajectory` 列是最终 COS 链接。

### 9.3 收尾登记

1. `workspace.py set --root . --project <name>__<record> --state done`
2. 登记全局已用仓库/项目（`--github-url` 用 `ensure` 输出的 `repoUrl` 完整地址，即真实项目名的仓库地址）：

```bash
# 优先用 GitHub 地址
python3 <skill>/scripts/repo_registry.py register <repo|url> --source auto \
  --github-url <github_url> --local-path <local_path> \
  --project <name>__<record> --note "bug <bug_id>"
python3 <skill>/scripts/repo_registry.py sync --root .
```

3. 确认已就位：`<project>/<session_id>.jsonl`、`<project>/collection.json`（含 COS 链接）、`<project>/收集表_<project>.xlsx`、GitHub 上的 `repo_url`（bugfix 题的分支已含测试模型修复 commit）。

### 9.4 后置质检（交付前硬校验，交付前必跑）

整期所有记录都完成第 9.1–9.3 步后，在本期根目录执行：

```bash
python3 <skill>/scripts/post_qc.py --root .
```

只读、不改产物。逐条输出 ✅/❌，最终汇总；有任何一条不合格就退出码非 0。校验项：

1. **build**：`env` 与 `_gold` 都能 `go build ./...`（项目能编译）；
2. **scope**：bugfix 的 gold 修复相对埋错基线同时满足功能代码文件数 ≥4、增删总行数 ≥20；
3. **red**：埋错基线 `.base_snapshot` 跑 `verify_cmds` 必须红（bug 真实可复现）；
4. **green**：`_gold` 跑同样命令必须绿；
5. **files**：`<session_id>.jsonl` / `BUG_REPRO.md` / `collection.json` 齐全；
6. **fields**：`collection.json` 必填字段齐全（bugfix 必填 `verify_cmds`+`verify_result`；diagnosis 必填 `verify_cmds`+`gold_root_cause`+`verify_result`）；
7. **evidence**：`verify_result` 结构、URL、session_id 校验；
8. **diagnosis**：diagnosis 题 `env` 与 `.base_snapshot` 零差异（零代码改动）；
9. **coverage**：`verify_cmds` 只含唯一目标包、精确测试名和 `-count=1`；并发题还必须带 `-race`，且确定性复现方案已写入规定字段；红灯失败测试真实存在，并人工逐项确认该测试完整覆盖 `user_query` 的全部现象与触发条件；
10. **difficulty**：`difficulty_review.json` 的运行时机制、跨层触发、至少两个题面症状覆盖和至少四个修复文件逐一回退证据齐全。

> 甲方抽检红线「项目能编译运行 / 运行时机制难度达标 / 修复规模达标 / bug 真实可复现 / verify_cmds 定向且完整覆盖 user_query / verify_cmds 红绿通过」全部落在这 10 项里；交付前必须全绿。

## 腾讯文档粘贴注意

在腾讯文档粘贴 `user_query` / `verify_cmds` / 长文本时，**先点击进入目标输入框，再执行粘贴**。如果未进入输入框直接粘贴，带换行、特殊符号的内容容易异常跳行、数据截断丢失，导致录入出错。

## 附：红/绿证据轨迹（bugfix=红+绿，diagnosis=仅红；回填 verify_result）

用目标模型（Claude Code）产出只验证、不改代码的证据轨迹，上传 COS 后回填 `verify_result`。**分两个阶段、穿插在主流程里执行**（脚本按 `collection.json` 的 `task_type` 与 `--phase` 自动选择）：

- **bugfix**：红灯（第 6→7 步之间，门禁）+ 绿灯（第 8→9 步之间，验收）（`pre_fix` + `post_fix`）。
- **diagnosis**：只产红灯（仅 `pre_fix`，不出现 `post_fix`），红灯阶段即完成上传与回填。

1. **红灯验证轨迹（pre_fix，开考门禁）**：在**跑修复轨迹之前**，于埋错基线（`.base_snapshot`，不存在时自动从未跑轨迹的 `env/` 生成）上运行验证命令，结论必须含「BUG 存在」——测试模型实测证明 bug 真实可复现。**红灯不过 = 埋错质量不合格，回滚重新埋错，不得开跑第 7 步**。
2. **绿灯验证轨迹（post_fix，仅 bugfix）**：在第 8 步四查通过后、**8.1 push-fix 之前**，于**修复轨迹改好的 `env/`**（测试模型自己的修复成果）上运行带 `-count=1` 的定向 `verify_cmds`，结论必须含「已修复」——证明测试模型的修复确实有效，绿灯通过后才把修复推上 GitHub。**绿灯验证的不是 `_gold`**（gold 的有效性在第 5 步红绿校准已由出题方验证）。

> 模型红线：红/绿都用 `claude`（目标模型 `model_hub/glm-52-coding`）；**不生成 gold 修复轨迹**，当前 Codex 的正确修复只保存在本地 `_gold/` 供校准和质检。
> diagnosis 的红灯复现命令同样来自 `collection.json` 的 `verify_cmds`；`--verify-cmds` 只允许临时校准时覆盖，正式交付前必须写回 collection，且与证据轨迹实际执行的唯一 Bash 命令、最终回复【命令】逐字符一致。不再提供全量测试默认值。

### 统一验收标准（硬门禁）

- **红灯达标**：结论含「BUG 存在」且不含「BUG 不存在」，并且 `red_env` 与埋错基线零差异（没动代码）。不达标 → **回滚 env 重新埋错（红）**，再重跑。
- **绿灯达标（仅 bugfix）**：结论含「已修复」且不含「未修复」，并且 `green_env` 与修复后的 `env/` 零差异（验证时没动代码）。不达标 → 说明该修复轨迹实际无效（质检结论有误或测试 flaky），**回滚重跑第 7 步修复轨迹并重新质检**，再重跑本命令。
- 每条证据最多自动重试 3 次；每次重试前从基线 / 修复后 `env/` 重新 rsync 验证环境。

### 全局串行（红线）

red / green / 修复轨迹都调用同一个限流测试模型，必须全局串行。`run_evidence_trajectories.py generate` 与 `run_trajectory.py run` 共用全局锁 `~/.codex/go-annotation-pipeline/test_model.lock`，自动排队等待（`--lock-timeout 0` 表示一直等）。

### 生成并回填 verify_result

```bash
# 阶段 1（第 6→7 步之间）：红灯门禁——bug 必须实测可复现，不过则重新埋错
python3 <skill>/scripts/run_evidence_trajectories.py generate \
  --root . --project <name>__<record> --date <YYYY-MM-DD> --phase red

# 阶段 2（第 8 步质检通过后，仅 bugfix）：绿灯验收测试模型修复成果，
# 上传红+绿两条并把 pre_fix/post_fix JSON 回填 collection.json、重建 xlsx
python3 <skill>/scripts/run_evidence_trajectories.py generate \
  --root . --project <name>__<record> --date <YYYY-MM-DD> --phase green

# diagnosis：只有红灯阶段，使用 collection.json 的 verify_cmds；红灯即完成上传回填
python3 <skill>/scripts/run_evidence_trajectories.py generate \
  --root . --project <name>__<record> --date <YYYY-MM-DD> --phase red

# 缺省 --phase auto：没跑过红灯就跑红灯，跑过就进入绿灯阶段
# 只生成不上传（verify_result 不回填，仅本地预览）
python3 <skill>/scripts/run_evidence_trajectories.py generate \
  --root . --project <name>__<record> --date <YYYY-MM-DD> --skip-upload

# 独立校验已有 verify_result（结构 / result 值 / URL 可访问 / session_id 匹配）
python3 <skill>/scripts/run_evidence_trajectories.py validate \
  --root . --project <name>__<record> --date <YYYY-MM-DD>
```

回填到 `verify_result` 的 JSON 结构：

```json
// bugfix
{
  "pre_fix":  {"trajectory_url": "https://cos.xxxx.com/xxx.jsonl", "session_id": "xxx", "result": "red"},
  "post_fix": {"trajectory_url": "https://cos.xxxx.com/yyy.jsonl", "session_id": "yyy", "result": "green"}
}

// diagnosis：只有 pre_fix
{
  "pre_fix":  {"trajectory_url": "https://cos.xxxx.com/xxx.jsonl", "session_id": "xxx", "result": "red"}
}
```

产物统一写入 `<record>/_evidence/`：

- `verify_red.jsonl`：红灯轨迹（bugfix / diagnosis 都产），Claude Code 原始 session 文件。
- `verify_green.jsonl`：绿灯轨迹（仅 bugfix），Claude Code 原始 session 文件。
- `verify_red.stream.jsonl` / `verify_green.stream.jsonl`：stdout 捕获的运行校验副本，不交付。
- `red_result.json`：红灯阶段的中间结果（session_id + 上传链接），供绿灯阶段回填时读取。
- `verify_result.json`：pre_fix / post_fix JSON（人工可读、机器校验用）。
