---
name: yelM5-红绿轨迹-最新-go-annotation-pipeline-0814
description: 生产「Go 语言 × Bugfix/问题排查」模型训练数据的完整流水线（0-1 自建项目埋 bug，GitHub repo_url 分支交付）。当用户需要出题、埋 bug、写题面、红绿校准、跑 Claude Code 轨迹、质检轨迹或填写数据收集表时使用。首次使用或说「配置技能/初始化配置」时，先运行 scripts/configure.py。
---

# Go 标注数据生产流水线（0-1 自建项目版）

一条合格数据 = 可还原的题目 + 一条验证通过的好轨迹（单轮对话）。

硬性规则（红线、防泄漏清单、缺陷分类、判定标准）见 [references/rules.md](references/rules.md)，出题和质检前必读。
收集表 21 字段口径见 [references/collection-table.md](references/collection-table.md)。
禁止项目与功能点见 [references/forbidden-domains.md](references/forbidden-domains.md)，项目选型、功能点设计和题面落笔前都必须审查。

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
3. **repo_url 按 task_type 取分支**：bugfix 填 `bug<record>_green`，其 green 从 G1 追加 G2，并配套 orphan red R1；diagnosis 只创建 orphan `bug<record>_red` 单提交，`repo_url` 直接填该 red 地址，禁止创建 green。
4. GitHub 去重身份**优先用 GitHub 地址，其次用本地路径**。
5. 交付只提交 GitHub `repo_url` 分支地址；不再提交修复 commit 字段，不打 zip、不截图。
6. Dockerfile 要支持 arm64/amd64，但本流程**实际只验证当前机器平台**。
7. **项目和交付分支禁止包含 `BUG_REPRO.md`**。每条记录用 `project_summary.txt` 保存一句包含 Go 与项目类型的简介，发布时写到 `BENZHI_README.md` 第一行。
8. **Docker 验证和 G1 发布必须在跑轨迹前完成；G2/R1 只能在轨迹、私有绿灯和全量回归通过后创建**。不得远程发布 `main`、干净基座、gold 或其他可用于反推答案的分支。
9. **GitHub 仓库名用真实项目名，长度 3-5 个英文单词**：用「领域 + 用途 + 类型」拼成描述性名字（如 `sensor-telemetry-ingestion-service`），既具体又不至于重名；不加 `go-` 前缀、不加随机码、不出现 `test`/`fix` 等字样；本地项目名用领域命名，别用 `forex` 这类泛化名。
10. **`verify_cmds` 对 bugfix / diagnosis 都必填，且只能是目标 Bug 的定向复现命令**：明确写出唯一目标包、精确测试名和 `-count=1`（并发类加 `-race`），禁止 `go test ./...`、通配包、当前目录、多包或拼接全量回归；命令对应的测试必须完整覆盖 `user_query` 描述的全部现象与触发条件。红、绿证据轨迹都必须**只实际执行一次**这条命令，实际 Bash 调用、最终回复【命令】和正式填表的 `verify_cmds` 必须逐字符完全相同，空格、路径写法、引号、参数顺序均不得变化；bugfix 校验红+绿，diagnosis 校验红。
11. **Bug 难度由真实故障链决定**：至少涉及 1 个 Go 运行时机制、2 个相关模块/包，并依赖调用顺序、并发交错、请求生命周期或状态转换才能完整触发。文件数和增删行数只记录为辅助信息，不作为通用硬门禁。
12. **禁止用规模冒充难度**：纯索引/边界/容量计算、单字段映射、单比较符、单 `%w`、单 nil 判断、单状态漏边等局部错误通常不合格；但是否合格以真实定位难度、故障传播和测试证据判断，不要求人为扩写到固定文件数或行数。
13. **禁止项目/功能点门禁优先于埋错和难度设计**：查账账务与订单类为最高优先级禁区，完整清单见 [references/forbidden-domains.md](references/forbidden-domains.md)。项目总体允许不代表每个功能点都允许；任一单条功能点命中禁区就立即换题，不得先埋错再靠改写 `user_query` 规避。
14. **`bug_id` 固定为记录目录主体 + `-` + 三位 record**：例如 `16-exam-system【10】__001` 对应 `16-exam-system【10】-001`，保留目录名原始字符。

## 流程总览（编号与下文章节一一对应）

1. **选题准备** → 0-1 项目来源、全局去重、创建空 GitHub repo
2. **创建记录 workspace** → `workspace.py init / new-project`，每仓建 001–030 记录目录
3. **环境构建** → 埋 bug（env）+ gold 修复（_gold）+ 本地量改动规模
4. **题面写作** → 产出 user_query、task_type（含 4.1 难度审查、4.2 去重自检）
5. **红绿校准** → 产出 verify_cmds、success_criteria、gold_root_cause
6. **Docker 验证 + 项目简介 + 发布 GitHub**（跑轨迹前）→ 产出 repo_url
7. **无测试跑修复轨迹**（Claude Code 干净 session）→ 脚本用系统临时隔离副本运行，副本中没有任何 `*_test.go`，模型只收到 `prompt.txt` 原文
8. **轨迹质检四查** → `cheat/suspect/clean` 防作弊审计；仅 bugfix 继续私有绿灯并 `finalize` 生成 G2/R1
9. **填收集表 + 上传轨迹 + 收尾登记** → 产出 collection

> 「附:红/绿证据轨迹」中红灯在第 6→7 步之间；仅 bugfix 在第 8 步四查通过后、8.1 finalize 之前跑绿灯。G2/R1 在 finalize 之前不得存在。

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
      env/                      #   埋好 bug 的业务代码（脚本由此生成无测试隔离副本）
      evaluator/                #   私有目标测试（镜像项目相对路径；修复轨迹不可见）
      prompt.txt                #   题面
      project_summary.txt       #   单行项目类型简介，生成 BENZHI_README.md 第一行
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

批次阶段（`status.json.pipeline.stage`）依次为：

```text
prepared -> preflight_passed -> g1_published -> red_passed -> main_running
-> main_accepted -> green_passed -> finalized -> uploaded -> done
```

## 常用脚本（均在本期根目录下执行）

