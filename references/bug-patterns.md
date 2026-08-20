# 深度埋错模式库（随机选用）

配套脚本：`python3 <skill>/scripts/pick_bug_pattern.py`（随机抽取，支持按类别过滤、排除已用模式）。
同一 repo 最多 30 条记录。优先轮完 P1–P12 后再复用模式骨架；复用时必须更换业务层次、机制组合、触发条件和用户可见症状，确保不是同一个 bug。`--exclude` 只用于排除近期已用模式，不要在轮完 12 种后继续排除全部模式。

每个模式都按「多文件协同失效」设计，目标是天然满足复杂度红线（gold 修复同时覆盖至少 4 个功能代码文件且增删总行数至少 20 行），
且单看任何一个文件都看不出完整问题。埋完一律执行 rules.md 的「埋错自检清单」。

通用红线（所有模式适用）：

- buggy 代码必须能 `go build ./...`（注意删改后悬空的 import）。
- 不留任何「故意埋错」的注释；不改测试暴露答案。
- 症状要能被评分测试稳定捕获（20/20 全红）；并发类必须加 `-race` 仍稳定。
- 每个模式必须落实标注的主机制和耦合机制；只实现其中一个局部错误不算采用该模式。
- 触发至少包含 2 个有顺序的步骤，症状跨至少 3 个模块/包；目标测试至少断言 2 个相关的用户可见结果。
- 纯索引、边界、容量、单 `%w`、单 nil 判断或单状态漏边不得成为根本修复点。

---

## P1 锁范围漂移 + 内部引用逃逸（concurrency并发问题）

- **机制组合**：`concurrency_sync` + `shared_state_pollution`。

- **埋点分布（4 文件）**：
  1. store 层：`Get`/`List` 直接返回内部 map/slice 的引用（不拷贝），且读路径不加锁；
  2. service 层：拿到引用后交给 goroutine 异步迭代/聚合；
  3. worker 层：并发调用 store 的写方法；
  4. cache 层：把逃逸引用再存一份，污染面扩大。
- **症状**：偶发 `concurrent map read and map write` panic，或 `-race` 报 data race；结果偶发缺条目/脏数据。
- **定位难点**：panic 栈指向迭代处（service），真正的错在 store 的返回约定和锁范围；只看任何一层都"没毛病"。
- **gold 修复形状**：store 返回深拷贝 + 读路径补锁；service 改为快照迭代；worker 写路径收窄锁粒度；cache 只保存独立快照。至少 4 文件、20 行以上。
- **评分测试要点**：并发读写压测 + `-race`；断言最终聚合结果完整。

## P2 WaitGroup 计数错配 + channel 生命周期错位（concurrency并发问题）

- **机制组合**：`channel_lifecycle` + `concurrency_sync`。

- **埋点分布（4 文件）**：
  1. producer：错误分支提前 `return`，不 `close(ch)`；
  2. coordinator：`wg.Add(1)` 写在 goroutine **内部**（`Wait` 可能先于 `Add` 通过）；
  3. consumer：`for range ch` 依赖 close 退出，且错误只写入无缓冲 errCh、无人读时阻塞。
  4. result/collector：只等待结果 channel，不同时监听错误与取消，放大生命周期错位。
- **症状**：测试偶发超时挂死，或结果偶发少条目；触发错误分支时必挂。
- **定位难点**：挂死点在 consumer，根子在 producer 的关闭约定与 coordinator 的计数时机，三处要一起看。
- **gold 修复形状**：`Add` 移到 spawn 前；`defer close(ch)` 覆盖所有分支；errCh 改带缓冲或 select 发送；collector 同时处理结果、错误与取消。至少 4 文件、20 行以上。
- **评分测试要点**：构造错误分支输入 + 正常输入各一组；`-race -timeout 30s`；断言条目数。

## P3 错误归一化错位 + 重试与事务结果误判（error异常错误）

