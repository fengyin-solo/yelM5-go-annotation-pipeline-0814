# 出题与质检硬性规则（0-1 自建项目版）

## 目录
- 来源与去重
- 禁止项目与功能点
- 任务类型与配比
- 选题与埋 bug 门槛
- 缺陷分类 bug_category
- 防泄漏清单
- 验证口径红线
- 轨迹合格判定（四查 + 典型 fail）
- 跑轨迹重试与回滚
- GitHub 分支与交付
- Docker 验证口径
- 收集表文案书写规范
- 作弊红线

## 来源与去重

- 选题来源统一为**自己 0-1 生成的项目**，不再去 GitHub/Gitee 找题。
- 项目与 GitHub 仓库名要**具体到业务**，禁止 `forex`、`task` 这类泛化名；GitHub 仓库名用「领域+用途+类型」拼成 3-5 个英文单词的描述性名字（如 `sensor-telemetry-ingestion-service`），不加 `go-` 前缀、不加随机码、不出现 `test`/`fix` 字样。
- 0-1 项目来源由用户指定：可以是项目生成提示词，也可以是本地项目目录。
- 去重身份按优先级：**优先 GitHub 仓库地址，其次本地项目绝对路径**。
- 同一个 repo（GitHub 地址或本地路径任一命中）最多出 30 条记录，每条一个不同 bug。总需求超过 30 条时按 `[30, ..., 余数]` 拆到多个不同的 0-1 项目和 GitHub 仓库，仓库数为 `ceil(总条数 / 30)`。
- 同一个 bug 只能出一个 task_type：bugfix / diagnosis 二选一，不得同时出两条。
- 全局注册表唯一事实源是 `used-repositories.json`；每次确认选题后立即 `repo_registry.py register` 登记并 `sync`。

## 禁止项目与功能点（最高优先级）

完整清单与判定方法见 [forbidden-domains.md](forbidden-domains.md)，开始任何编码前必须阅读。

- 审查顺序固定为：项目总体类型 → 本条数据的具体业务功能点 → 最终 `user_query` / `success_criteria` / `gold_root_cause`。任一层命中即淘汰。
- 查账 / 账务和订单类是最高优先级硬禁区；其他禁区包括清单中的游戏图形、平台业务系统、本地桌面工具、数据可视化和前端页面。
- 项目允许不代表其中所有功能都允许。例如允许项目里的库存调拨、财务核对、预约或工单功能仍然不能出题。
- 禁止关键词规避。若核心实体、主流程或用户目标在语义上等价，改名、删词、换同义词后仍判不合格。
- 在生成项目、建仓和埋错前分别运行 `domain_guard.py`；`github_project.py ensure` 会在创建 GitHub 仓库前检查候选仓库名、项目目录名与根 README。
- 题面阶段再次运行 `check_prompt_duplicates.py`，填表和 `post_qc.py` 还会复查。若此时命中禁止类型，必须回到功能点阶段换题，不能只改题面。

## 任务类型与配比

- bugfix 60%：题面必须明确写「请修复 xxx / 帮我修好 xxx」，终点是约定测试从红到绿。
- diagnosis 40%：题面必须明确写「不要修改代码，帮我定位 xxx / 文件先不要改，帮我查清楚原因」，终点是根因定位正确且全程零代码变更。
- 指令遵循失败 = 数据失败：bugfix 只诊断没修、diagnosis 动了代码，均不可提交。
- user_query 必须是纯文本提示词：禁止富文本、空文本块、多余占位符；**不写任何验收/复现/运行命令，不贴命令代码块**（`go test` / `go build` / `go run` 等一律不出现）；确需展示关键症状的最小复现 ≤3 行（症状代码，不是命令），禁止大段源码、大段日志。
- user_query 不得写 Go 版本号或 Go 工具链版本，包括“Go 1.23”“Go 版本为 1.23”“1.23 版 Go”及其空格、`v` 前缀、补丁版本变体。环境交代正常写“当前项目就可以了”，修复/定位要求再由任务指令说清。Go 版本只在 `go_version` 字段和内部验证中维护。
- 题面简短口语化，信息要素齐全（现象 / 一句自然背景 / 环境交代 / 任务指令），但**写法自由**：顺序可乱、要素可合并进同一句、详略可变，禁止按模板逐句排。
- `verify_cmds` 是收集表独立字段，**不写进题面**：bugfix / diagnosis 都必填，只允许唯一目标包 + 精确测试名 + `-count=1` 的定向命令（并发加 `-race`），禁止 `go test ./...`、通配包、当前目录、多包或拼接回归命令。对应测试必须完整覆盖 `user_query` 描述的全部现象与触发条件。红/绿轨迹必须各自只实际执行一次该命令；实际 Bash 调用、最终回复【命令】和正式填表值必须逐字符一致，空格、引号、路径及参数顺序也不能变化。bugfix 校验红+绿，diagnosis 只校验红。