| 脚本 | 作用 |
|---|---|
| `python3 <skill>/scripts/repo_registry.py` | 全局已用仓库/项目注册表：`check` 查重 / `register` 登记 / `list` / `sync`；一个 repo 最多 30 条 |
| `python3 <skill>/scripts/workspace.py` | 工作区与项目状态：`init` / `new-project` / `list` / `set` / `reject` / `purge` |
| `python3 <skill>/scripts/pick_bug_pattern.py` | 随机抽取深度埋错模式（P1–P12）：`--category` 过滤 / `--exclude` 排除同 repo 已用 / `--list` |
| `python3 <skill>/scripts/difficulty_review.py` | 私有难度审查单：`init` 创建模板 / `check` 校验运行时机制、跨层触发、题面覆盖和逐文件回退证据 |
| `python3 <skill>/scripts/contract_coverage.py` | evaluator 契约覆盖：`init` 提取每条直接失败断言 / `check` 校验题面触发、预期、success_criteria、难度证据和精确输入边界映射 |
| `python3 <skill>/scripts/github_project.py` | GitHub public repo 创建与 orphan 分支管理：`ensure` / `publish` / `finalize` |
| `python3 <skill>/scripts/collection_table.py` | 收集表填表数据：`new` / `write` / `sync` / `list` |
| `python3 <skill>/scripts/run_trajectory.py` | 跑轨迹与失败回滚 |
| `python3 <skill>/scripts/trajectory_guard.py` | 跑前私有测试门禁 + 轨迹越界/测试接触审计 |
| `python3 <skill>/scripts/trajectory_acceptance.py` | 正式轨迹后的本地并发验收（由 `run_trajectory.py` 调用） |
| `python3 <skill>/scripts/run_evidence_trajectories.py` | 红/绿证据轨迹：`generate` 生成上传并回填 verify_result / `validate` 校验 |
| `python3 <skill>/scripts/analyze_trajectory.py` | 轨迹客观检查 |
| `python3 <skill>/scripts/upload_trajectory.py` | 上传轨迹到 COS 并回填链接 |
| `python3 <skill>/scripts/build_docker.py` | Docker 本机验证（不打包、不截图） |
| `python3 <skill>/scripts/configure.py` | 首次配置向导：`check` 自检 / `setup` 写配置 / `show` 查看 / `reset-registry` 清空已用清单 |
| `python3 <skill>/scripts/domain_guard.py` | 禁止项目/功能点最低门禁：建仓和埋错前检查项目描述、仓库名、README 与候选功能点；通过后仍须人工语义审查 |
| `python3 <skill>/scripts/post_qc.py` | **后置质检**：默认核对输入指纹绑定的运行证据、隐私、文件、字段、轨迹和仓库拓扑，不重复执行 build/红绿命令；仅排障时加 `--recheck-runtime` |
| `python3 <skill>/scripts/batch_pipeline.py` | 推荐批次入口：`preflight` / `run` / `resume` / `status`，负责一次性预检、状态恢复、自动流转、本地并发和批末集中同步 |
| `python3 <skill>/scripts/batch_preflight.py` | 单独批次预检：工具链 canary、输入指纹、无原始测试 evaluator 编译、diagnosis 验收器自检、20/20 红绿校准和目标断言到达；逐文件回退按题目需要选用 |

## 批次编排（多条记录默认使用）

所有记录的题面、env、gold、evaluator、难度审查和收集字段准备完成后，从本期根目录运行：

```bash
python3 <skill>/scripts/batch_pipeline.py preflight --root . --workers 3
python3 <skill>/scripts/batch_pipeline.py run --root . --workers 3
python3 <skill>/scripts/batch_pipeline.py resume --root .
python3 <skill>/scripts/batch_pipeline.py status --root .
```

批处理默认用 `--workers 3` 执行本地检查、用 `--model-workers 8` 限制目标模型；记录流水线线程数至少等于模型并发数，确保单批次可实际使用 8 个模型槽位。需要降低压力时可设为 1-7，其中 `--model-workers 1` 恢复模型全局串行。每条记录独立执行 G1 发布→红灯→正式轨迹→绿灯→finalize，不再等待整批记录到达同一阶段。同一 staging Git 仓库的写操作按仓库加跨进程锁，不同仓库可并行。

- `status.json.pipeline` 是批次阶段、输入指纹和 `attempt_history` 的唯一事实源；原有 `state` 继续管理 workspace 生命周期。
- 预检按批次中每个 `go.mod` 版本分别验证工具链切换、`-race`、测试二进制和 `LC_UUID` 能力，并发执行本地 Go 检查（默认 3 条），Docker 单独限流（默认 1 条）。`LC_UUID` 只在 evaluator 明确依赖它时硬失败；无此依赖时仍记录能力结果，由真实红灯断言到达门禁阻断工具链假红。只有输入指纹未变时才复用结果。
- 红灯、正式轨迹、绿灯共用跨进程测试模型槽位，默认最多 8 路并发；同一记录仍严格按红灯→正式轨迹→绿灯执行。瞬时进程错误按上限重试；evaluator 编译、轨迹守卫和回归失败立即停止；同一 diagnosis/private_verify 失败签名第二次出现即熔断，修改输入后必须重新 preflight。
- 同一记录的整条流水线有跨进程记录锁，避免两个批次重复更新其 `status.json` 和证据产物；Git 的 ensure/publish/finalize 另有按 staging repo 的跨进程锁，只锁 checkout/commit/push 临界区，不占用模型执行时间。
- diagnosis 在 PreToolUse 就禁止 Edit/Write 和明确 Bash 写操作，跑后仍做零 diff 与「文件/符号/机制」根因语义验收。
- 证据轨迹先本地生成，通过质量门禁后并发上传；Excel 和注册表各只在批末重建一次，并分别用跨进程写锁保护。
- `--skip-upload` 会留下完整本地产物；后续 `resume` 只补上传和同步，不重跑测试模型。

## 第 1 步：选题准备（0-1 项目 + 去重）

### 1.0 禁止领域预审（最高优先级，任何编码前）

先完整阅读 [references/forbidden-domains.md](references/forbidden-domains.md)，对三层分别判断：项目的核心用户/实体/主流程、准备埋错的具体业务功能点、预计写入题面的触发与预期。任一层命中就淘汰；项目总体允许，但局部功能命中也不能使用。

查账 / 账务类和订单类优先级最高。其余禁区包括指定游戏与图形模拟、RBAC / 仓库库存 / OA / CRM 等平台系统、本地桌面工具，以及指定数据看板和前端页面。**语义判定优先于关键词**：改实体名、删“账面”或把“订单”换成“任务”不能挽救同一业务流程。

在生成项目或接收本地项目后，先运行最低关键词门禁：

```bash
python3 <skill>/scripts/domain_guard.py \
  --text '<项目的核心用途>' \
  --text '<准备埋错的具体业务功能点>' \
  --project <项目目录> --repo-name <repo_name>
```

- 命中任何项：立即放弃候选项目或功能点，不创建记录、不埋错、不写 gold、不发布仓库。
- 脚本通过：只表示未发现明确关键词；必须再人工确认业务实质不在禁区。
- 若功能点已经命中，不能仅重写 `user_query`、`success_criteria` 或测试名后继续交付。

### 1.1 确定项目来源

0-1 项目来源由用户指定：可以是项目生成提示词，也可以是本地项目目录。若给的是提示词，先由本流程模型生成完整可运行的 Go 项目；若给的是本地目录，直接使用。

- **0-1 项目必须保留/补写 `README.md`**（项目说明、目录结构、运行与测试命令、环境变量），交付时随代码进各 bug 的 orphan green/red 分支；不发布可比较的 `main` 基座。

