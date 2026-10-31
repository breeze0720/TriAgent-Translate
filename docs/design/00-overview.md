# 00 · 总览与图结构：这个多智能体到底长什么样

本篇先给出**图（graph）**的形状与**一次真实执行**的完整轨迹，后续各篇在此基础上展开。
所有内容都来自最终代码 `workflow.py`、`core/runner.py`，以及服务 `http://127.0.0.1:8001` 上的真实任务记录（任务 #3）。

先给结论：这不是"几个 Agent 自由聊天"，而是**一个确定性主控（supervisor）+ 六个专家节点**的星型状态机（state machine）。

## 1. 实现概览

### 1.1 图的形状

```
                         ┌──────────────┐
   START ───────────────▶│  supervisor  │◀──────────────────────────────┐
                         │  （主控节点） │                               │
                         └──────┬───────┘                               │
        stage 路由（条件边，纯函数，只返回节点名）                          │
     ┌──────────┬──────────┬────┴─────┬────────────┬──────────────┐     │
     ▼          ▼          ▼          ▼            ▼              ▼     │
terminology  translate   review     polish   human_review    finalize ──┘
 术语专家     翻译专家    审校专家   润色专家   人在环（暂停）    收尾
     └──────────┴──────────┴──────────┴────────────┴──────────────┘
                          每个专家执行完都回到 supervisor

   supervisor 的 stage == "final" 时 → END（图结束）
```

记住两个要点：

1. **只有 supervisor 决定"下一步是谁"**，专家节点不指定下一跳；
2. 专家节点一律回到 supervisor，所以加一个新专家 = 加一个节点 + 在 `_ROUTES` 加一行映射，不动已有边。

### 1.2 建图代码（`workflow.py` 597-636 行）

```python
builder = StateGraph(TranslationState)
builder.add_node("supervisor", supervisor_node)      # 主控：只决策，不调 LLM、不写库
builder.add_node("terminology", terminology_node)    # 术语抽取（DeepSeek）
builder.add_node("translate", translate_node)        # 分段翻译 / 带意见重译（DeepSeek）
builder.add_node("review", review_node)              # 4 维结构化审校（Qwen）
builder.add_node("polish", polish_node)              # 只改 issues 的定向润色（DeepSeek）
builder.add_node("human_review", human_review_node)  # interrupt() 暂停，等人工
builder.add_node("finalize", finalize_node)          # 判定 done / failed

builder.add_edge(START, "supervisor")
builder.add_conditional_edges("supervisor", route_after_supervisor, {
    "terminology": "terminology", "translate": "translate", "review": "review",
    "polish": "polish", "human_review": "human_review",
    "finalize": "finalize", "end": END,
})
# 专家节点执行完一律回到主控（星型拓扑，便于加新专家）
for node_name in ("terminology", "translate", "review", "polish", "human_review", "finalize"):
    builder.add_edge(node_name, "supervisor")
```

逐段解释：

- `StateGraph(TranslationState)`：图的"共享内存"就是第 01 篇讲的 state，节点通过**返回 dict** 更新它；
- `add_edge(START, "supervisor")`：入口固定是主控，没有任何节点能绕过主控；
- `add_conditional_edges`：条件边只做**字符串映射**——`route_after_supervisor` 读 `state["stage"]`，查 `_ROUTES` 表，返回节点名或 `"end"`；
- `for ... add_edge(node_name, "supervisor")`：这是"星型"的关键，专家之间没有直连边。

### 1.3 一次完整执行的节点顺序（任务 #3，开了人工审核）

下面是从 `task_events` 表拉出来的真实时间线（只列阶段与状态，省略内部版本号）：