## 选题与埋 bug 门槛

1. 项目总体类型和本条具体功能点均通过禁止领域人工语义审查与 `domain_guard.py` 最低门禁。
2. 0-1 项目能 `go build ./...` 通过，是真实可运行的项目。
3. repo 尚未用满 30 条记录，且当前 bug 未在本 repo 出现过。
4. 埋的 bug 能稳定复现，并能反推出文件、符号、失效机制三项。
5. 版本固定、可重复构建，Go toolchain 版本已确定。
6. 与改动半径相关的既有测试可运行，可用作回归。
7. gold 根因与 gold 修复不进入测试模型可见环境。

埋 bug 难度按 bug_category 选型：优先 concurrency / nil / slice / error / context / defer 里需要多步定位、强模型也会栽的缺陷；**必须满足「埋错复杂度红线」**（见下节），禁止一眼看出、单字符、常量改错、go vet 级或改坏测试暴露答案的简单埋错。

**本地 `_gold/` 修复由执行本标注流程的模型（当前 Codex）完成**，不是由 Claude Code 配置的测试模型完成。它只用于红绿校准、难度检查和回归验证，不创建远程分支、不进入收集表。

## 埋错复杂度红线（硬性，出题前必读）

**难度以真实故障链为准，不以修复规模代替。** 核心缺陷必须涉及至少 1 个 Go 运行时机制，跨至少 2 个相关模块/包，并依赖调用顺序、并发交错、请求生命周期或状态转换才能完整触发。存在第二种耦合机制时应记录，但不要求每题强行叠加。

允许的运行时机制标签：`concurrency_sync`、`channel_lifecycle`、`context_lifecycle`、`error_retry`、`resource_lifecycle`、`transaction_lifecycle`、`typed_nil_dispatch`、`panic_recovery`、`shared_state_pollution`、`state_machine_idempotency`。

bugfix 的 gold 必须包含真实功能代码修复。文件数和增删行数作为审查信息记录，统计不含 `_test.go`、README、Markdown/其他文档、注释和交付文件；不得靠改测试、拆文件、加注释或堆无效代码制造规模。

- 禁止把以下内容作为核心缺陷：纯索引/长度/offset/容量/边界计算，单字段映射、常量、比较符或条件分支，单个 `%v`/`%w`、nil 判断、状态转换漏项、序列化标签，以及单函数纯输入输出变换。扩大改动规模也不能让这类简单缺陷合格。
- 根本修复若只是显而易见的局部常量、比较符或字段改动，通常淘汰；集中修复本身不是淘汰理由，关键看定位和故障传播难度。
- 纯数据变换只有与异步消费、共享状态逃逸、跨请求污染等运行时机制形成不可拆故障链时才允许使用。
- diagnosis 的根因也应体现跨模块影响或运行时机制，不搞一眼看穿的局部错误。

### 复杂埋错设计方法（把 bug 做成“多文件协同失效”）

> 具体到文件分布、症状画像、gold 修复形状的 12 个深度模式（P1–P12）见 [bug-patterns.md](bug-patterns.md)，用 `pick_bug_pattern.py` 随机抽取。单仓最多 30 条，优先轮完 P1–P12 后再复用模式骨架；复用时必须更换业务层次、机制组合、触发条件和症状，保证仍是不同 bug。