生成/拿到项目后，先确认三件事：

```bash
cd <项目目录>
go build ./...
go test ./...
```

`go build ./...` 必须通过。此时是**干净、无 bug** 的基线。

随后再用 `domain_guard.py --project ... --repo-name ...` 检查实际项目名与根 README；未通过不得执行 1.3 建仓。`github_project.py ensure` 会在任何 GitHub 写操作前复查并拒绝明确命中。

### 1.2 全局去重（红线，先做）

```bash
# 优先用 GitHub 地址；没有 GitHub 地址时用本地绝对路径
python3 <skill>/scripts/repo_registry.py check <repo|url|local-path> --source auto \
  --github-url <github_url> --local-path <local_path>
```

- 全局注册表唯一事实源是 `~/.codex/go-annotation-pipeline/used-repositories.json`（个人目录，随技能升级/分享不受影响）；同目录 `used-repositories.md` 是自动生成的人类可读镜像，勿手改。旧版存在技能目录 `references/` 内的清单会在首次运行时自动迁移过来（原文件保留为 `*.migrated` 备份）。
- 去重身份按优先级：**GitHub 地址优先，本地路径其次**。
- 一个 repo 最多 30 条；达到 30 条即永久排除，不得换 bug / 换任务类型继续出。总需求超过 30 条时新建下一个独立 0-1 项目和 GitHub 仓库，不得复制同一本地项目或更换地址绕过上限。

### 1.3 建立空 GitHub 仓库与 staging repo

```bash
python3 <skill>/scripts/github_project.py ensure \
  --root . --repo-name <repo_name> --local-path <项目目录>
```

脚本会：

- 用 `~/.codex/pg-code/github-context.json` 的 GitHub 凭据/作者自动创建 **public** repo（审核方需要能访问）；
- **GitHub 仓库名直接用真实项目名，3-5 个英文单词**（`--repo-name` 的 slug，如 `sensor-telemetry-ingestion-service`），避免重名；不加 `go-` 前缀、不加随机码、不出现 `test`/`fix` 等字样。
- 远程保持空仓，**不创建、不推送 `main` 或干净基座**；
- 本地 staging repo 保存在 `_repos/<repo_name>/`，只用于构造各个互不相连的 orphan 交付分支；
- Docker 交付文件在第 6 步发布 G1 时写入每条分支根目录。

GitHub 分支模型：

```text
bugfix:    bug<record>_green  G1 orphan Bug 单提交 -> 收题后 G2（模型修复+验收测试） -> repo_url
           bug<record>_red    R1 orphan 单提交（G1 业务树+与 G2 完全相同的验收测试）
diagnosis: bug<record>_red    orphan Bug 单提交 -> repo_url（不创建 green）
```

bugfix 的每对 green/red 无共同祖先；不同 bug 之间也无共同祖先。当前 Codex 的正确修复只保存在本地 `_gold/<project>/`，不创建或推送远程 gold 分支。

> 旧仓库只要已推送 `main`、干净基座或旧 `bug-*` 分支，就不得继续用于新数据。`publish` 会拒绝这类远程；必须换新的 0-1 项目和 GitHub 仓库，不得删历史后伪装迁移。

## 第 2 步：创建记录 workspace

初始化工作区：

```bash
python3 <skill>/scripts/workspace.py init --root .
```

创建记录（`--count` 是单仓数量，范围 1–30）：

```bash
python3 <skill>/scripts/workspace.py new-project --root . --source local \
  --repo <repo_name> --local-path <项目目录> --count <1-30> \
  --project-summary '<包含 Go 与项目类型的一句话简介>'
```

- 每条记录目录是 `YYYY-MM-DD/<name>__<record>/`，单仓 record=001–030。
- `env/` 是待埋 bug 的 workspace；`_gold/<name>__<record>/` 是 gold 答案区，模型不可达。
- `project_summary.txt` 由 `new-project` 自动写入，必须只有一行，包含 `Go` 和明确类型（如 CLI、命令行工具、服务、API、系统或库）。例如：`基于 Go 实现的停车场管理 CLI 项目，一款命令行工具，完成车位录入、车辆进出登记与费用核算。`
- 建完后对每条记录 `workspace.py set --root . --project <name>__<record> --state selected`。

### 2.1 超过 30 条时拆仓（硬规则）

- 收到总数 `N` 后，先计算仓库数 `ceil(N / 30)`，按顺序分片；前面的仓库各 30 条，最后一个仓库放余数。例如：10 条=`[10]`，30 条=`[30]`，31 条=`[30,1]`，60 条=`[30,30]`，65 条=`[30,30,5]`。
- 每个分片必须对应一个**不同的 0-1 项目、不同本地路径和不同 GitHub 仓库**；分别执行一次 `github_project.py ensure`，再对该仓执行 `workspace.py new-project --count <本仓条数>`。
- 每个仓库的记录编号都从 `001` 重新开始，最多到 `030`；不得向单次 `workspace.py new-project` 传入大于 30 的 `--count`。
- 仓库名仍按真实业务命名，不能只在同一名称后加序号，也不能复制同一份源码来规避每仓 30 条上限。

## 第 3 步：环境构建（埋 bug + gold 修复）

对每条记录：

1. **在 `<project>/env/` 里埋一个 bug**：
   - **先审功能点再设计机制**：用 `domain_guard.py --text '<业务功能点 + 触发流程 + 用户可见结果>'` 检查，并按 forbidden-domains.md 人工复核。命中禁区时更换功能点；不得在禁用功能上继续选择 P1-P12、写测试或埋错。
   - 按 bug_category 选型。bug_category 只允许以下取值：`concurrency并发问题` / `slice相关问题` / `error异常错误` / `nil相关问题` / `context相关问题` / `defer相关问题` / `其他问题`；优先 concurrency / nil / slice / error / context / defer 里多步定位、强模型也会栽的缺陷。
   - **埋法从深度模式库随机抽取**：`python3 <skill>/scripts/pick_bug_pattern.py`（P1–P12，详细埋法见 [references/bug-patterns.md](references/bug-patterns.md)）；单仓达到 13–30 条时允许轮换复用模式骨架，但同一模式必须换业务层次、埋点组合、触发条件和用户可见症状，不能复刻同一个 bug。优先轮完 P1–P12 后再复用，`--exclude` 只排除近期已用模式。
   - **硬性红线（真实故障链）**：核心缺陷必须涉及运行时机制，跨至少 2 个相关模块/包，并依赖时序、生命周期或状态转换触发。文件数和增删行数只用于辅助审查；不得拆文件或堆无效代码制造表面规模。详细标准见 [references/rules.md](references/rules.md) 的「埋错复杂度红线」。
   - 埋完不得留任何「这里故意埋错」的注释或说明。
   - **埋错自检（本地、发布前）**：直接对比 `env/` 与 `_gold/` 统计功能代码改动规模，不需要等 GitHub 分支：

     ```bash
     git diff --no-index --numstat <project>/env _gold/<name>__<record> \
       | grep -Ev '(_test\.go|README|\.md$|BENZHI|benzhi\.Dockerfile|build_benzhi_docker\.sh)' \
       | awk '{add+=$1; del+=$2; n++} END {print n" functional files, "add+del" changed lines (+"add"/-"del")"}'
     ```

     该命令只用于观察改动规模，不再以固定文件数或行数决定通过。人工查看 diff，排除注释、格式化、拆文件和无效代码，并确认修复确实对应故障链。最终复核仍对比本地埋错基线与 `_gold/`；**不要在跑完轨迹后的 env 上量**。
   - **难度审查必须在题面完成后、红绿校准前通过**：先运行 `difficulty_review.py init` 创建私有审查单，填写机制、触发顺序、跨层范围、题面症状与测试断言映射。对确实分布在多个文件的修复，可填写 `repair_ablation_checks` 并逐文件回退留证，但不作为所有 bugfix 的统一要求。
   - **目标测试只能写入 `<project>/evaluator/`**，并按它最终进入 Repo 的相对路径存放。G1 和正式轨迹快照中不得有任何测试文件、`test/tests/testdata/evaluator` 夹具目录或常见 test/spec 文件；验收测试只在收题后的 G2/R1 出现。
