# 收集表 21 字段填写口径

表头（以甲方 2026-08 新收集表为准，21 列）：

```
sample_id | session id | bug_id | task_type | bug_category | repo_url | go_version |
repro_determinism | user_query | trajectory | verify_cmds | gold_root_cause |
success_criteria | verify_result | harness | generator_model | 做题人 | 创建人 | 质检结果 | 质检备注 | 是否同步飞书
```

## 字段口径

| 字段 | 口径 |
|------|------|
| sample_id | 纯数字，全局唯一；填前先查全表已占用编号 |
| session id | 测试模型轨迹的 session uuid（jsonl 文件名） |
| bug_id | 描述性命名，如 `项目-缺陷-机制-序号` 或 `姓名-go-日期-序号` |
| task_type | `bugfix` / `diagnosis`；同一个 bug 只能二选一，不得同时出两条 |
| bug_category | 只允许以下取值（原样填写）：`concurrency并发问题` / `slice相关问题` / `error异常错误` / `nil相关问题` / `context相关问题` / `defer相关问题` / `其他问题`；优先前六类，确属其他才用 `其他问题` |
| repo_url | **`bug-<record>` 分支地址**（GitHub `https://github.com/<owner>/<repo>/tree/bug-<record>`）；bugfix 题跑完轨迹后该分支已包含 Claude Code 测试模型的修复 commit，diagnosis 题仍是埋好 bug 的代码 |
| go_version | 三段式，ASCII 分号 `;` 分隔：基础镜像 tag + go.mod 指令 + GOTOOLCHAIN |
| repro_determinism | 只填 `deterministic` / `flaky`；flaky 不做 bugfix |
| user_query | 纯文本、简短口语化、高中生语言的**纯提示词**：现象 + 人味化废话 + 环境交代 + 任务指令；**不写任何验收/复现/运行命令、不贴命令代码块**（`verify_cmds` 独立维护）；确需展示症状代码时最小复现 ≤3 行；bugfix 明确写「帮我修好」，diagnosis 明确写「先别改代码」；不出现根因/原因，不用「不是...而是...」等 AI 词 |
| trajectory | 腾讯 COS 上传链接；本地 `<session_id>.jsonl` 作为附件一并交付（必须是 Claude Code 自己落盘的原始 session 轨迹文件，不是 stdout 捕获拼装的文件） |
| verify_cmds | **bugfix / diagnosis 都必填**：只写一条目标 Bug 定向复现命令，明确唯一目标包、精确测试名和 `-count=1`，如 `go test ./path/to/pkg -run '^TestTargetBug$' -count=1`（并发加 `-race`）；禁止 `go test ./...`、通配包、当前目录、多包、多测试及拼接回归命令。目标测试必须完整覆盖 `user_query` 描述的全部现象与触发条件；红/绿证据轨迹必须各自只实际执行一次这条命令，实际 Bash 调用、最终回复【命令】与正式填表值逐字符一致，空格、引号、路径和参数顺序也不得变化；bugfix 校验红+绿，diagnosis 校验红 |
| gold_root_cause | **diagnosis 必填**：紧凑三项式 `文件: ... 符号: ... 机制: ...`，每项附可复核位置；bugfix 也建议填（供排查阶段 QC） |
| success_criteria | **必须是本条数据专属的业务验收摘要**，并原样复用 `user_query` 中至少一个 4 字以上业务短语。bugfix 写「具体业务触发在埋错态 20/20 出现的异常、修复态 20/20 恢复的公开行为、回退后重新出现的业务现象、全量回归」；diagnosis 写「具体业务触发 20/20 出现的异常、输入/接口值到恢复路径再到后续影响的定位链、工作区零改动」。禁止只写代码状态、定向命令、稳定变红、定位文件符号、公开现象、真实复现等通用流程描述；只写真实验收事实，不解释未验证的根因，不用「不是...而是...」等 AI 词 |
| verify_result | 红/绿证据轨迹自动回填的 JSON（上传后由 `run_evidence_trajectories.py generate` 写入）；**bugfix**：`pre_fix`+`post_fix`；**diagnosis**：仅 `pre_fix`。每项含 `trajectory_url` / `session_id` / `result` |
| harness | 生成轨迹的工具名 + 版本号，如 `Claude Code CLI v2.1.233`；禁止只写 `Claude Code CLI` 或只写模型名 |
| generator_model | 实际生成轨迹的模型标识 |
| 做题人 | 实际跑测试轨迹的人 |
| 创建人 | 出题人 |
| 质检结果 | 留给质检人 |
| 质检备注 | 留给质检人 |
| 是否同步飞书 | **本技能不填写**，留空由甲方/录入方处理 |

## 自建 0-1 项目填表要点

> **必填对照（红线）**：bugfix 必填 `verify_cmds` + `verify_result`（`pre_fix`+`post_fix`）；diagnosis 必填 `verify_cmds` + `gold_root_cause` + `verify_result`（仅 `pre_fix`）。

- `repo_url`：填 `bug-<record>` 分支地址；bugfix 题在跑完轨迹并质检后把测试模型修复 commit 推到该分支（分支地址不变，HEAD 变为测试模型 fix），diagnosis 题保持 bug 分支不动。
- 当前 Codex 完成的正确修复只保存在本地 `_gold/`，用于红绿校准、难度检查和回归验证，不创建远程分支、不进入收集表。
- 一个 repo（GitHub 地址或本地路径任一命中）最多 30 条记录，每条一个不同 bug；总需求超过 30 条时拆到多个不同的 0-1 项目和 GitHub 仓库，每仓独立从 001 编号。
- 同一个 bug 只能出一个 task_type：bugfix 或 diagnosis 二选一。
- 交付不再打 zip、不再截图，只提交 GitHub `repo_url` 分支地址。

## BUG_REPRO.md（交付文件，不是收集表字段）

- 每条记录都要随交付分支提供 `BUG_REPRO.md`（本流程所有题目都是埋好 bug 的项目）。
- 内容：Bug 是什么、如何触发、错误信息。
- 只放进 GitHub 交付分支，不放进测试模型的 `env/`。

## 生成脚本要点

用 openpyxl 生成时：表头加粗、列宽 28 左右，长文本列设 `wrap_text=True, vertical="top"`；保存后重新 load 校验表头与行数。

## 本地文件维护

- 每个项目的 21 字段事实源是 `<project>/collection.json`，由 `scripts/collection_table.py` 维护。
- `collection_table.py sync` 产出：
  - 全局一份：`<root>/_shared/收集表_汇总.xlsx`
  - 项目独立一份：`<project>/收集表_<project>.xlsx`
- 本地 jsonl 轨迹存 `<project>/trajectory.jsonl`，每个项目独立。