| # | stage | status | 说明（来自节点返回的 history.note） |
|---|-------|--------|--------------------------------------|
| 1 | init | running | 任务开始执行 |
| 2 | terminology | running | supervisor 初始化：原文切分为 2 段，进入术语抽取 |
| 3 | terminology_done | running | 术语抽取完成：15 个术语 |
| 4 | translate | running | 术语表就绪，进入翻译 |
| 5 | translated | running | 第 1 次翻译完成：2 段，术语 15 个 |
| 6 | review | running | 翻译完成，进入审校 |
| 7 | reviewed | running | 审校完成：总分 84.65，问题 5 条 |
| 8 | polish | running | 审校总分 84.65，问题 5 条（重试 1/3）→ polish |
| 9 | polished | running | 定向润色完成：处理 5 条问题 |
| 10 | human_review | waiting_human | 等待人工审核（或在 /resume 前可编辑术语表） |
| 11 | human_resolved | running | 人工审核通过（含人工修改） |
| 12 | finalize | running | 人工审核通过 → 收尾 |
| 13 | final | done | 任务完成 |

注意第 10 到 11 之间可能隔了很久（人什么时候点"通过"就什么时候继续），这段时间**图是暂停的、进程里没有线程在跑**——这是第 04 篇的主题。

不开人工审核时（任务 #2）路径更短：`reviewed → polish → polished → finalize → final`。
审校分低于 `PASS_SCORE=80` 时是另一条支路：`reviewed → translate`（带 issues 重译），最多 `MAX_RETRY=3` 次翻译。

### 1.4 决策只发生在一个地方

`supervisor_node` 按 `stage` 分派，核心就这几行（`workflow.py` 284-312 行）：

```python
if stage == "terminology_done":  return go("translate", "术语表就绪，进入翻译")
if stage == "translated":        return go("review", "翻译完成，进入审校")
if stage == "reviewed":          return go(decide_after_review(state), "审校后决策…")
if stage == "polished":          return go(decide_after_polish(state), "润色完成…")
if stage == "human_resolved":    return go(decide_after_human(state), "人工审核结果…")
return go("finalize", f"未知阶段 {stage}，兜底进入收尾")   # 兜底：绝不卡死
```

`go()` 只返回三个字段：`stage`（下一跳）、`status="running"`、`version+1`（外加一条 history）。
三个 `decide_*` 函数都是**纯函数**：只读 state、只返回节点名，不写库、不改 state。

### 1.5 进度百分比与两个"收尾阶段"

`STAGE_PROGRESS`（`workflow.py` 76-90 行）是 SSE、DB、前端共用的唯一口径：

| stage | 进度 | stage | 进度 |
|---|---|---|---|
| init | 1 | polish | 80 |
| terminology | 10 | polished | 88 |
| terminology_done | 15 | human_review | 90 |
| translate | 30 | human_resolved | 95 |
| translated | 45 | finalize | 97 |
| review | 60 | final | 100 |
| reviewed | 70 | | |

注意 `finalize`（97）和 `final`（100）是两个不同的 stage：

- `finalize` 是"supervisor 决定去收尾"的中间态（对应 `finalize` 节点还没跑）；
- `final` 是 `finalize_node` 已经判定完 `done/failed`、图即将结束的终态。

`status` 和 `stage` 也不是一回事：`status` 回答"任务整体什么状态"
（`pending/running/waiting_human/done/failed`），`stage` 回答"走到流程哪一步"。
所以会出现 `status=waiting_human, stage=human_review` 这种组合。

### 1.6 读图时容易误解的三点

1. **专家节点没有"出口判断"**：`translate_node` 里没有 `if 分数低就重试`，
   它只负责生产译文；要不要重试由 supervisor 看完 `review` 后决定。
2. **condition 的返回值是节点名，不是布尔**：`route_after_supervisor` 返回 `"translate"` 或 `"end"`，
   映射表在 `_ROUTES` 里；写错名字会直接在图编译/运行时报错。
3. **supervisor 会执行很多次**：一次任务里它至少跑 7 次（每个专家回来都过一次），
   重试/润色越多跑得越多；每次只花一次字典比较的时间，所以"多一跳"不心疼。