2. **在 `_gold/<name>__<record>/` 里写 gold 修复**：
   - 这是**执行本流程的模型（当前 Codex）自己写的正确修复**。
   - 不是 Claude Code 测试模型后来生成的修复；测试模型的修复只存在轨迹和收题后的 G2 中。
3. 验证 bug 可复现、gold 修复后行为正确；校准时只在临时副本中注入 `evaluator/`，不得把测试回写到 `env/` 或 `_gold/`。
4. 确保 `env/` 和 `_gold/` 都没有 `.git` 历史、remote、补丁文件或答案线索；`env/` 内尤其不能有本技能相关文件（`SKILL.md`、`AGENTS.md`、`CLAUDE.md`、`.claude`）。

## 第 4 步：题面写作

> **执行顺序**：先确定评分测试与验收命令（即第 5 步 `verify_cmds` 字段的内容），再写题面。`verify_cmds` 是收集表的独立字段，**只进 collection.json，绝不写进 user_query**；第 5 步红绿校准用 `verify_cmds` 实跑验证红/绿，校准发现命令不合适就改 `verify_cmds` 本身，题面因为是纯提示词、不含命令，通常无需同步改动。

### 写法核心：一次真人求助，不是一份报告

- 使用自然、具体的真人求助口吻；不要套用现成句式池。长度和句数按说明问题所需决定，不设固定区间。
- 收敛到**唯一目标缺陷**；根因、源码位置、文件名、函数名、测试名、修复方案一律不进题面，从「症状」写起。
- **难度必须来自真实现象，不靠堆术语**：写清足以稳定触发问题的过程、主要用户可见症状和正确预期；所有描述必须被同一条定向测试覆盖。不得为了数量凭空增加未复现、未断言的现象。
- **task_type 指令必须明确写进题面（红线）**：bugfix 写「请修复 xxx / 帮我修好 xxx」；diagnosis 写「不要修改代码，帮我定位 xxx / 文件先不要改，帮我查清楚原因」——说法逐条换，意思不能含糊。

### 信息要素：要素齐全，写法自由

题面要让模型能直接开工：**出了什么事 + 要它干什么**（现象、背景、环境都是为把诉求说清楚服务）。以下要素都要覆盖，但组织方式必须逐条不同：

1. **触发过程**：口语化写清复现所必需的操作和状态顺序；简单输入足以触发时无需人为扩写步骤。
2. **关联症状**：写出目标缺陷真实产生的主要可见现象；存在多个相关症状时一并说明，不要求凑数量。
3. **正确预期**：说清取消后应停止、后续请求应隔离、失败不应留下副作用等公开行为，不写修复方案。
4. **必要背景**：只写帮助理解问题的上下文，不要求添加情绪或废话。
5. **环境交代**只需自然写“当前项目就可以了”；禁止写 Go 版本号或 Go 工具链版本。是修复还是只定位，另用任务指令说清。
6. **任务指令**（红线，见上）：bugfix 说清「帮我修好」，diagnosis 说清「先别改代码、帮我定位原因」。
7. **纯提示词红线**：**不写任何验收/复现/运行指令，不贴命令代码块，也不要要求对方运行、执行、重跑或验证测试**；`verify_cmds`、复现命令只进 collection.json 与红/绿证据阶段，绝不进题面。只描述真实现象、触发过程、公开预期和修复/定位诉求。

三条自由度都要用起来，这是防结构性雷同的关键：**顺序自由**（不必按现象→环境→指令排，真人不按模板说话）、**可以合并**（要素揉进同一句）、**详略自由**（有的题环境多说半句、有的题一笔带过）。

### 简洁红线

- **不写 Go 版本环境**：`user_query` / `prompt.txt` 中禁止出现“Go 1.23”“Go 版本为 1.23”“1.23 版 Go”等描述；不论是否带空格、`v` 前缀或补丁版本都不得写。环境交代正常写“当前项目就可以了”，Go 版本继续只在 `go_version` 字段和内部验证中维护。
- **不写任何命令**：`go test` / `go build` / `go run` 等验收、复现、运行命令一律不出现；纯提示词即可，命令交给模型自己去想或由 `verify_cmds` 独立维护。
- 默认**不贴源码**；确有必要展示关键症状的最小复现 ≤3 行（症状代码，不是命令），能一句话说清就不用代码。
- 报错只贴关键一行，禁止整段堆栈、整段日志。
- 不排版：不用标题、加粗、列表、编号——真人求助不排版；需要并列用「；」或逗号带过。

### 防雷同（红线）

- 避免整批套用同一个模板。`check_prompt_duplicates.py` 默认以 24 字连续重复作为人工复核提示，不阻断真实且必要的相同术语；需要严格批次策略时才使用 `--strict-duplicates`。

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