1. **先选机制组合**：从允许标签中选 1 个主机制和至少 1 个不同耦合机制，例如 context 生命周期 + 错误重试、锁与快照 + 跨请求污染、资源释放 + 事务错误传播。
2. **再设计有顺序的触发**：至少经过两次操作或两个生命周期阶段，例如首请求超时后再发正常请求、失败后重试成功、写入与异步读取交错。
3. **让故障跨层传导**：入口层切断生命周期 → service 作出错误决策 → worker/store 留下副作用；用户在末端看到症状，不能从报错行直接得到修复点。
4. **保持一个目标缺陷**：多个埋点必须共同违反同一份生命周期、所有权、错误或状态契约，不得拼接互不相关的四个 bug。
5. **确认修复对应故障链**：修复涉及多个关键文件时，可逐个回退并确认定向测试重新失败；不要为满足数量拆分修复。

### 实战经验（踩坑记录，照做）

- **复杂 bug 首选同一契约下的机制耦合**：例如 context 传播、重试停止、client 请求绑定和跨请求隔离共同服务于“请求结束后所有工作都应停止”这一份契约。不要把比较方向、不排序、去锁、WaitGroup 错位等互不相关问题硬拼成一道题。
- **buggy 代码必须能 `go build ./...`**：改动函数后若某个 import 不再被使用（如去掉 `sort.SliceStable` 后 `import "sort"` 悬空），要同步删/加 import，否则整个项目编译失败、红绿校准全部落空。
- **修复规模始终在本地埋错基线与 `_gold/` 之间测量**，**不要在跑完轨迹后的 `env/` 上量**——env 已被测试模型改回接近 gold，diff 会很小。
- **diagnosis 也要准备专用复现测试**：目标测试只写入记录根目录的私有 `evaluator/`，校准和红灯证据时注入临时副本；不得同步到 `env/`、`.base_snapshot/` 或 `_gold/`。
- **不再生成 `SOURCE.txt`**：`workspace.py` 不再在 `_gold/<record>/` 写本地路径；`github_project.py` 的 `sync_worktree` 仍保留 `--exclude=SOURCE.txt` 兜底，禁止手动把任何本地路径/出题人元数据加回 git。

### 埋错自检清单（出题人逐项确认）

- [ ] gold 修复包含真实功能代码变更，文件数和行数已记录且没有凑规模
- [ ] 至少 1 个主运行时机制真实存在；填写的耦合机制均有代码和运行证据
- [ ] `core_defect_review` 明确证明：故障链依赖运行时机制、调用顺序/生命周期和跨层状态传导；根本修复不是单点数据变换
- [ ] `root_cause_locations` 至少覆盖 2 个真实相关功能文件，并逐项说明运行时职责和故障贡献
- [ ] 故障链跨至少 2 个相关模块/包，触发过程与真实复现一致
- [ ] user_query 的主要症状和正确预期均由目标测试覆盖
- [ ] 若填写逐文件回退证据，每一项都真实复跑并重新变红
- [ ] 单看任何一个文件都看不出完整问题
- [ ] 20/20 全红稳定，打上 gold 修复后 20/20 全绿
- [ ] 题面只写症状，不暴露文件/符号/改动位置

### 改动规模测量（出题时跑，本地、发布前）

```bash
# 首选：发布前直接对比本地 env/ 与 _gold/，不需要 GitHub 分支
git diff --no-index --numstat <project>/env _gold/<name>__<record> \
  | grep -Ev '(_test\.go|README|\.md$|BENZHI|benzhi\.Dockerfile|build_benzhi_docker\.sh)' \
  | awk '{add+=$1; del+=$2; n++} END {print n" functional files, "add+del" changed lines (+"add"/-"del")"}'

```

- **测量应在进入红绿校准之前完成**（用本地 `--no-index` 命令），用于辅助审查修复形状。
- `numstat` 只能机械排除测试/文档/交付文件，不能识别注释、纯格式化、拆文件和无效逻辑；必须人工审阅 diff，确认变更确实服务于故障链。
- **不要在跑完轨迹后的 `env/` 上量**——env 已被测试模型改回接近 gold，diff 会很小。

**运行时机制、跨模块传播、真实复现或题面测试覆盖任一硬门禁不达标，都判埋错不合格。** 文件数、行数和逐文件回退属于辅助或增强证据。使用 `scripts/difficulty_review.py init/check` 维护记录根目录的私有 `difficulty_review.json`；该文件不得放进 `env/`。

