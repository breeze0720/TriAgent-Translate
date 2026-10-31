# 07 · 三个真实 bug 的前后对照：从"能跑"到"敢上线"

这一篇是复盘。三个 bug 都来自 v1 的真实代码（旧文件保留在
`_archive/old-annotate_backup/02-多智能体翻译-LangGraph__workflow.py` 与 `...__database.py`），
最终代码在注释里标了修复点，并且都有回归测试兜底。

每个 bug 固定三段：**原代码片段 → 问题是什么 → 修复后代码片段**。

## 1. 实现概览

### bug ① 重试计数只在"已有反馈"分支自增 → 无限循环

**原代码**（旧 `workflow.py` 51-79 行，节选）：

```python
def translator_node(state):
    if state["retry_count"] > 0:
        # 携带审校反馈重新翻译
        result = translate(..., review_feedback=state["review_feedback"],
                           previous_translation=state["translation"])
        new_retry = increment_retry(state["task_id"])   # ← 只在这个分支自增
        state["retry_count"] = new_retry
    else:
        result = translate(source_text=..., source_lang=..., target_lang=...)  # 首译：不自增
    state["translation"] = result
    return state
```

**问题**：首译时 `retry_count=0`，走 `else` 分支，**不自增**；审校低分后 `should_continue` 用 `retry < 3` 判断，此时 `retry` 还是 0 → 永远成立 → 又回到 translator；第二次进入时 `retry_count` 仍是 0，还是走 `else`（不带审校意见重译），还是不自增……于是形成**真死循环**。实际表现有两种：

- LangGraph 默认 `recursion_limit=25`，跑满后抛 `GraphRecursionError`，任务卡在 `running`；
- 如果调高了递归上限，就是无限烧钱。

更隐蔽的后果：即使循环能停下来，**"重试"也从来没有带上审校意见**（因为一直走首译分支），质量不会变好。

**修复后**（`workflow.py` 357-412 行，节选）：

```python
def translate_node(state: TranslationState) -> dict:
    retry_count = int(state.get("retry_count") or 0) + 1  # ★ 每次进入就 +1，无条件
    ...
    review = state.get("review") or {}
    issues = (review.get("issues") or []) if retry_count > 1 else []   # 首译不带意见
    previous = {int(item.get("index", -1)): str(item.get("text") or "")
                for item in (state.get("translations") or []) if isinstance(item, dict)}
    ...
    return {"translations": translated, "translation": translation,
            "retry_count": retry_count, "stage": "translated", "version": version,
            "history": [_history("translate", note)]}
```

修复的关键不是"多加一行 +1"，而是**把计数的语义改成"第几次进入翻译节点"**：计数和分支解耦，就不会再有"某个分支忘了加"的问题。`config.MAX_RETRY=3` 的含义也随之明确：最多翻译 3 次（首译 + 2 次重译）。

**回归测试**（`tests/test_retry_no_infinite_loop.py`）：

```python
def test_retry_stops_after_max_retries_and_fails(monkeypatch):
    calls = _patch_agents(monkeypatch, _low_review)      # 审校恒 60 分
    result = graph.invoke(_initial_state(), config=graph_config)
    assert calls["translate"] == config.MAX_RETRY        # 恰好 3 次，不多不少
    assert result["retry_count"] == config.MAX_RETRY
    assert result["status"] == "failed"
    assert result["stage"] == "final"
```

测试里还加了一道"保险丝"：假翻译函数被调用超过 20 次就直接 `AssertionError`，这样即使将来又写出死循环，测试是"失败"而不是"挂住"。

### bug ② 条件边里写数据库（副作用 + 不可重放）

**原代码**（旧 `workflow.py` 142-165 行）：