# 提取 evaluator 中每条直接失败断言，填写四方精确片段后校验：
python3 <skill>/scripts/contract_coverage.py init --project <project>
python3 <skill>/scripts/contract_coverage.py check --project <project>
```

- `primary_runtime_mechanism` 必须是运行时机制；`coupled_runtime_mechanisms` 可选，填写时必须是不同且真实存在的机制。
- 必须填写 `core_defect_review`：核心缺陷必须是真实运行时机制失效，且必须依赖调用顺序/生命周期和跨层状态传导；同时明确证明它不是索引、边界、容量、字段映射、比较符/条件分支、单 `%w`、单 nil 判断、单状态漏边或单函数输入输出变换。`failure_chain` 与 `local_fix_rejection` 各至少 20 字，不能用“改了多个文件”替代故障链证据。
- `core_defect_review.minimum_function_files` 与 `root_cause_locations` 至少覆盖 2 个真实相关功能文件，并说明各自的运行时职责和故障贡献。不得靠拆文件、格式化、注释或防御性代码凑数。
- `trigger_sequence` 记录真实所需的触发顺序，`affected_layers` 至少包含 2 个相关模块/包。
- `query_evidence` 的触发与预期片段、`symptom_coverage` 的主要症状片段必须逐字来自 `user_query`；每个已填写症状都写明目标测试中的对应断言。
- `repair_ablation_checks` 是可选增强证据。填写时每个条目都必须真实回退对应修复、执行原样 `verify_cmds` 并重新变红；未填写时不阻断普通 bugfix。
- `manual_reviewed` 只能在真实审阅代码、diff 和复跑结果后设为 `true`。脚本只校验证据结构，不能代替人工判断机制是否真实。
- 新建的 `contract_coverage.json` 使用 version 2：除每条 evaluator `Fatal/Fatalf/Error/Errorf` 映射到题面触发、题面预期、`success_criteria` 和难度证据外，`test_cases` 还必须将 evaluator 设置输入的精确源码片段映射到题面触发与预期。输入片段不能拿断言文案代替；空字符串或 nil 边界必须在题面片段中点名对应字段。题面包含“照常/仍能/不影响”等既有行为要求时，必须增加 `kind: preservation` 映射。旧 version 1 成品只做兼容复核，新数据不得手工降级。

### 4.2 题面去重自检（必做）

每写完一道或一批题面，在本期根目录执行：

```bash
python3 <skill>/scripts/check_prompt_duplicates.py --root .
```

脚本会递归扫描各 `YYYY-MM-DD/<record>/prompt.txt` 与 `collection.json` 里的 `user_query` / `success_criteria` / `verify_cmds`，硬性拦截禁止业务、内部构造泄漏、Go 版本环境描述和已知模板句；默认把任意两题同一字段 >=24 字的连续重复片段作为人工复核提示。只有批次明确要求严格去重时才加 `--strict-duplicates`。

## 第 5 步：红绿校准（出题自检）

必须实际完成以下校准，缺一不可：

> 校准必须在临时工作目录中完成：分别复制 Bug 基线和 gold，再把 `evaluator/` 注入临时副本后执行 `verify_cmds`。正式修复轨迹的 workspace 不得出现这些测试。

1. `env`（埋好 bug）+ verify_cmds → **必须红**。
2. 打上 gold 修复 + verify_cmds → **必须绿**（全量测试无回归）。
3. 若 `difficulty_review.json` 填写了 `repair_ablation_checks`，逐项回退并用原样 `verify_cmds` 重新验证；每次检查后恢复该文件，最终完整 gold 再次为绿。

- `verify_cmds` 必须是单条定向命令，形如 `go test ./path/to/pkg -run '^TestTargetBug$' -count=1`；`concurrency并发问题` 必须显式加 `-race`，脚本会按 `bug_category` 强制校验。禁止 `go test ./...`、`.`、`...`、多包、多个测试或拼接回归命令。
- 并发 bugfix 的 `repro_determinism` 必须填 `deterministic`；具体的同步原语、交错控制、测试钩子或超时边界及稳定性验收事实写入 `success_criteria`，diagnosis 还必须在 `gold_root_cause` 说明同一方案。只写“稳定复现”“多跑几次”不合格。
- `verify_cmds` 对应测试必须完整覆盖 `user_query` 描述的问题：逐项核对题面里的每个用户可见现象、触发条件和结果；任一项没有断言或无法由该命令触发，都判不合格，不能只验证相邻行为或单个局部症状。
- 稳定性校准需要红/绿各验证 ≥20 次时，重复执行同一条 `-count=1` 定向命令；不得把字段改成 `-count=20`。并发题每次都必须保留 `-race`，即至少连续实跑 20 次原样的 `-race -count=1` 命令。
- 埋错自检必须 20/20 全红，修复后必须 20/20 全绿；连跑 ≥20 遍仍不稳的标 flaky，只做 diagnosis。

### 5.1 `success_criteria` 生成规则（业务场景硬门禁）

`success_criteria` 是**这条数据自己的验收摘要**，不能是换到任何项目都成立的流程说明。写之前先从 `user_query` 和目标测试断言中摘出本题的「业务对象/输入状态、可见异常、后续影响」；字段中必须原样复用至少一个来自 `user_query` 的 4 字以上业务短语，再把实跑事实写到这些对象上。不得只写“代码状态、定向命令、定位结论、公开现象、真实复现、无回归、工作区不变”等流程词。

- **bugfix**：写清具体业务触发在问题存在时 20/20 出现什么错误，修复后 20/20 恢复什么公开行为；再写回退关键改动后哪个业务现象重新出现，以及全量回归结果。每个红/绿点都要点名本题的业务对象、状态或回执，不能只写“稳定变红 / 修复后全绿 / 回退再红”。
- **diagnosis**：写清具体业务触发 20/20 出现什么异常；定位结论必须串起本题的输入或接口值、恢复/状态路径与后续跳过、污染或错误回执；最后说明工作区零改动。不能拿 bugfix 的“修复后 20 绿、全量无回归”模板来填 diagnosis。
- **逐项可核对**：字段里的业务现象必须在 `user_query` 中出现，并由 `verify_cmds` 对应测试的断言或 diagnosis 结论实际覆盖；不许为显得具体而新编业务名词或结果。
- **禁止错误范式**：`出问题的代码状态下定向命令稳定变红；定位结论说清文件、符号和现象链路；全程不改项目文件，只看公开现象和真实复现。` 这类没有业务对象、触发条件和具体异常的描述直接判不合格。
- **合格形态**：`缺失地址的定向检查 20 遍都稳定出现异常回执污染；结论需解释接口值、恢复路径和后续跳过之间的联系；工作区保持原样。` 这里的“缺失地址 / 异常回执污染 / 后续跳过”必须替换为该条数据真实存在且已经验证的业务内容，禁止把本句当模板复用。

写完由 `collection_table.py write` 和 `post_qc.py` 校验业务短语锚点及已知空泛句；脚本通过只代表基础门禁通过，仍须人工逐项对照题面、断言和真实复跑结果。

### 5.2 交付字段叙事视角（硬门禁）

收集表和题面要把缺陷表述为**程序本身已经存在、等待修复或定位的问题**。内部可以按本技能流程构造和校准数据，但内部出题过程不能进入对外交付字段。

- `bug_id`、`user_query`、`gold_root_cause`、`success_criteria` 以及 `prompt.txt` 中禁止出现“埋错 / 埋 bug / 人工注入 / 故障注入 / 故意制造 / 出题环境 / `_gold` / gold 修复”等措辞。
- 红灯事实写“问题存在时 / 修复前 / 当前代码中”，绿灯事实写“修复后”；不得写“埋错态 / 埋错环境 / 注入缺陷后”。
- `gold_root_cause` 只写最终可复核的文件、符号和故障机制，不交代缺陷是怎样被构造出来的。
- `success_criteria` 只写业务触发、故障表现、修复后的公开行为、回退复现与回归结果；不得出现 `env`、`_gold`、出题人或测试模型等内部角色和目录。
`collection_table.py write` 和 `post_qc.py` 会对上述交付字段执行同一套禁词校验；命中即拒绝写入或判后置质检失败。

## 第 6 步：Docker 验证 + 项目简介 + 发布 GitHub（跑轨迹前）

> 必须在跑轨迹前发布无测试资产的 orphan Bug 单提交：bugfix 发布到 `bug<record>_green`，diagnosis 发布到且只发布到 `bug<record>_red`。bugfix 此时 G2 和 R1 绝对不得存在。

### 6.1 Docker 本机验证

```bash
python3 <skill>/scripts/build_docker.py verify --root . --project <name>__<record>
```

- 验证 `env/`（bug 环境）能构建，并实际确认目标问题可触发。
- 验证 `_gold/<project>/`（gold 环境）`go build ./...` 与 `go test ./...` 全绿。
- Dockerfile 基于官方 golang 多架构基础镜像，支持 arm64/amd64；本流程只验证当前机器平台。
- 刻意包含构建失败 Bug 时，`build_docker.py verify` 的自动验证不适用；需人工改造**仓库根目录**的 `benzhi.Dockerfile`，再手动在容器内验证。

### 6.2 项目类型简介

`<project>/project_summary.txt` 必须在创建记录时生成并保持为单行。内容说明项目使用 Go 实现、属于什么类型、主要完成什么功能，例如：

`基于 Go 实现的停车场管理 CLI 项目，一款命令行工具，完成车位录入、车辆进出登记与费用核算。`

发布脚本会把该句逐字写到仓库根目录 `BENZHI_README.md` 的第一行。`project_summary.txt` 是记录元数据，不进入 GitHub 分支。项目目录、`env/` 和交付分支均不得包含 `BUG_REPRO.md`。

### 6.3 发布 Bug 分支，产出 repo_url

```bash
python3 <skill>/scripts/github_project.py publish \
  --root . --repo-name <repo_name> \
  --project <name>__<record> --bug-id <bug_id>