## 缺陷分类 bug_category

`bug_category` 只允许以下取值，填写收集表时**原样使用**：

`concurrency并发问题`、`slice相关问题`、`error异常错误`、`nil相关问题`、`context相关问题`、`defer相关问题`、`其他问题`

| bug_category | 典型失效模式 | 复现要点 |
|------|-------------|---------|
| concurrency并发问题 | data race、WaitGroup 计数错配、channel 未关闭泄露、锁保护范围漏字段 | -race 跑；用同步原语强制交错改造为确定性 |
| slice相关问题 | 底层数组逃逸后被异步消费、缓存持有调用方切片造成跨请求污染 | 断言调用顺序、完整内容与请求隔离，不收纯容量/边界题 |
| error异常错误 | 错误归一化破坏类型、错误分类驱动错误重试/事务结果/状态映射 | 同时断言错误链、重试次数和最终副作用，不收单 `%w` 题 |
| nil相关问题 | 向 nil map 写入、nil 指针解引用、装 nil 的接口与 nil 比较为假 | 构造零值路径；区分 panic 与静默错误 |
| context相关问题 | 取消/超时不向下游传播、context 存结构体复用、忽略 ctx.Err() | 带 deadline 卡时间上限 |
| defer相关问题 | 循环内 defer 不执行、defer 改命名返回值、error 分支跳过释放 | 断言资源确实释放 |
| 其他问题 | 不属于以上类别 | gold_root_cause 写清具体失效机制 |

## 防泄漏清单

- 目标红绿测试只存放在私有 `evaluator/`；正式轨迹前的 G1 不得包含任何测试文件、夹具目录或 test/spec 资产。
- 正式修复轨迹必须在系统临时目录的无测试副本中运行：排除所有 `*_test.go`、`.git`、`evaluator`、`_gold` 和交付线索；成功后只回写非测试业务文件。
- 不得用 system prompt、append system prompt 或拼接用户题面的方式注入额外轨迹约束文字；模型只收到 `prompt.txt` 原文，防泄漏依靠环境隔离和轨迹审计。
- 测试模型 workspace 必须来自已发布 G1 的单分支快照，且不能有 `.git` 历史、remote、commit SHA、补丁文件、gold 修复说明。
- 测试模型 workspace 和 prompt 里不能暴露本技能：不得出现 `SKILL.md`、`AGENTS.md`、`CLAUDE.md`、`.claude`、`BUG_REPRO.md`，也不得出现 `repo_url`、技能名等标记。`BUG_REPRO.md` 已全面禁用，任何位置发现都应删除或拒绝交付。
- 远程不得存在 `main`、干净基座或 gold 分支。每个 bug 的 `bug<record>_green` 与 `bug<record>_red` 都用 `--orphan` 独立生根；不同 bug 也不得有共同祖先。
- 跑轨迹前 green 只有 G1 单提交。bugfix 收题后 green 为 G1→G2，red 为 R1 单提交；G1/R1 非测试树完全一致，G2/R1 验收文件完全一致。
- 绝不能把原始多分支 repo 交给模型；必须导出 G1 的模型可见快照，用 `g1_snapshot.json` 逐文件验证后再运行。
- 轨迹质检发现模型 clone 上游 / WebFetch 上游源码按疑似作弊重跑。

## 验证口径红线

