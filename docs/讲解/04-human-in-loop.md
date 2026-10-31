# 04 · Human-in-the-loop：interrupt 暂停与 resume 恢复的完整链路

"人工审核"听起来像产品功能，实际是一道**分布式状态题**：图在某个节点停住、进程里没有线程在等，
几小时后用户点"通过"，任务必须从原地继续，而不是从头重跑。
LangGraph 给的答案是 `interrupt()` + `Command(resume=...)`，本篇从前端按钮一路追到图恢复。

对应代码：`workflow.py` 的 `human_review_node`（502-544 行）、`core/runner.py` 的 `resume_task`（271-302 行）、
`main.py` 的 `POST /api/tasks/{id}/resume`（387-407 行）、前端 `App.vue` 的 `submitReview`（315-339 行）。

## 1. 这段代码在做什么

### 1.1 暂停发生在一个节点内部（节选，省略 target_lang 等展示字段）

```python
def human_review_node(state: TranslationState) -> dict:
    version = _version_bump(state)
    payload = {                                  # 组装"给人看"的载荷，纯计算
        "type": "human_review",
        "task_id": state.get("task_id"),
        "source_text": state.get("source_text", ""),
        "translation": state.get("translation", ""),
        "review": state.get("review") or {},
        "glossary": state.get("glossary") or [],
        "question": "请确认译文是否可用；驳回后会在剩余重试预算内重新翻译。",
    }
    decision = interrupt(payload)                # ★ 第一次执行到这里抛 GraphInterrupt
    if not isinstance(decision, dict):
        decision = {"approved": bool(decision)}  # 容错：脚本直接传布尔值也能用

    approved = bool(decision.get("approved"))
    edited = str(decision.get("edited_translation") or "").strip()
    final_translation = edited or state.get("translation", "")
    return {
        "human_decision": {"approved": approved, "edited": bool(edited), "comment": comment, "at": _now_iso()},
        "translation": final_translation,
        "stage": "human_resolved",
        "status": "running",
        "version": version,
        "history": [_history("human_review", note)],
    }
```

关键机制：`interrupt()` 第一次执行时**不会返回**，而是让图抛出中断，把"停在这里、等一个值"写进 checkpoint。
恢复时 LangGraph 会**从这个节点的第一行重新执行**，`interrupt()` 这次直接返回 `Command(resume=...)` 带来的值。
所以 `interrupt()` 之前只允许纯计算——这里组装 payload 是安全的；如果之前写了数据库，恢复一次就会多写一次。

### 1.2 暂停时 runner 做了什么（`core/runner.py` 166-192 行）

```python
for chunk in graph.stream(graph_input, graph_config, stream_mode="updates"):
    elapsed = (datetime.now() - started_at).total_seconds()
    if elapsed > config.TASK_TIMEOUT_SECONDS:                 # 600s 软超时
        raise TimeoutError(...)
    if "__interrupt__" in chunk:                              # ← 暂停信号
        interrupted = True
        break                                                 # 跳出流，不当作失败
    snapshot = graph.get_state(graph_config)
    _persist(task_id, snapshot.values or {}, message=_last_note(snapshot.values or {}))

if interrupted:
    snapshot = graph.get_state(graph_config)
    values = snapshot.values or {}
    _persist(task_id, values, status="waiting_human",
             message="等待人工审核（或在 /resume 前可编辑术语表）")
    database.update_task_runtime(task_id, status="waiting_human")   # 双保险
    return
```

于是暂停时：

- `tasks` 表：`status=waiting_human`、`stage=human_review`、`progress=90`；
- `task_events` 表：追加一条 `(human_review, waiting_human)` 事件，前端时间线可见；
- `checkpoints.db`：这个 thread 的最新快照里 `next == ("human_review",)`，等一个 resume 值；
- 进程：worker 线程已经释放，**没有线程在空转等待**。

任务 #3 的真实数据：17:56:35 进入 `waiting_human`，18:02:20 才被 resume——
中间 **5 分 45 秒**，进程里没有任何线程为这个任务待命，4 个 worker 可以继续跑别的任务。
这就是 `interrupt` 相对"节点里 sleep 轮询"最直接的价值。

### 1.3 用户点"通过"的完整调用链

```
App.vue submitReview('approve')
  → api.resumeTask(id, {approved, edited_translation?, comment?})       (client.js 98-103)
  → POST /api/tasks/{id}/resume   {approved: true, ...}
  → main.api_resume_task
        404 任务不存在 / 409 正在执行 / 409 状态不是 waiting_human
        decision = {"approved": true, ...}                             (400-404)
        _submit(resume_task, task_id, decision)                        (406)
  → 线程池 worker → core.runner.resume_task
        _acquire(task_id)                        防重复 resume（进程内去重）
        snapshot = graph.get_state(config)
        if not snapshot.next: return             已经不暂停了 → 幂等忽略
        update_task_runtime(status="running", stage="human_review", progress=90)
        publish(... "已收到人工审核结果，继续执行")
        _stream(graph, Command(resume=decision), config, task_id)      (295)
  → human_review_node 重放：interrupt() 返回 decision
  → 节点写 human_decision / translation / stage=human_resolved
  → supervisor 走 decide_after_human：通过 → finalize；驳回且有预算 → translate
```

前端拿到的实时反馈是 SSE 推的（`_persist` 每次都会 `publish`），同时 `submitReview` 会 `reloadDetail` 补拉一次完整详情，
因为 SSE 只推增量字段，评分/术语/历史这些要读 DB。

### 1.4 两种"待审核"状态的落库方式