- **机制组合**：`error_retry` + `transaction_lifecycle`。
- **埋点分布（4 文件）**：
  1. adapter 层：把下游的可重试错误和业务拒绝错误都归一成同一种无类型错误，丢掉 retry-after 与临时性信息；
  2. repository/transaction 层：收到归一化错误后仍提交部分写入，并把 commit 结果覆盖到原错误上；
  3. service 层：按错误类别决定重试，但读取不到类型后把业务拒绝也放进重试队列；
  4. worker/handler 层：重试成功后重复执行已提交的副作用，最终响应又使用第一次错误的状态。
- **症状**：一次业务拒绝会被重复请求并留下部分写入；临时错误重试成功后出现重复记录，响应仍报失败。
- **定位难点**：错误类型、事务边界和重试幂等性互相影响；任何一层单独修好都仍会留下至少一个症状。
- **gold 修复形状**：adapter 保留类型与元数据；transaction 只在成功路径提交并合并错误；service 按 typed error 决策；worker 使用幂等键并以最终结果映射响应。至少 4 文件、20 行以上。
- **评分测试要点**：先触发业务拒绝再触发临时错误重试；同时断言调用次数、事务副作用、错误类型与最终响应。

## P4 零值路径级联 + typed-nil 接口旁路（nil相关问题）

- **机制组合**：`typed_nil_dispatch` + `shared_state_pollution`。

- **埋点分布（4 文件）**：
  1. config/loader 层：某缺省分支返回的结构体漏初始化内部 map（nil map）；
  2. 构造层：返回接口时把 nil 具体类型指针装进接口（typed-nil），调用方 `!= nil` 判断恒为 true；
  3. service 层：用 `len(m) == 0` 判"无配置走默认"，另一条路径却直接向 m 写入；
  4. 校验层：typed-nil 导致空校验器被当成有效校验器，静默放行。
- **症状**：特定缺省配置下偶发 `assignment to entry in nil map` panic，或校验被静默跳过。
- **定位难点**：panic 点在写入处，根子在 loader 的零值分支和构造层的接口返回；两条症状（panic + 旁路）指向不同文件。
- **gold 修复形状**：构造函数统一初始化；接口返回改显式 `return nil`；写路径判空补建；校验层恢复空值拒绝。至少 4 文件、20 行以上。
- **评分测试要点**：覆盖缺省配置路径；同时断言 panic 消失与校验确实生效。

## P5 取消传播断点 + ctx 入结构体复用（context相关问题）

- **机制组合**：`context_lifecycle` + `shared_state_pollution`。

- **埋点分布（4 文件）**：
  1. middleware/入口层：创建了带超时的 ctx，但向下传的是 `context.Background()`；
  2. store 层：把首个请求的 ctx 存进结构体字段复用（首请求 deadline 污染后续所有请求）；
  3. worker 层：循环重试时忽略 `ctx.Err()`，取消后仍继续打请求。
  4. client/adapter 层：下游调用重新创建 `Background`，再次切断取消链。
- **症状**：超时设置不生效（该断不断）；或服务运行一段时间后所有请求立刻超时（旧 deadline 污染）；取消后仍有残留请求。
- **定位难点**：三处错互相掩盖——传播断点让超时"看似没配"，结构体复用又让它"偶发全挂"，症状随请求顺序变化。
- **gold 修复形状**：入口层向下传正确 ctx；store 去掉 ctx 字段改参数传递；worker 补 `ctx.Err()` 检查；client 使用传入 ctx。至少 4 文件、20 行以上。
- **评分测试要点**：带 deadline 的测试卡时间上限；先后两个请求验证互不污染。

## P6 切片所有权逃逸 + 异步消费与跨请求污染（slice相关问题）

- **机制组合**：`shared_state_pollution` + `concurrency_sync`。
- **埋点分布（4 文件）**：
  1. parser 层：复用接收缓冲区并把其中的子切片直接放进领域对象，下一次解析会覆盖旧对象；
  2. service 层：把这些对象交给 goroutine 延迟聚合，异步读取与下一次解析重叠；
  3. cache 层：直接保存调用方切片，返回时也暴露内部切片，使污染跨请求保留；
  4. exporter 层：先缓存后异步导出，同一批数据在响应与导出结果中出现不同内容。