- **字段必填对照**：bugfix 必填 `verify_cmds` + `verify_result`（`pre_fix`+`post_fix`）；diagnosis 必填 `verify_cmds` + `gold_root_cause` + `verify_result`（仅 `pre_fix`）。
- **定向命令硬门禁**：`verify_cmds` 只能写一条 `go test <目标包> -run '^Test目标测试$' -count=1`；必须明确唯一目标包和唯一测试名称，`concurrency并发问题` 必须显式加 `-race`，由脚本结合 `bug_category` 强制校验。禁止 `go test ./...`、`.`、`...`、多包、多测试、shell 管道或拼接全量回归。
- **并发确定性复现硬门禁**：并发 bugfix 的 `repro_determinism` 必须填 `deterministic`；具体同步原语、交错控制、测试钩子或超时边界及稳定性验收事实写入 `success_criteria`，diagnosis 还要在 `gold_root_cause` 写明同一复现策略。只写“多跑几次”“稳定复现”不合格。
- **轨迹命令一致性硬门禁**：`verify_cmds` 必须与红灯证据轨迹中要求执行、实际执行、最终回复【命令】逐字一致；bugfix 的绿灯证据轨迹也必须逐字一致。不得把 `./internal/service` 改成 `./internal/./service`、不得调整参数顺序或增删等价参数；若命令要改，必须重跑对应证据轨迹。
- **完整覆盖硬门禁**：逐项对照 `user_query`，目标测试必须能触发并断言其中描述的每个用户可见现象、触发条件和错误结果；遗漏任一项、只覆盖相邻行为、只证明构建失败或只命中局部症状，均判 `verify_cmds` 不合格。
- **断言契约硬门禁**：正式轨迹前必须用 `contract_coverage.py init/check` 将 evaluator 中每条直接失败断言逐项映射到 `user_query` 的触发/预期原文、`success_criteria` 原文和 `difficulty_review.json` 证据。任何断言无映射或 evaluator 改动后映射过期，均不得调用正式模型。
- bugfix 必须先跑初始状态红和完整 gold 绿。若 `difficulty_review.json` 填写了逐文件回退项，则每项都用原样 verify_cmds 重新跑红，随后恢复并确认完整 gold 仍为绿。
- bugfix 稳定性校准红/绿各重复执行同一条 `-count=1` 定向命令 ≥20 次；不得把 `verify_cmds` 改成 `-count=20`。并发题每次必须保留 `-race`，形成至少 20 次 `-race -count=1` 的真实稳定性证据。
- 埋错自检必须 20/20 全红，修复后必须 20/20 全绿；连跑 ≥20 遍仍不稳的标 flaky，只做 diagnosis。
- `batch_preflight.py` 必须把每次原样命令的退出码、耗时和输出尾部留在 `_evidence/preflight.json`；红灯还必须实际到达至少一条 `contract_coverage.json` 目标断言消息，禁止把编译失败或工具链崩溃计为红。
- 已填写的 bugfix 回退项必须真实执行：从完整 gold 开始，每次用 buggy 版本替换对应功能文件，注入 evaluator 并执行原样 `verify_cmds`；每个条目必须重新变红。
- 回归仍以单独复跑全量测试为准，但全量命令不得写入 `verify_cmds`；只有全量确实跑不动时才收敛到改动半径，并在 success_criteria 写明原因。
- 只验证公开行为，不要求测试模型的写法与本地 `_gold/` 修复一致。

## 轨迹合格判定（四查）

| 检查项 | 通过标准 |
|--------|---------|
| 完整性 | 题面到最终回复无中断；只有一轮用户输入；无任何斜杠命令（/model /status 等）；工作区和会话干净 |
| 结果 | bugfix：轨迹四查后在私有副本注入 evaluator，verify_cmds 绿且全量无回归；diagnosis：结论命中 gold 根因的文件/符号/机制 |
| 过程 | 有读码定位和相称的业务改动；无预置验收测试、Git 历史、私有答案或工作区外接触；自建且不交付的复现脚本允许 |
| 指令遵循 | bugfix 真正修好；diagnosis 全程零代码变更（临时复现文件允许但必须自删） |

过程不合格典型情形：访问工作区外路径、`.git`、`_gold`、`evaluator` 或其他记录；在读实现定位前读验收测试断言；反复改了又撤；改动范围明显超出问题。

防作弊输出三档：`cheat` 命中直接证据则作废；`suspect` 只允许绑定当前 session_id 的人工复核通过后交付；`clean` 自动放行。模型自建复现脚本不等于接触预置验收测试。

### 过程硬约束（避免被甲方误判 + 补真问题）

不得把下列约束追加到 system prompt 或 user prompt；由隔离快照、`PreToolUse` 工作区守卫和跑后 `analyze_trajectory.py` 客观实现：