```

脚本会：

- 先硬性检查 `evaluator/` 中的目标测试不存在于 `env/`；
- 用 `git checkout --orphan` 创建交付分支，把排除所有 `*_test.go` 的 `env/` 与交付文件写成单提交；bugfix 使用 `bug<record>_green`，diagnosis 只使用 `bug<record>_red`；
- **强制校验** `benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md` 均在仓库根目录；若旧版在模块子目录留下同名副本，脚本会清理后再发布；
- **强制校验** `BENZHI_README.md` 第一行与 `project_summary.txt` 一致，且所有交付分支都不含 `BUG_REPRO.md`；
- 写入 `<project>/_delivery/g1_snapshot.json`，记录模型可见文件的 SHA-256；`run_trajectory.py` 必须逐文件匹配该清单才能开跑；
- 输出对应交付分支的 `repoUrl`（填 `repo_url`）；本地 `_gold/` 不提交到 GitHub。

## 第 7 步：跑轨迹

- 每个 session 只产一条数据；环境目录下**从未开过** claude session。
- **红灯门禁（红线）**：开跑修复轨迹之前，必须先完成附录的红灯证据（`run_evidence_trajectories.py generate --phase red`）且达标——测试模型实测确认 bug 在基线上可复现。红灯不过说明埋错质量不行，回滚重新埋错，不得开跑正式轨迹（避免白烧一场限流额度）。`run_trajectory.py run` 会自动检查：缺少 `_evidence/red_result.json` 即拒绝开跑，不允许跳过。
- **交付轨迹必须是 Claude Code 原始 session 文件（红线）**：三条轨迹（修复轨迹、红灯、绿灯）都直接取 Claude Code 自己落盘的 `~/.claude/projects/<按 cwd 转换的目录>/<session_id>.jsonl` 原始文件，不再用捕获 stdout 拼装的 stream-json 交付。`run_trajectory.py run` 与 `run_evidence_trajectories.py generate` 成功后会按 session_id 自动取出保存；stdout 捕获只用于运行时校验，另存为 `*.stream.jsonl` 备查，不交付。
- **不能暴露本技能/答案线索（红线）**：`env/` 和 `prompt.txt` 中不得出现 `SKILL.md`、`AGENTS.md`、`CLAUDE.md`、`.claude`、`BUG_REPRO.md`，也不得出现 `repo_url`、本技能名等标记；其中 `BUG_REPRO.md` 已全面禁用，脚本仍会防御性拒绝旧残留。
- **单轮强制约定（红线）**：全程有且仅有**一条用户输入**，内容就是 `prompt.txt` 原样；不得追加任何说明、不得输入任何斜杠命令（`/model`、`/status`、`/clear`、`/help` 等），也不得有任何“运行命令式”的额外输入。出现第二条用户消息或任意斜杠命令即不合格，必须回滚重跑。
- **不注入任何额外约束文字（红线）**：不使用 `--system-prompt` / `--append-system-prompt`，不向题面拼接“不得读测试”等专门说明。轨迹文件中只能看到 `prompt.txt` 原文；隔离与限制完全由脚本实现。
- **G1 单分支快照隔离（红线）**：`run_trajectory.py` 将校验快照与 `g1_snapshot.json` 完全一致，再在系统临时目录生成无测试资产、无 `.git`、无交付线索的副本。绝不把原始多分支 repo 交给模型。
- **工具执行前越界守卫（红线）**：脚本通过 Claude `PreToolUse` hook 在 Bash/Read/Edit/Write/Glob/Grep 真正执行前拦截工作区外、`.git`、`_gold`、`evaluator`和证据目录。不向 system prompt 或 user prompt 注入任何文字；轨迹会区分“已被 hook 拦截的尝试”与“实际越界成功”。
- **模型正式轨迹不执行 `verify_cmds`（红线）**：模型可以 build、运行业务程序或做普通定位，但目标测试不对模型可见。模型结束后，脚本才在独立临时副本注入 evaluator 执行验收。
- **自动验收后才同步**：一轮模型成功结束后，本地并发执行轨迹分析、私有 `verify_cmds`、`go test ./...`和任务语义/基线 diff，写入 `_evidence/trajectory_acceptance.json`。任一项失败立即停止，显示原始断言，不消耗后续模型重试；全部通过后才同步 `env/`并自动绑定 `collection.json.session_id`。
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

跑前也可单独执行同一门禁：

```bash
python3 <skill>/scripts/trajectory_guard.py preflight \
  --env <project>/env --evaluator <project>/evaluator --verify-cmds '<verify_cmds>'
