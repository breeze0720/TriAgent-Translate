# 01 · State 设计：覆盖型 vs 累积型，reducer 到底解决什么问题

LangGraph 的节点之间不直接传参，它们共享一份 **state（状态）**，每个节点返回"我要改哪些字段"。
本篇拆开 `workflow.py` 的 `TranslationState`：哪些字段是覆盖型（每次重写），哪些是累积型（每次追加），
`Annotated[list, operator.add]` 和 `version` 各自为什么存在。

## 1. 这段代码在做什么

### 1.1 状态定义（`workflow.py` 41-72 行，节选）

```python
class TranslationState(TypedDict, total=False):
    """total=False 表示节点只需返回自己修改的字段（局部更新）。"""

    # ---- 入参（API 层写入，之后只读）----
    task_id: int
    source_lang: str
    target_lang: str
    source_text: str
    style: str
    domain: str
    enable_human_review: bool

    # ---- 业务数据：全部是"覆盖型"，节点返回整份新值 ----
    segments: list[dict]        # [{"index": 0, "text": "..."}]
    glossary: list[dict]        # [{"term", "translation", "note"}]
    translations: list[dict]    # [{"index", "source", "text", "attempt"}]
    translation: str            # 当前（最终）译文
    review: dict                # 4 维评分 + overall + issues
    review_unavailable: bool    # 审校结构化输出不可用（不允许静默给 50 分）
    human_decision: dict | None # {approved, edited, comment, at}

    # ---- 运行控制 ----
    retry_count: int
    max_retries: int
    status: str                 # pending/running/waiting_human/done/failed
    stage: str                  # init/terminology/translate/.../final
    version: int                # 覆盖型：每次节点写入 +1
    error: str

    # ---- 累积型 channel：operator.add 保证每版记录都追加而不是覆盖 ----
    history: Annotated[list[dict], operator.add]
```

逐段解释：

- `total=False`：节点返回值是**局部更新**。`terminology_node` 只返回 `glossary / stage / version / history` 四个键，
  其余字段保持原值。如果是 `total=True`（默认），类型检查会要求你返回所有字段，节点就被迫当"全量搬运工"。
- 除 `history` 外，所有字段都是覆盖型：节点返回什么，state 里就是什么。
  `translations` 虽然是列表，但每次翻译是"整批重算"，所以整份替换才是对的。
- `history: Annotated[list[dict], operator.add]`：带 **reducer（归约函数）** 的 channel。
  LangGraph 不会用新值覆盖旧值，而是执行 `旧值 + 新值`，即把节点返回的那条记录**追加**到列表尾部。
- `version` 是覆盖型，但没有 reducer，靠每个节点自己 `+1`：

```python
def _version_bump(state: TranslationState) -> int:
    return int(state.get("version") or 0) + 1     # 每个节点返回时写回这个值
```

### 1.2 一次执行里 version / history 怎么长

以任务 #3 为例，state 的演化（只看关键字段）：

| 节点 | 返回的 stage | version | history 追加的 note |
|------|--------------|---------|---------------------|
| （初始） | init | 0 | （空列表） |
| supervisor | terminology | 1 | 初始化：原文切分为 2 段，进入术语抽取 |
| terminology | terminology_done | 2 | 术语抽取完成：15 个术语 |
| supervisor | translate | 3 | 术语表就绪，进入翻译 |
| translate | translated | 4 | 第 1 次翻译完成：2 段，术语 15 个 |
| supervisor | review | 5 | 翻译完成，进入审校 |
| review | reviewed | 6 | 审校完成：总分 84.65，问题 5 条 |
| supervisor | polish | 7 | 审校总分 84.65，问题 5 条（重试 1/3）→ polish |
| polish | polished | 8 | 定向润色完成：处理 5 条问题 |
| supervisor | human_review | 9 | 润色完成，进入人工审核/收尾 |
| human_review（resume 后） | human_resolved | 10 | 人工审核通过（含人工修改） |
| supervisor | finalize | 11 | 人工审核通过 → 收尾 |
| finalize | final | 12 | 任务收尾：done |
| supervisor | final | 13 | 工作流结束（done） |

`history` 的每条形如：

```python
{"stage": "translate", "at": "2026-10-22T17:56:08+08:00", "note": "第 1 次翻译完成：2 段，术语 15 个"}
```

`stage` 用来分组、`at` 给前端画时间轴、`note` 给人看（也是 SSE 事件的默认文案，
`core/runner.py` 的 `_last_note()` 就取 `history[-1]["note"]`）。

### 1.3 初始 state 从哪来（`build_initial_state`，651-681 行）

图执行的第一个参数是 `build_initial_state(task)`：从 DB 的任务行**显式**拼出完整 state。

```python
return {
    "task_id": int(task["id"]),
    "source_text": task.get("source_text") or "",
    ...                                   # 入参照抄
    "segments": [], "glossary": [], "translations": [], "translation": "",
    "polished": "", "review": {}, "review_unavailable": False,
    "human_decision": None,               # ← 运行态字段全部显式清空
    "retry_count": 0, "max_retries": config.MAX_RETRY,
    "status": "running", "stage": "init",
    "version": int(task.get("version") or 0) if restart else 0,
    "error": "", "history": [],
}
```