1. **读码定位必须显式出现**：读源码/测试优先用 `Read` / `Grep` / `Glob` 工具；用 `Bash cat/sed/grep/head` 读文件也可以，但轨迹里绝不能「完全没有读码动作」。质检会把「无 Read/Grep」直接判成「未定位」，所以要确保定位动作在工具序列里看得见。
2. **正式轨迹不运行目标红绿测试**：`verify_cmds` 只在私有证据/验收副本中执行。正式轨迹的定位过程依靠读业务代码、build、运行业务程序或其他不依赖目标测试的真实观察。
3. **同类多处修改要批量做，避免同一文件反复单独 Edit**：例如多处 `%v`→`%w`、多处补锁这类机械修改，用一次 `MultiEdit` 或一次 `Bash sed -i` / `perl -i` 完成，不要对同一个文件连续打 6 次 `Edit`。质检会按「同一文件被写入次数过多」误判成「反复改了又撤」。真正的「反复改撤」定义是：**同一位置（同一段 old_string）被多次 Edit**；同一文件多处不同位置的同类修改不算。

## 跑轨迹重试与回滚

- 每个 session 只产一条数据；环境目录下从未开过 claude session。
- **交付轨迹必须是 Claude Code 原始 session 文件**：修复轨迹、红灯、绿灯三条都取 `~/.claude/projects/<目录>/<session_id>.jsonl` 原始落盘文件（脚本成功后自动取出）；stdout 捕获的 stream-json 仅作运行校验（`*.stream.jsonl`），不交付。
- 单轮强制约定：全程有且仅有一条用户输入，内容就是题面原样；不得追加说明，不得输入任何斜杠命令（`/model`、`/status`、`/clear`、`/help` 等），也不得有运行命令式额外输入。
- **红灯门禁**：开跑修复轨迹之前必须先跑红灯证据（`run_evidence_trajectories.py generate --phase red`）且达标；红灯不过 = 埋错不合格，回滚重新埋错，不得开跑轨迹。绿灯在质检通过后跑（`--phase green`），验证测试模型修复后的 `env/`。
- 跑之前确认 `env/` 和 `prompt.txt` 不暴露本技能/答案；`run_trajectory.py` 会自动拦截技能文件与 `repo_url` 等标记。
- `run_trajectory.py` 还会硬性检查 `evaluator/` 中有目标测试、`env/` 中无该测试，并在无任何 `*_test.go` 的隔离副本中运行。
- `run_trajectory.py` 还会在正式轨迹前强制校验 `contract_coverage.json`；轨迹结束后在相互独立的临时副本并发执行轨迹分析、私有定向验收、全量回归和基线语义检查。任一项失败会立即停止，不盲目重试模型；通过后才同步业务文件并自动绑定 session_id。
- 一次没有成功结束处理就必须回滚到 base 干净态再重跑，禁止在上一次残留改动上继续。
- **重跑自动归档**：重跑轨迹/红灯/绿灯前，上一轮产物自动迁入 `<project>/_failed_rounds/`（红灯证据与基线快照有效则保留原位），禁止让失败轮次的文件混进新一轮交付。
- 回滚优先用 `run_trajectory.py run` 自动完成；失败尝试留档 `trajectory.failN.jsonl`。

## GitHub 分支与交付

分支模型（无主分支/干净基座）：

```text
bug<record>_green  G1 orphan Bug 单提交 -> 收题后 G2（模型修复+验收测试） -> repo_url
bug<record>_red    R1 orphan 单提交（G1 业务树+同一验收测试）
```

- 每个 repo 最多 30 条记录，每条 bugfix 一对 green/red orphan 分支；diagnosis 只有 green G1。
- `repo_url` 填 `bug<record>_green` 分支地址。
- green/red 交付分支必须包含根目录 Docker 交付文件，且不得包含 `BUG_REPRO.md`。
- 每条记录必须有单行 `project_summary.txt`，包含 `Go`、明确项目类型和主要功能；发布后该句必须位于 `BENZHI_README.md` 第一行，`project_summary.txt` 本身不提交。
- 交付不再打 zip、不再截图，只提交 GitHub `repo_url` 分支地址。
- **`publish` 和 Docker 验证必须在跑轨迹前完成**；轨迹前只允许 G1。轨迹与私有绿灯通过后由 `finalize` 一次创建 G2/R1；禁止把 gold 修复内容混入任何远程分支。
- GitHub 仓库用 `github_project.py ensure` 自动创建 **public** repo（审核方需要能访问）；用 `publish` 推送 Bug 分支并输出 `repoUrl`。
- GitHub 凭据/作者从 `~/.codex/pg-code/github-context.json` 读取，禁止输出 token。

