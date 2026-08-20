#!/usr/bin/env python3
"""随机抽取深度埋错模式（详细埋法见 references/bug-patterns.md）。

同一 repo 最多 30 条记录。优先轮完 P1-P12 后再复用模式骨架；复用时必须改变具体业务埋点和症状。

用法:
  pick_bug_pattern.py                           # 随机抽 1 个
  pick_bug_pattern.py --count 30                # 按随机轮次抽取，轮内不重复
  pick_bug_pattern.py --category concurrency并发问题   # 按 bug_category 过滤后抽
  pick_bug_pattern.py --exclude P1,P3           # 排除同 repo 已用模式
  pick_bug_pattern.py --list                    # 列出全部模式
"""
import argparse
import json
import random
import sys

PATTERNS = [
    {"id": "P1", "name": "锁范围漂移 + 内部引用逃逸", "category": "concurrency并发问题",
     "primary": "concurrency_sync", "coupled": ["shared_state_pollution"],
     "files": "store + service + worker + cache",
     "summary": "store 返回内部引用且读不加锁，service 异步迭代，worker 并发写；偶发 map 并发 panic / data race"},
    {"id": "P2", "name": "WaitGroup 计数错配 + channel 生命周期错位", "category": "concurrency并发问题",
     "primary": "channel_lifecycle", "coupled": ["concurrency_sync"],
     "files": "producer + coordinator + consumer + collector",
     "summary": "错误分支不 close、Add 在 goroutine 内、errCh 无人读阻塞；偶发挂死或少条目"},
    {"id": "P3", "name": "错误归一化错位 + 重试与事务结果误判", "category": "error异常错误",
     "primary": "error_retry", "coupled": ["transaction_lifecycle"],
     "files": "adapter + repository + service + worker/handler",
     "summary": "错误类型与元数据丢失，事务仍提交，业务拒绝被重试，临时错误成功后又重复副作用"},
    {"id": "P4", "name": "零值路径级联 + typed-nil 接口旁路", "category": "nil相关问题",
     "primary": "typed_nil_dispatch", "coupled": ["shared_state_pollution"],
     "files": "loader + 构造层 + service (+校验层)",
     "summary": "缺省分支漏初始化 nil map + typed-nil 判空恒真；panic 或校验静默旁路"},
    {"id": "P5", "name": "取消传播断点 + ctx 入结构体复用", "category": "context相关问题",
     "primary": "context_lifecycle", "coupled": ["shared_state_pollution"],
     "files": "middleware + store + worker + client/adapter",
     "summary": "下传 Background、ctx 存字段复用、重试忽略 ctx.Err；超时不生效或全部立刻超时"},
    {"id": "P6", "name": "切片所有权逃逸 + 异步消费与跨请求污染", "category": "slice相关问题",
     "primary": "shared_state_pollution", "coupled": ["concurrency_sync"],
     "files": "parser + service + cache + exporter",
     "summary": "复用缓冲区的子切片被异步消费并进入缓存；两批请求重叠后响应、导出和缓存内容分叉"},
    {"id": "P7", "name": "循环 defer 堆积 + 命名返回值吞错 + 漏释放", "category": "defer相关问题",
     "primary": "resource_lifecycle", "coupled": ["transaction_lifecycle"],
     "files": "batch + repo + service + metrics/audit",
     "summary": "循环内 defer 耗尽句柄、defer Commit 覆盖业务 err、错误分支跳过回滚"},
    {"id": "P8", "name": "重试状态机 + 幂等副作用与旧缓存回写", "category": "其他问题",
     "primary": "state_machine_idempotency", "coupled": ["shared_state_pollution"],
     "files": "model + service + worker + query",
     "summary": "重试成功后首轮延迟回调覆盖终态，外部副作用重复，缓存令列表与详情状态分叉"},
    {"id": "P9", "name": "取消后 goroutine 残留 + 重试风暴", "category": "context相关问题",
     "primary": "context_lifecycle", "coupled": ["channel_lifecycle"],
     "files": "entry + dispatcher + worker + scheduler",
     "summary": "请求超时后任务和重试继续扩散，结果 channel 残留写入，shutdown 等待超时"},
    {"id": "P10", "name": "回滚错误覆盖 + 成功审计提前发布", "category": "error异常错误",
     "primary": "transaction_lifecycle", "coupled": ["error_retry"],
     "files": "repository + service + publisher + audit",
     "summary": "事务回滚但成功事件已发布，重试后产生重复事件，审计状态互相冲突"},
    {"id": "P11", "name": "panic 恢复后继续使用半初始化状态", "category": "nil相关问题",
     "primary": "panic_recovery", "coupled": ["shared_state_pollution"],
     "files": "builder + recovery + cache + handler",
     "summary": "首请求 panic 被转成普通错误却缓存半成品，后续请求读取时二次 panic 并看到部分状态"},
    {"id": "P12", "name": "对象池复用未清理 + 跨请求身份污染", "category": "其他问题",
     "primary": "shared_state_pollution", "coupled": ["context_lifecycle"],
     "files": "pool + middleware + service + logger/adapter",
     "summary": "池对象归还后仍被异步读取并被下一请求复用，导致身份、日志和下游参数串请求"},
]


def main():
    p = argparse.ArgumentParser(description="随机抽取深度埋错模式")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--category", help="按 bug_category 过滤（如 concurrency并发问题）")
    p.add_argument("--exclude", default="", help="逗号分隔的已用模式 id，如 P1,P3")
    p.add_argument("--list", action="store_true", help="列出全部模式")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = p.parse_args()

    pool = PATTERNS
    if args.list:
        for x in pool:
            mechanisms = x["primary"] + "+" + ",".join(x["coupled"])
            print(f"{x['id']}  [{x['category']}]  {x['name']}  [{mechanisms}]  ({x['files']})")
        return

    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    unknown = excluded - {x["id"] for x in PATTERNS}
    if unknown:
        sys.exit(f"未知模式 id: {', '.join(sorted(unknown))}（可用: {', '.join(x['id'] for x in PATTERNS)}）")
    pool = [x for x in pool if x["id"] not in excluded]
    if args.category:
        pool = [x for x in pool if x["category"] == args.category]
    if not pool:
        sys.exit("过滤后没有可用模式：放宽 --category 或减少 --exclude")
    if args.count < 1:
        sys.exit("--count 必须大于 0")

    picked = []
    while len(picked) < args.count:
        cycle = random.sample(pool, len(pool))
        if picked and len(cycle) > 1 and cycle[0]["id"] == picked[-1]["id"]:
            cycle[0], cycle[1] = cycle[1], cycle[0]
        picked.extend(cycle[:args.count - len(picked)])
    if args.json:
        print(json.dumps(picked, ensure_ascii=False, indent=2))
        return
    for x in picked:
        print(f"✅ {x['id']}  {x['name']}")
        print(f"   类别: {x['category']}")
        print(f"   机制: {x['primary']} + {', '.join(x['coupled'])}")
        print(f"   埋点: {x['files']}")
        print(f"   故障链: {x['summary']}")
        print(f"   详细埋法: references/bug-patterns.md 的「{x['id']}」小节")


if __name__ == "__main__":
    main()