| 层 | 字段/键 | 作用 |
|---|---|---|
| `tasks` | `status=waiting_human`, `stage=human_review`, `progress=90` | 列表/详情直接读，刷新页面也能看到 |
| `task_events` | `(task_id, "human_review", version)` 一条事件 | 时间线展示"等待人工审核" |
| `checkpoints.db` | thread 快照里 `next=("human_review",)` | 真正的"续跑凭证" |
| 内存 | `_active_tasks` 集合 | 防止 resume/retry/恢复扫描同时跑同一任务 |

`main.py._task_detail()` 还有一层兜底（212-218 行）：如果 DB 的 status 还是 `running`，
但 checkpoint 的 `snapshot.next` 里含 `human_review`，就临时把详情显示成 `waiting_human`。
这解决的是"图已经暂停、DB 还没来得及写"的极短窗口。

### 1.5 审核期间还能改术语表

`POST /api/tasks/{id}/glossary` 在任务不活跃时允许整表替换：写 `glossary` 表，
再 `graph.update_state()` 把新术语表和 `version+1` 写回 checkpoint（`main.py` 442-461 行）。
这样如果用户驳回重译，`translate` 节点读到的是人改过的术语表。
执行中的任务会被 409 拒绝——因为 runner 每个 superstep 都会用 state 里的术语表覆盖 DB，
这时写库会被下一拍冲掉，反而制造"改了不生效"的错觉。

## 2. 为什么这样写（对比错写法）

### 2.1 为什么不用"轮询数据库等人工操作"

错写法（很多人第一反应）：

```python
# ❌ 节点里 while 循环等数据库被改成 approved
while True:
    row = database.get_task(task_id)
    if row["status"] == "approved":
        break
    time.sleep(5)          # 占住一个 worker 线程，最长等到超时
```

三个致命问题：

1. **占线程**：worker 池只有 `MAX_WORKERS=4`，4 个人工审核任务就能把整个系统挂死；
2. **重启即丢**：进程重启后这个 `while` 没了，任务永远停在 running；
3. **不知道"从哪继续"**：DB 里只有一个状态字符串，没有"下一步该执行哪个节点"的信息，
   你得自己写一套"状态 → 恢复动作"的映射，等于重造 checkpointer。

`interrupt()` 把"暂停点"变成 checkpoint 里的 `next`，恢复就是"喂一个值让它继续"，
线程在暂停期间完全释放。这就是它比轮询高级的地方。

### 2.2 对比：把人工审核做成独立接口、节点直接结束

```python
# ❌ 另一种常见写法：human_review 节点直接 return，任务标记 waiting，
#    另写一个接口把状态改成 done 并直接写最终译文
def human_review_node(state):
    database.update_task_status(state["task_id"], "waiting_human")
    return {}          # 图到此结束
```

问题：人工审核后的"驳回重译"没法接回图里——你只能在接口里手动再 `invoke` 一次，
重试预算、审校数据、术语表这些 state 全丢了；而且 DB 状态和图的真实位置会脱节。
用 `interrupt` 的话，驳回就是 `Command(resume={"approved": False})`，
supervisor 自然走"还有预算 → translate"，一行分支都不用重写。

### 2.3 为什么 resume 要做幂等和去重

用户可能双击"通过"，或者前端重试导致同一个 resume 发两次：

- `_acquire(task_id)` 用进程内集合挡住并发（第二个请求打日志后返回）；
- `snapshot.next` 为空说明已经恢复过/已结束，直接 return，不报错；
- 接口层还校验 `status == waiting_human`，状态不对返回 409。

这三层加起来，重复 resume 的最坏结果是"多打一条日志"，不会把任务跑两遍。

### 2.4 诚实的取舍

- **resume 依赖进程内单例图**：`get_workflow()` 是进程级缓存，`_active_tasks` 也是进程内集合。
  单机没问题；多副本部署必须把请求路由到持有该 thread 的实例，或者换 Postgres checkpointer + 共享任务队列。
- **暂停点没有超时**：`waiting_human` 可以无限等（这是产品选择）。如果要"24 小时不处理自动通过/失败"，
  需要加一个定时扫描，把超时任务按策略 resume。
- **`interrupt` 之前的代码会重放**：所以那段只能是纯计算。这个约束很容易被后来的人破坏，
  代码注释和本系列文档都特意标了。
- **人工修改译文不重新审校**：用户改了 `edited_translation` 就直接作为最终译文，
  分数还是上一轮的。这是信任人工优先；如果要求"人工改完再自动审一次"，得再加一个分支。

## 3. 面试官可能怎么问

**Q1：`interrupt()` 和 `Command(resume=...)` 的原理是什么？**
A：interrupt 让当前节点抛中断、把暂停点和待恢复的 channel 写进 checkpoint，恢复时节点从头重放、
`interrupt()` 直接返回 resume 带的值；所以暂停期间不占线程、进程重启也能恢复。

**Q2：待审核状态在数据库里怎么表示？**
A：`tasks` 写 `status=waiting_human/stage=human_review/progress=90`，`task_events` 追加一条事件，
真正的续跑凭证在 checkpoint 快照的 `next` 里；详情接口还会用 `snapshot.next` 做兜底显示。

**Q3：为什么不用轮询数据库等人工操作？**
A：轮询会占死 worker 线程、进程重启就丢、而且没有"下一步执行哪个节点"的信息；
interrupt 把暂停点存进 checkpoint，恢复只需一个 resume 值，天然支持断点与幂等。

---

**一句话总结**：人工审核不是"卡住等"，而是"把暂停点存盘、把线程还回去、等人给一个值再继续"。