```

- `env/` 没有 `.git` 时，脚本用 `.base_snapshot` 做回滚基线：**已有快照（红灯门禁阶段生成）一律复用、绝不覆盖**——重跑时 env 里是上一轮模型的改动，覆盖会把污染代码拍成基线；快照不存在才从当前 env 创建。
- **重跑自动归档**：`run_trajectory.py run` 开跑前会把上一轮的主轨迹（`<uuid>.jsonl` / `*.stream.jsonl`）、`trajectory.*` 临时/失败/日志文件和绿灯产物自动迁入 `<project>/_failed_rounds/<时间戳>-rerun/`，不污染本轮；红灯证据与基线快照仍然有效，保留原位。红灯/绿灯重跑时同样各自归档上一轮产物（`-red-retry` / `-green-retry`）。
- 失败的尝试留档为 `<project>/trajectory.failN.jsonl`。
- 跑完后项目目录里的交付轨迹**文件名必须是 `<session_id>.jsonl`**（Claude Code 原始 session 文件，脚本自动取出）；脚本自动保证三处一致：文件名 == `collection.json` 的 `session_id` == 轨迹内的 `sessionId`。同名 `*.stream.jsonl` 是运行校验副本，不交付。
- 跑轨迹期间尽量断网；脚本默认禁 `WebFetch` / `WebSearch` / `Bash(git clone *)` / `Bash(curl *)` / `Bash(wget *)`。
- **测试模型限流，全局八路并发（红线）**：修复轨迹、红灯、绿灯都调同一个测试模型，`run_trajectory.py run` 与 `run_evidence_trajectories.py generate` 共用跨进程槽位，默认同一时刻最多运行八条模型任务；可通过 `--model-slots 1-8` 调整，设为 1 时恢复串行。
- **跑前验收仿真（红线）**：preflight 必须删除原始 `*_test.go`，只注入 evaluator 后编译全部包，禁止 evaluator 依赖原项目测试 helper；diagnosis 还必须用 `gold_root_cause` 通过与正式轨迹相同的文件/符号/机制验收器。任一失败都不得调用目标模型。
- **确定性失败熔断（红线）**：evaluator 编译失败、轨迹守卫命中和全量回归失败不得自动重跑；同一输入指纹下相同 diagnosis 或 private test 失败第二次出现即停止。修改 prompt、evaluator、collection 或验收契约后必须重新 preflight，不能沿用旧输入继续 retry。

## 第 8 步：轨迹质检四查

```bash
python3 <skill>/scripts/analyze_trajectory.py <project>/trajectory.jsonl
```

然后对照四项人工判定：

| 检查项 | 通过标准 |
|--------|---------|
| 完整性 | 题面到最终回复无中断；只有一轮用户输入；无任何斜杠命令（/model /status 等） |
| 结果 | bugfix：质检人独立复跑 verify_cmds 从红到绿、全量无回归；diagnosis：结论命中 gold 根因三要素 |
| 过程 | 有读码定位和相称业务改动；无预置验收测试、Git 历史或私有答案接触；模型自建且不回写交付的复现脚本允许 |
| 指令遵循 | bugfix 真正修好；diagnosis 全程零代码变更 |

- 模型修复写法与 gold 修复不同是常态，**只验证公开行为**。
- bugfix 的 `verify_cmds` 必须使用 `-count=1`；稳定性校准重复执行该定向命令 20 次并全部通过，再单独执行 `go test ./...` 检查全量无回归。全量命令不得写入 `verify_cmds`。
- diff 环境目录与 base 快照，确认只改了该改的文件。

防作弊脚本输出三档：

- `cheat`：读取预置验收测试、`.git`/历史 diff、`_gold`、`evaluator` 或工作区外答案线索；直接作废。
- `suspect`：只能确认枚举/搜索过测试，或最终修改文件在测试搜索后才首次打开；必须人工读轨迹并写 `trajectory_review.json`。
- `clean`：结论由题面和模型已读的实现代码足以支撑。运行测试查看失败输出、已经读实现后再确认期望、读取普通项目 README、模型自建复现脚本，本身不算作弊。

`suspect` 人工复核通过时写入：

```json
{"session_id":"<当前 session_id>","decision":"approved","reason":"<已逐步复核的事实理由>"}
```

文件路径为 `<project>/_evidence/trajectory_review.json`。`reason` 至少 20 字，不得只写“已确认”。

### 8.1 finalize G2/R1（bugfix 题）

bugfix 题在轨迹审计、自动轨迹验收、私有绿灯和全量回归通过后执行。`finalize` 强制检查 collection / guard / acceptance 三方 session_id 非空且一致，并检查自动验收内的私测、回归、语义与轨迹分析都通过。`clean` 自动放行；`suspect` 必须有绑定 session_id、填写理由的 `_evidence/trajectory_review.json`；`cheat` 不得 finalize。

```bash
python3 <skill>/scripts/github_project.py finalize \
  --root . --repo-name <repo_name> \
  --project <name>__<record> --bug-id <bug_id>