- **症状**：第一批响应正确但稍后的导出混入第二批内容；再次查询第一批缓存时内容也被改写，`-race` 可报告共享访问。
- **定位难点**：不是长度或容量算错，而是所有权在 parser、service、cache、exporter 间没有明确交接，只有两批请求按顺序重叠才暴露。
- **gold 修复形状**：parser 脱离复用缓冲区；service 向异步任务交付不可变快照；cache 入库与出库都复制；exporter 只持有自己的快照。至少 4 文件、20 行以上。
- **评分测试要点**：按“提交第一批 → 阻塞导出 → 提交第二批 → 放行导出 → 回查第一批”触发；断言响应、导出和缓存三处一致，并加 `-race`。

## P7 循环 defer 堆积 + 命名返回值吞错 + 错误分支漏释放（defer相关问题）

- **机制组合**：`resource_lifecycle` + `transaction_lifecycle`。

- **埋点分布（4 文件）**：
  1. batch 层：循环体内 `defer f.Close()`/`defer tx.Rollback()`，堆积到函数尾才执行，句柄/连接耗尽；
  2. repo 层：`defer func() { err = tx.Commit() }()` 配合命名返回值，把先前的业务 err 覆盖吞掉；
  3. service 层：某错误分支提前 return，跳过资源释放/回滚。
  4. metrics/audit 层：在提交前记录成功，回滚后仍留下错误的成功状态。
- **症状**：批量任务跑到中途报"too many open files"/连接池耗尽；出错时数据却被提交了，错误信息还丢了。
- **定位难点**：资源耗尽的报错点远离循环 defer；吞错让日志失真，误导排查方向。
- **gold 修复形状**：循环体抽成独立函数让 defer 即时生效；commit/rollback 错误合并而非覆盖；错误分支补释放；成功审计移到提交完成后。至少 4 文件、20 行以上。
- **评分测试要点**：批量规模要大于句柄限额可暴露的阈值；断言出错时未提交且原始错误保留。

## P8 重试状态机 + 幂等副作用与旧缓存回写（其他问题）

- **机制组合**：`state_machine_idempotency` + `shared_state_pollution`。
- **埋点分布（4 文件）**：
  1. model 层：重试 attempt 与幂等键没有作为一次状态转换的共同版本，旧任务仍能覆盖新状态；
  2. service 层：失败重试时创建新 attempt，却沿用会重复执行外部副作用的操作标识；
  3. worker 层：重试成功写终态后，首轮延迟回调又用旧版本写回进行中；
  4. cache/query 层：缓存没有按版本拒绝旧回写，列表与详情看到不同终态。
- **症状**：重试实际成功且外部操作执行两次，任务随后又从成功退回进行中；列表和详情状态不一致。
- **定位难点**：需要还原“首轮失败 → 重试成功 → 首轮延迟回调”顺序，并同时追踪状态版本、幂等键和缓存写入。
- **gold 修复形状**：model 引入单调版本契约；service 为副作用使用稳定幂等键；worker 做 compare-and-swap 式终态写入；cache/query 拒绝旧版本并统一读取语义。至少 4 文件、20 行以上。
- **评分测试要点**：用同步点强制延迟回调最后到达；断言副作用仅一次、终态不回退、列表与详情一致。

## P9 取消后 goroutine 残留 + 重试风暴（context相关问题）

- **机制组合**：`context_lifecycle` + `channel_lifecycle`。
- **埋点分布（4 文件）**：入口创建 deadline；dispatcher 派生任务却换成 Background；worker 取消后仍向结果 channel 发送；retry scheduler 不监听 Done 并继续扩散任务。
- **症状**：请求超时返回后后台调用数仍增长；随后关闭服务时等待 worker 超时，结果 channel 还有残留写入。
- **定位难点**：入口看似设置了超时，泄漏发生在 dispatcher 和 scheduler，最终挂点却在 shutdown。
- **gold 修复形状**：贯通 ctx、定义 channel 关闭所有权、取消后停止重试、shutdown 等待真实任务集合。至少 4 文件、20 行以上。
- **评分测试要点**：超时后记录调用数，再执行 shutdown；断言调用数不再增长、关闭及时且无 channel 写入 panic。