```python
def should_continue(state) -> Literal["polisher", "translator", "end"]:
    score = state["review_score"]
    retry = state["retry_count"]
    if score >= 80:
        return "polisher"
    elif retry < 3:
        return "translator"
    else:
        state["status"] = "failed"                       # ← 改 state（不生效）
        mark_failed(state["task_id"], f"审校评分 {score} 未达标…")  # ← 条件边里写库
        return "end"
```

**问题**：LangGraph 的条件边（conditional edge）是**路由函数**，它可能被多次求值（分支解析、重放、调试），而且它的返回值只用来决定下一跳，**不写 state**。在里写数据库会带来三个后果：

1. **重复写**：同一次决策被求值两次，就多一条失败记录/多一次 `increment_retry`；
2. **不可重放**：断点恢复时图会重新计算路由，又写一遍库，数据可能被改坏；
3. **不可测**：想单测"低分耗尽预算"这个决策，必须连数据库。

另外 `state["status"] = "failed"` 在条件边里是**无效的**——LangGraph 不把条件边的原地修改当更新，所以旧代码"标记失败"实际上是靠 `mark_failed` 写库，而库里的失败原因（见 bug ③）还丢了。最终表现为：任务状态是 failed，但没有任何原因，且状态随时可能被下一次求值覆盖。

**修复后**：决策拆成两层。条件边只做映射：

```python
def route_after_supervisor(state: TranslationState) -> str:
    """条件边只做映射：supervisor 已经把决策写进 stage，这里翻译成节点名/END。"""
    return _ROUTES.get(str(state.get("stage") or "init"), "end")

def decide_after_review(state) -> Literal["translate", "polish", "human_review", "finalize"]:
    """纯函数：不写库、不改 state，只返回下一跳。"""
    ...   # 只读 overall / retry_count / max_retries / enable_human_review
```

副作用全部收口到 `core/runner.py` 的执行边界：

```python
def _persist(task_id, values, *, status=None, message=""):
    database.save_task_snapshot(...)     # 整份快照，幂等
    database.replace_glossary(...)
    database.append_event(..., version)  # (task_id, stage, version) 唯一，幂等
    publish(task_id, event)              # SSE
```

**回归测试**（`tests/test_state_machine.py`）：`test_route_after_supervisor_is_pure_mapping()` 直接对纯函数断言，
包括"未知阶段回退到 end"这种兜底路径——不连 DB、不调 LLM，毫秒级跑完。

### bug ③ `mark_failed` 丢 `error` 参数

**原代码**（旧 `database.py` 107-116 行）：

```python
def mark_failed(task_id: int, error: str):
    """标记任务失败"""
    sql = "UPDATE translation_tasks SET status = 'failed' WHERE id = %s"   # ← error 没用上
    cursor.execute(sql, (task_id,))
```

**问题**：函数签名收了 `error`，SQL 里却没写。调用方传的"审校评分 60 未达标，已重试 3 次"这类信息**整条丢失**：

- 前端只能显示"任务失败"，用户不知道是模型报错、超时还是分数不够；
- 排障时只能翻控制台日志，而日志随进程重启就没了；
- 无法判断"该不该重试"——有些失败重跑一次就好，有些重跑也没用。

**修复后**（`database.py` 297-306 行）：

```python
def mark_failed(task_id: int, error: str) -> bool:
    """标记失败。注意 error 参数必须落库（旧版 SQL 只 SET status='failed'，
    失败原因整条丢失，前端既不能展示也没法判断该不该重试）。"""
    rowcount, _ = _execute(
        "UPDATE tasks SET status = 'failed', error_message = %s, progress = 100, "
        "updated_at = NOW() WHERE id = %s",
        (str(error)[:1000], task_id),
    )
    return rowcount > 0
```

配套地在 runner 的异常兜底里统一传原因（`core/runner.py` 134-146 行）：

```python
def _fail(task_id: int, error: str) -> None:
    database.mark_failed(task_id, error)
    publish(task_id, {"task_id": task_id, "stage": "failed", "status": "failed",
                      "progress": 100, "message": f"任务失败：{error}"})
```