两个细节：

- **为什么每个字段都写出来**：`/retry` 重新执行时如果漏清 `review` 或 `human_decision`，
  新一遍会带着上一轮的评分/审核结论跑，出现"没审校却已经 approved"的怪状态；
- **`restart=True` 时沿用 DB 的 version**（`/retry` 走这条）：保证版本号单调递增，
  不会和上一轮留下的事件撞 `(task_id, stage, version)` 唯一键。

对照 `total=False` 的局部更新：节点只返回自己改的字段，但**初始状态必须完整**——
这两件事不矛盾，一个是"起点要给全"，一个是"过程可以只给增量"。

## 2. 为什么这样写（对比错写法）

### 2.1 如果 `history` 也用覆盖型，会出什么错

把 `Annotated[list[dict], operator.add]` 删掉，改成普通 `history: list[dict]`，节点返回的仍然是：

```python
return {"history": [_history("translate", "第 1 次翻译完成…")]}   # 只有一个元素的列表
```

那么每次节点写入都会**整块替换** `history`。跑完之后：

- 前端时间线只剩最后一条（`工作流结束（done）`），看不到术语、翻译、审校发生过什么；
- `core/runner.py` 的 `_last_note()` 只能拿到最后一条 note，SSE 消息全部退化成"最后一步"；
- 断点恢复的验收点直接失败——`tests/test_checkpointer_recovery.py` 断言
  `any("初始化" in item["note"] for item in resumed["history"])`，覆盖型下这条必挂。

正确写法是让 reducer 帮忙拼接，节点只负责产出"本次新增的一条"。

### 2.2 如果 `version` 不自增会怎样

`version` 被三处依赖：

1. `task_events` 表的唯一键是 `(task_id, stage, version)`，靠它做**幂等 upsert**（第 05 篇）；
   version 不变，同一个 stage 的事件永远只有一行，重试、润色、人工审核的时间线全部塌缩；
2. SSE 事件里带 version，前端可据此判断"这条比本地新"；
3. 人工编辑术语表时 `main.py` 会 `update_state` 并把 version 设成 `旧值 + 1`（448-461 行），
   避免与图自己写入的事件撞键。

错写法：把 version 当"重试次数"用（v1 旧代码就是 `version = state["retry_count"] + 1`，
见 `_archive/old-annotate_backup/02-多智能体翻译-LangGraph__workflow.py` 第 103 行）。
重试次数只在翻译时变化，审校、润色、人工审核拿到的 version 相同，
`(stage, version)` 还是会撞，而且"版本"和"重试"两个概念被混在一起，排查时非常痛苦。

### 2.3 为什么 `translations` / `segments` 又坚持用覆盖型

它们是"当前这一版的完整事实"，不是"历史流水"：

- `segments` 只在初始化时算一次，之后是只读的；
- `translations` 每次翻译都重新生成整批（第 N 次翻译覆盖第 N-1 次），
  历史版本靠 `history` 的 note 和 `task_events` 记录，不需要在 state 里堆 3 份译文。

如果给 `translations` 也加 `operator.add`，第二次翻译会在旧列表后面再追加一批，
`translation = "\n\n".join(item["text"] for item in translated)` 就会混入上一版，译文直接错乱。
**判断标准：这个字段是"最新事实"还是"过程记录"？前者覆盖，后者累积。**

### 2.4 诚实的取舍

- `operator.add` 只会拼接，不会去重、不会截断。`history` 长度 = 节点执行次数，长任务会一直涨，
  目前没有做上限；真要做，得换成自定义 reducer（比如只保留最近 100 条）。
- 节点如果在 checkpoint 提交前失败并被重跑，它返回的那条 history 会再追加一次——
  这属于"可重放"的边界：副作用（写库）已经全部挪到 runner，所以最多多一条日志，不会重复扣费。
- `review_unavailable` 单独占一个布尔字段，而不是塞进 `review` 里，
  是为了让 `decide_after_review()` 能用一个明确条件分支，而不是猜 `overall == 0` 是不是真的 0 分。

## 3. 面试官可能怎么问

**Q1：LangGraph 的 reducer 是什么？你为什么用 `Annotated[list, operator.add]`？**
A：reducer 决定"新值和旧值怎么合并"；列表默认是覆盖，加上 `operator.add` 后变成追加，
所以历史记录、消息、日志这类"过程型"字段才不会在下一个节点写入时被整块冲掉。

**Q2：`version` 字段是干什么的？为什么不直接用 `retry_count`？**
A：version 是"状态写入序号"，每个节点 +1，用作事件表的幂等唯一键和前端增量判断；
retry_count 只表示"第几次翻译"，语义不同，混用会让不同阶段的事件撞键、时间线丢步。

**Q3：你的 state 会不会越跑越大？**
A：会——`history` 和 `translations` 都随轮次增长，目前靠 `MAX_RETRY=3` 封顶；
如果做长任务，我会给 history 加自定义 reducer 限制条数，或者把过程记录只写 `task_events` 表、state 里只留摘要。

---

**一句话总结**：覆盖型字段回答"现在是什么"，累积型 channel 回答"发生过什么"——两者混用，时间线和重放都会坏掉。