## P10 回滚错误覆盖 + 成功审计提前发布（error异常错误）

- **机制组合**：`transaction_lifecycle` + `error_retry`。
- **埋点分布（4 文件）**：repository 延迟回滚覆盖业务错误；service 在 commit 前发布成功事件；publisher 失败后重试整笔事务；audit 把第一次成功事件保留为最终状态。
- **症状**：事务实际回滚但外部已收到成功事件；自动重试后写入成功，审计里却同时存在成功、失败和重复事件。
- **定位难点**：数据库、事件和审计各自看似有记录，必须按事务时间线对齐才能发现发布边界错误。
- **gold 修复形状**：保留原错误并合并 rollback 错；只在 commit 后发布；重试使用幂等 outbox；audit 按事件版本收敛。至少 4 文件、20 行以上。
- **评分测试要点**：第一次发布失败、第二次成功；断言提交次数、事件次数、错误链和审计终态。

## P11 panic 恢复后继续使用半初始化状态（nil相关问题）

- **机制组合**：`panic_recovery` + `shared_state_pollution`。
- **埋点分布（4 文件）**：builder 分阶段写共享对象后 panic；recovery 把 panic 转错误却返回半成品；cache 保存半成品；后续 handler 将其当成完整对象继续处理。
- **症状**：首请求只返回普通错误，没有崩溃；下一次读取同一对象却 panic，其他请求还能看到首请求留下的部分字段。
- **定位难点**：真正 panic 出现在第二次请求，污染发生在第一次 panic 被恢复之后，栈无法直接指出首轮写入。
- **gold 修复形状**：builder 使用局部对象原子发布；recovery 不返回半成品；cache 只接收已验证对象；handler 对错误结果停止后续处理。至少 4 文件、20 行以上。
- **评分测试要点**：先触发构建 panic，再读取同 key 和不同 key；断言无半成品、无二次 panic、请求间隔离。

## P12 对象池复用未清理 + 跨请求身份污染（其他问题）

- **机制组合**：`shared_state_pollution` + `context_lifecycle`。
- **埋点分布（4 文件）**：pool 归还对象前未清理身份与切片；middleware 把请求 ctx 信息写入池对象；service 异步持有对象超过请求结束；logger/adapter 在对象归池后继续读取。
- **症状**：高并发下第二个请求偶尔带上前一个用户身份；请求结束后的日志和下游调用出现错租户数据，`-race` 可见复用读写冲突。
- **定位难点**：数据来源正确，污染发生在对象归还与异步读取交错时，只看认证或日志层都无法解释。
- **gold 修复形状**：明确 pool 对象所有权和清理函数；middleware 只写请求快照；service 在归还前等待或复制；logger/adapter 不持有可复用对象。至少 4 文件、20 行以上。
- **评分测试要点**：同步控制 A 请求归还后 B 请求复用，再放行 A 的异步读取；断言身份、日志、下游参数隔离并加 `-race`。

---

## 选用与登记

1. 抽取：`python3 <skill>/scripts/pick_bug_pattern.py --count 1`；同 repo 后续记录加 `--exclude P1,P3`（已用的）。
2. 需要指定类别配比时用 `--category concurrency并发问题` 过滤后再抽。
3. 模式是骨架不是模板：**埋点要落在当前项目自己的业务层次上**，文件名、症状表述随项目走，禁止跨 repo 复刻同一份代码。
4. 选定后执行：埋错 → 本地量改动规模 → 写题面与目标测试 → `difficulty_review.py check` → 红绿校准。难度审查不过不得继续。