## Docker 验证口径

- 根目录的 `benzhi.Dockerfile` 使用官方 `golang:<version>` 基础镜像，具备 arm64/amd64 多架构支持能力；嵌套 Go 模块时仍以仓库根目录为 Docker build context，并在容器内切换到模块目录。
- 本流程实际只验证当前平台；如需双架构镜像，用 `docker buildx build --platform linux/arm64,linux/amd64`。
- bug 环境必须能 `go build ./...`；`go test ./...` 只记录，不强判全绿。
- gold 环境必须 `go build ./...` 和 `go test ./...` 全绿。
- 不打包 zip，不截图。

## 项目类型简介

- 创建记录时通过 `workspace.py new-project --project-summary '<简介>'` 生成 `<project>/project_summary.txt`。
- 简介必须只有一行，明确包含 `Go` 和项目类型（如 CLI、命令行工具、服务、API、系统或库），并概括主要功能。
- 发布脚本把简介逐字写到根目录 `BENZHI_README.md` 第一行；后置质检直接读取最终 green 分支核对。
- `project_summary.txt` 只作为记录元数据，不进入 `env/` 或 GitHub 交付分支。
- 项目目录、源码、green/red 分支均禁止出现 `BUG_REPRO.md`；发布脚本会删除 staging 中的旧残留，post-QC 会拒绝仍含该文件的记录或分支。

## 收集表文案书写规范

- `user_query` / `success_criteria` / `verify_cmds` 三条都要逐条差异化，禁止整批套同一个模板，避免被判为批量 AI 制作；`verify_result` 现为机器生成的 pre_fix/post_fix JSON，不参与文案差异化检查。
- `user_query`、`success_criteria` 或 `gold_root_cause` 命中 [forbidden-domains.md](forbidden-domains.md) 时，不得通过删词或换同义词继续使用同一功能点；必须回到选题阶段更换业务功能。
- `user_query` 差异化要点：
  - **纯提示词红线**：只写自然语言提示词，**不写验收/复现/运行指令，不贴命令代码块，也不要求模型跑、执行、重跑或验证测试**；验收命令只放 `verify_cmds` 和红/绿证据阶段。
  - **自然表达**：使用具体的真人求助口吻，禁止从现成句式池挑句子；不要求固定人设、字数、句数或情绪铺垫。
  - **真实复杂度要素齐全、写法自由**：写清真实复现所需的触发过程、主要用户可见症状、正确行为预期和任务指令；不为满足数量增加虚假步骤或症状。
  - **题面不泄漏机制**：不写 context、锁、channel、事务、对象池等推断性答案，难度通过触发顺序、关联症状和状态影响自然体现；所有描述必须来自真实复跑且有测试断言，禁止为了显难编造现象。
  - **避免模板化**：不同题目应忠实描述各自场景，不要求为了表面差异机械调整句序或长度。
  - 被禁模板句（含旧版 SKILL 示例句池）逐字出现即判雷同，完整清单以 `scripts/check_prompt_duplicates.py` 的 BANNED_PHRASES 为准，典型如：`之前是好的，估计是最近哪次改动搞出来的`、`仓库就是当前目录`、`工具链我都装好了`、`先别改代码，帮我看看是哪里的问题`、`环境我都配好了`、`直接 go test ./... 就能跑`。
  - 任务指令说法逐条换：bugfix 的「帮我修好 / 帮我修一下 / 麻烦修掉」、diagnosis 的「先别改代码 / 文件先别改 / 帮我把原因查清楚」交替且不逐字重复。
  - 写完运行 `check_prompt_duplicates.py --root .`。默认报告 24 字以上连续重复供人工复核，但不阻断真实必要术语；只有批次明确要求严格去重时使用 `--strict-duplicates`。