## 2. 设计取舍与备选方案

### 2.1 对比：纯辩论式 / 自由协商的多智能体

错写法（也是很多教程里的默认写法）：

```python
# ❌ 自由协商：每个 Agent 自己决定下一个发言者，直到"达成一致"
while not consensus:
    speaker = llm_pick_next_speaker(messages)     # 每次都要一次 LLM 调用
    messages.append(call(speaker, messages))      # 上下文越来越长、成本指数涨
```

本项目选择"主控-专家 + 有限协商"，理由三条：

1. **成本**：每次 LLM 调用都消耗 token（模型计费单位）和时间。任务 #3 里审校一次花了 26 秒（17:56:08 → 17:56:34），
   任务 #4 的审校花了 72 秒；如果 3 个 Agent 互相辩论 3 轮，就是 9 次调用起步，
   且上下文一轮比一轮长。我们用 `MAX_RETRY=3` 把最坏情况写死：最多 3 次翻译 + 3 次审校。
2. **可控**：整个流程只有一个真值 `stage`，任何时刻都能回答"现在到哪了、为什么重试、还剩几次预算"。
   自由协商里没有"阶段"这个概念，超时、重试、人工介入都无处下手。
3. **可测**：决策函数是纯函数，单测不需要 LLM、不需要数据库（`tests/test_state_machine.py` 就是干这个的），
   用 mock 把审校固定成 60 分就能钉死"最多翻译 3 次后 failed"（`tests/test_retry_no_infinite_loop.py`）。

### 2.2 对比：让专家直接互连

```python
# ❌ 专家互连：加一个专家就要改 N 条边，路由逻辑散落各处
builder.add_edge("translate", "review")
builder.add_edge("review", "polish")        # 那 review 不通过回哪？只能写死在节点里
builder.add_edge("review", "translate")     # 同一条边有两个去向 → 必须再抽条件函数
```

星型拓扑的代价是**多一跳**（每次专家回来都过一次 supervisor，多一条 history、version+1），
但换来"路由只有一个入口"。本项目里这一跳不花钱（supervisor 不调 LLM），所以非常划算。

### 2.3 诚实的取舍

- supervisor **不调用 LLM**，所以它不能"根据内容动态选专家"——它只会按 `stage` 查表。
  任务阶段固定的流水线，这样最简单；如果任务是开放式研究（先搜再算再写），就需要 Planner 了。
- 专家之间**不直接对话**，翻译专家看不到审校专家的"人格"，只看到一个 `issues` 列表。
  这是刻意的：把协商压缩成"结构化意见"，比自然语言辩论更省 token、更好断言。
- 这是"有限协商"：审校不满意 → 带着 issues 重译，最多 3 次；再不行就转人工或失败，**不会无限扯皮**。

## 3. 常见问题（FAQ）

**Q1：你这个算"多智能体"吗，还是就是个流水线？**
A：是主控-专家（supervisor）模式的多智能体：四个专家各有独立 prompt、模型和结构化输出契约，
由 supervisor 做状态机决策；协商被限制在"审校给 issues → 翻译定向修正"这一条有预算上限的回路里，
所以它比自由辩论更可控、更可测。

**Q2：为什么每个专家执行完都要回到 supervisor，而不是直接连下一个节点？**
A：为了让"下一步是谁"只有一个决策点：加专家只改路由表、不碰已有边；所有分支都能用纯函数单测；
条件边里不写库，重放时不会重复产生副作用。

**Q3：为什么不用 AutoGen / 自由辩论式多智能体？**
A：翻译任务的阶段是固定的、可分解的，辩论带来的质量收益抵不过 LLM 调用成本与延迟；
而且本项目的验收点是断点恢复和人工审核，需要确定的阶段与预算，状态机是更合适的抽象。

---

**一句话总结**：主控只做决策、专家只做本职、边只做映射——这三条把"多智能体"从玄学变成了可调试的状态机。