于是失败原因同时出现在三个地方：`tasks.error_message`（详情接口 `error` 字段）、SSE 事件（前端 toast）、日志。`[:1000]` 是因为列定义是 `VARCHAR(1000)`，截断而不是让 insert 报错。

### 三个 bug 的共同点

① 计数与分支耦合、② 条件边写库 + 原地改 state、③ SQL 漏字段——共同点是**都是"副作用位置不对"**：
副作用要么跟控制流耦合、要么漏写、要么写在不该写的层。修法也就统一成一句话：
把副作用从控制流里拿出来，放到一个幂等、可观测的边界上。

## 2. 设计取舍与备选方案

### 2.1 为什么"计数"要绑定进入次数，而不是绑定分支

错写法把"是否需要反馈"和"第几次尝试"混在一个 `if` 里：

```python
if state["retry_count"] > 0:      # 既判断要不要反馈，又顺便计数
    ...
    state["retry_count"] += 1
```

两个语义一旦绑定，任何分支调整都会破坏计数。正确做法是"进入即计数"，再用 `retry_count > 1` 推导"要不要带反馈"——**用计数派生分支，而不是用分支决定计数**。

### 2.2 为什么条件边必须纯函数

LangGraph 的执行模型里，节点返回值才是状态更新；条件边只负责"下一个节点是谁"。
官方文档也明确建议条件边不要有副作用。本项目的边界更严格：

- 节点：可以写 state，但**不碰数据库**；
- 条件边：只读 state、只返回字符串；
- runner：唯一的落库/推送点。

这条边界带来两个直接收益：节点可重放（checkpoint 恢复不会重复写库）、状态机可纯单测（`test_state_machine.py` 不连 DB/不调 LLM 就能跑）。

### 2.3 为什么失败原因必须结构化落库

日志是"给人看的、会滚动的"，数据库是"给系统看的、可查询的"。`status='failed'` 是机器状态，`error_message` 才是人需要的信息。本项目还额外把失败原因塞进 SSE 事件和 `history` 的 note，保证"无论从哪个入口看，都知道为什么失败"。

### 2.4 三处"顺手发现"的小问题（不算 bug，但值得知道）

1. `core/runner.py` 定义了 `TaskBusyError`，但没有任何地方 `raise` 它——`_acquire()` 失败时只是打印日志跳过。属于"预留但未使用"的异常类型，可以在后续清理。
2. `decide_after_review()` 里"审校不可用最多再试一次"用了硬编码 `retry < 2`，而不是配置项；换 `MAX_RETRY` 时这个上限不会跟着变。
3. `README.md` 还停留在 v1 描述（三个 Agent、8000 端口、`translation_tasks/review_records` 两张表），与最终代码（supervisor + 6 节点、8001、三张新表）不一致——文档债，不是逻辑 bug。

## 3. 常见问题（FAQ）

**Q1：你遇到过最难的 bug 是什么？**
A：死循环——重试计数只在"已有反馈"分支自增，首轮永远不自增，导致"低分就重译"永远成立；定位靠打印每次进入翻译的次数，修复是把计数改成"每进入必 +1"，并补了一条"调用超过 20 次就失败"的回归测试。

**Q2：为什么条件边里不能写数据库？**
A：条件边可能被多次求值且不产生状态更新，写库就会重复写、破坏重放；我们的做法是条件边只返回节点名，所有落库收口到 runner，用幂等 SQL 兜底。

**Q3：任务失败后用户怎么知道原因？你怎么保证可观测？**
A：失败原因写 `tasks.error_message`，同时进 SSE 事件和 history；runner 的 `_fail()` 是唯一失败入口，保证任何异常都带着 `类型: 消息` 落库，前端能直接展示并决定是否重试。

---

**一句话总结**：三个 bug 的修法都是同一件事——把副作用从控制流里拿出来，放到一个幂等、可观测的边界上。