```

脚本会：

- 在 `bug<record>_green` 的 G1 上追加一个 G2，G2 同时含模型功能代码修复和 `evaluator/` 验收文件；
- 从 G1 文件树创建无父提交的 `bug<record>_red` R1，再加入同一份验收文件；
- 硬校验 green 恰好两个提交、red 恰好一个提交、两分支无共同祖先、G1/R1 非测试文件逐 blob 一致、G2/R1 验收文件逐 blob 一致；
- 写入 `_evidence/repository_delivery.json`，绑定 G1/G2/R1 SHA 与本条轨迹 session_id。

diagnosis 题不执行 finalize，只保留 publish 创建的 orphan red 单提交，且不得出现 green。`push-fix` 仅作旧命令兼容入口，新流程统一使用 `finalize`。

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

- `user_query` / `success_criteria` 两条：高中生口语、只写现象与公开预期/验收事实；`user_query` 仍是纯提示词，禁止写任何验收、复现、运行指令（包括要求“跑/执行/验证测试”），不写根因、不写「不是...而是...」等 AI 词。`success_criteria` 必须按 5.1 写入本条数据独有的业务对象、触发状态、异常结果和后续影响，禁止通用流程描述；所有交付字段按 5.2 采用“程序本身存在问题”的叙事，禁止泄露构造缺陷的过程。
- `verify_result`：**不再手写**，由附录「红/绿证据轨迹」自动生成并回填 JSON——bugfix 为 `pre_fix`+`post_fix`，diagnosis 只有 `pre_fix`。
- `harness`：必须写生成轨迹的工具名 + 版本号，例如 `Claude Code CLI v2.1.233`；禁止只写 `Claude Code CLI` 或只写模型名。
- `verify_cmds`：bugfix / diagnosis 都必填；必须与红灯证据轨迹实际执行的唯一 Bash 命令和最终回复【命令】逐字符一致，bugfix 还必须与绿灯的实际命令和最终回复逐字符一致。

- `bug_id`：严格填“记录目录主体-三位 record”，如 `16-exam-system【10】__001` 对应 `16-exam-system【10】-001`。
- `repo_url`：bugfix 填 `bug<record>_green` 分支地址；diagnosis 填唯一的 `bug<record>_red` 分支地址。
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

3. 确认已就位：`<project>/<session_id>.jsonl`、`collection.json`、项目 xlsx、`_delivery/g1_snapshot.json`、`_evidence/repository_delivery.json`以及 GitHub 交付分支（bugfix 为 green/red，diagnosis 仅 red）。

### 9.4 后置质检（交付前硬校验，交付前必跑）

整期所有记录都完成第 9.1–9.3 步后，在本期根目录执行：

```bash
python3 <skill>/scripts/post_qc.py --root . --workers 3
```

只读、不改产物。逐条输出 ✅/❌，最终汇总；有任何一条不合格就退出码非 0。校验项：

1. **runtime**：核对 preflight 与 Docker 留存的 build、回归和 20/20 红绿证据及输入指纹，默认不重复执行；排障时加 `--recheck-runtime` 显式复跑；
2. **scope**：报告功能代码改动文件数和行数，确认存在真实修复，不用固定规模代替难度判断；
5. **privacy**：目标测试只存在 `evaluator/`，`env/` 和初始 Bug 基线中不存在；
6. **files**：`<session_id>.jsonl` / `project_summary.txt` / `collection.json` 齐全，记录目录不含 `BUG_REPRO.md`；
7. **fields**：`collection.json` 必填字段齐全；
8. **evidence**：`verify_result` 结构、URL、session_id 校验；
9. **trajectory_guard**：正式修复轨迹守卫通过且 session_id 匹配；新 schema 还必须有通过的自动轨迹验收，bugfix 必须有绿灯后全量回归标记；
10. **diagnosis**：diagnosis 题 `env` 与 `.base_snapshot` 零差异；
11. **coverage**：`verify_cmds` 形态合法，且 `contract_coverage.json` 将 evaluator 每条断言映射到题面、`success_criteria` 和难度证据；
12. **difficulty**：运行时机制、跨层触发和题面覆盖证据齐全；已填写的可选回退证据必须有效；
13. **domain**：项目与功能点未命中禁止类型。
14. **repository**：远程无 `main`/干净基座；bugfix 校验 green/red orphan 拓扑及 G1/G2/R1，diagnosis 校验 red-only orphan 单提交且不存在 green；G1 快照、简介和 `BUG_REPRO.md` 禁令均一致。

> 交付前各项必须全绿；其中 repository 是防止模型通过 Git 历史或其他 bug 分支抄到答案的最后硬门禁。

`post_qc.py` 默认用 3 个 worker 并发核对不同记录的留存证据，输出仍按记录名稳定排序；同一 repo 的远程分支元数据只查一次并复用。默认模式不会再次执行 Go build、私测或回归；只有证据缺失、指纹变化或排障时才使用 `--recheck-runtime`。旧的已 finalize 记录没有 `pipeline_schema: 2` 时按旧证据兼容复核；新正式轨迹由脚本自动写入 schema 2，不得缺契约和自动验收文件。

## 腾讯文档粘贴注意

在腾讯文档粘贴 `user_query` / `verify_cmds` / 长文本时，**先点击进入目标输入框，再执行粘贴**。如果未进入输入框直接粘贴，带换行、特殊符号的内容容易异常跳行、数据截断丢失，导致录入出错。

## 附：红/绿证据轨迹（bugfix=红+绿，diagnosis=仅红；回填 verify_result）

用目标模型（Claude Code）产出只验证、不改代码的证据轨迹，上传 COS 后回填 `verify_result`。**分两个阶段、穿插在主流程里执行**（脚本按 `collection.json` 的 `task_type` 与 `--phase` 自动选择）：

- **bugfix**：红灯（第 6→7 步之间，门禁）+ 绿灯（第 8→9 步之间，验收）（`pre_fix` + `post_fix`）。
- **diagnosis**：只产红灯（仅 `pre_fix`，不出现 `post_fix`），红灯阶段即完成上传与回填。

1. **红灯验证轨迹（pre_fix，开考门禁）**：在私有 `red_env` 临时副本中注入 `evaluator/` 后运行 `verify_cmds`。目标测试不回写 `.base_snapshot/`、`env/` 或 Repo。
2. **绿灯验证轨迹（post_fix，仅 bugfix）**：正式轨迹四查通过后，复制模型修复后的 `env/` 到私有 `green_env`，注入同一份 `evaluator/` 后执行 `verify_cmds`。绿灯通过后才允许 finalize G2/R1。
3. **绿灯后全量回归**：脚本紧接着在同一私有 `green_env` 执行 `go test ./...`，通过后写入 `_evidence/green_regression.json`；`finalize` 缺少该通过凭据时拒绝推送。

> 模型红线：红/绿都用 `claude`（目标模型 `model_hub/glm-52-coding`）；**不生成 gold 修复轨迹**，当前 Codex 的正确修复只保存在本地 `_gold/` 供校准和质检。
> diagnosis 的红灯复现命令同样来自 `collection.json` 的 `verify_cmds`；`--verify-cmds` 只允许临时校准时覆盖，正式交付前必须写回 collection，且与证据轨迹实际执行的唯一 Bash 命令、最终回复【命令】逐字符一致。不再提供全量测试默认值。

### 统一验收标准（硬门禁）

- **红灯达标**：结论含「BUG 存在」且不含「BUG 不存在」；除脚本预先注入的 `evaluator/` 测试外，验证环境零改动。
- **绿灯达标（仅 bugfix）**：结论含「已修复」且不含「未修复」；除脚本预先注入的 `evaluator/` 测试外，验证环境零改动。不达标则回滚重跑第 7 步并重新质检。
- 每条证据最多自动重试 3 次；每次重试前从基线 / 修复后 `env/` 重新 rsync 验证环境。

### 全局八路并发（红线）

red / green / 修复轨迹共用跨进程模型槽位，默认全局最多同时运行 8 路。`batch_pipeline.py` 默认传 `--model-workers 8`；`run_evidence_trajectories.py generate` 与 `run_trajectory.py run` 默认使用 `--model-slots 8`，槽位文件位于 `~/.codex/go-annotation-pipeline/model-slots/`，等待超时仍由 `--lock-timeout` 控制。参数可设为 1-8；不同调用方并行运行时必须使用相同槽位数，设为 1 时恢复全局串行。

同一记录内部必须保持顺序：bugfix 为 G1 green 发布→红灯→正式轨迹→绿灯→G2/R1 finalize；diagnosis 为 red-only 发布→红灯→正式轨迹。不同记录独立推进并使用各自隔离工作区；同仓库的 Git 写临界区通过仓库锁串行，不同仓库的 Git 写可并行。并发不得删除、抽样或降级任何门禁。

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