- **交付字段叙事红线**：`bug_id`、`user_query`、`gold_root_cause`、`success_criteria` 和 `prompt.txt` 必须把缺陷写成程序本身已经存在的问题。禁止出现“埋错 / 埋 bug / 人工注入 / 故障注入 / 故意制造 / 出题环境 / `_gold` / gold 修复”等内部构造措辞；红灯事实用“问题存在时 / 修复前 / 当前代码中”，不得用“埋错态 / 埋错环境”。
- `success_criteria` 必须是本条数据专属的业务验收摘要，不能是换到任意项目都成立的流程话术。先从 `user_query` 与目标测试断言提取「业务对象/输入状态、可见异常、后续影响」，字段里原样复用至少一个 4 字以上业务短语，并把每个红绿事实落到这些对象上；不得只写代码状态、定向命令、定位结论、公开现象、真实复现、无回归或工作区不变。
  - bugfix：具体写出业务触发在问题存在时 20/20 出现什么异常、修复后 20/20 恢复什么公开行为、回退关键改动后哪个业务现象重新出现，以及全量回归事实。
  - diagnosis：具体写出业务触发 20/20 出现什么异常；定位结论要连接本题的输入/接口值、恢复或状态路径和后续跳过、污染或错误回执；最后写工作区零改动。禁止套用 bugfix 的修复后全绿模板。
  - `出问题的代码状态下定向命令稳定变红；定位结论说清文件、符号和现象链路；全程不改项目文件，只看公开现象和真实复现。` 属于空泛错误示范，直接判不合格。
  - 合格描述应像「缺失地址的定向检查 20 遍都稳定出现异常回执污染；结论需解释接口值、恢复路径和后续跳过之间的联系；工作区保持原样」，但其中每个业务词都必须替换为本题真实、已复跑且有断言支撑的内容，禁止复用示例本身。
- `verify_cmds` 要能独立复现红/绿，固定使用唯一目标包、`-run '^TestName$'`、`-count=1`（并发加 `-race`）；测试断言必须完整覆盖 `user_query` 的全部问题描述，但命名与参数顺序不要整批统一。
- `verify_result` 由红/绿证据轨迹自动生成并回填；绿灯使用“模型修复后 `env/` 的私有副本 + 临时注入的 `evaluator/`”，不是 `_gold`。不达标说明修复轨迹无效，回滚重跑。**bugfix**：`pre_fix`+`post_fix`；**diagnosis**：仅 `pre_fix`。
- **去 AI 味 + 高中生语言红线**（`user_query` / `success_criteria` 两条共用）：
  - 语言水平按普通高中生：口语化、短句、直白，不写书面腔、不堆专业名词、不总结升华。
  - 不出现「根因」或任何定责/归因表达：`根因`、`原因是`、`问题出在`、`关键在于`、`症结是`、`机制是`、`本质是`、`归根结底`、`这是...导致的` 等一律不写。
  - 不出现 AI 特征词与套话：`不是...而是...`、`并非...而是...`、`恰恰相反`、`换言之`、`也就是说`、`综上所述`、`总而言之`、`值得注意的是`、`由此可见`、`从而`、`进而`、`此外`、`更重要的是`、`需要指出的是` 等一律不写。
  - 这两条只写「现象 + 验收事实」，不解释原因、不分析机制、不总结规律。
  - **不出现生僻/序号字符**：`①②③④…`、`⑴⑵⑶…`、`ⅠⅡⅢ…`、`㈠㈡㈢…`、`ⒶⒷ…` 等一律不用；需要编号时用普通写法，如 `1. 2. 3.` 或用「；」直接分隔。
- 每行都要与真实复跑一致，不能为了去同质化而编造。

## 作弊红线

- 禁止虚假数据、篡改轨迹、根据结果反推轨迹；一经发现所有数据不结算并处罚。
- diagnosis 类型一定不能有文件变更；复现用临时文件允许但必须自删。
- 同一 API KEY 同一时间只能一个 IP 使用。
- 轨迹期间尽量断网：默认禁 `WebFetch` / `WebSearch` / `Bash(git clone *)` / `Bash(curl *)` / `Bash(wget *)`。
- 禁止让测试模型读到历史 commit、本地 `_gold/` 或任何答案线索。
