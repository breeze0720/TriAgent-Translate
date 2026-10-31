# 03 · Checkpointer：进程挂了，任务为什么还在

这是项目二最硬的验收点：**翻译跑到一半 kill 掉进程，重启后任务能接着跑**。
实现靠两样东西：LangGraph 的 checkpointer（检查点存储器）和 `thread_id = task_id` 的约定。
对应代码：`core/checkpointer.py`、`workflow.py` 的 `build_workflow()`、`core/runner.py` 的恢复逻辑。

参考实现：`_refs/deer-flow-main/backend/.../runtime/checkpointer/provider.py`（deer-flow 的进程级单例）。

## 1. 这段代码在做什么

### 1.1 进程级单例（`core/checkpointer.py` 24-45 行）

```python
_saver: SqliteSaver | None = None
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

def get_checkpointer() -> SqliteSaver:
    global _saver, _conn
    if _saver is not None:                 # 快路径：已初始化，直接返回
        return _saver
    with _lock:                            # 慢路径加锁，防止两个线程同时初始化
        if _saver is None:                 # 双重检查
            path = Path(config.CHECKPOINT_DB)          # 绝对路径，避免 cwd 变化接错库
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False 的前提：SqliteSaver 内部自己用锁串行化读写
            _conn = sqlite3.connect(str(path), check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")     # 读写不互斥
            _conn.execute("PRAGMA synchronous=NORMAL")   # 性能与安全的折中
            _conn.execute("PRAGMA busy_timeout=30000")   # 锁等待 30s 再报错
            _saver = SqliteSaver(_conn)
    return _saver
```

三个点必须能讲出来：

1. **为什么单例**：图在每次执行前 `compile(checkpointer=...)` 时要拿到同一个 saver；
   每个请求各开一个 sqlite 连接反而更容易 `database is locked`。整个进程共用一个连接 + saver 最稳。
2. **为什么 `check_same_thread=False` 安全**：`SqliteSaver` 内部自带 `threading.Lock`，
   checkpoint 的读写被串行化；这个参数只是解除 Python sqlite3 模块的线程归属检查。
3. **为什么 WAL**：WAL（Write-Ahead Logging，预写日志）让读和写不互相阻塞，
   详情接口/SSE 可能在读快照，同时后台线程在写下一拍。

图编译时把它接上（`workflow.py` 597-605、634 行）：

```python
def build_workflow(checkpointer: Any | None = None):
    if checkpointer is None:
        from core.checkpointer import get_checkpointer
        checkpointer = get_checkpointer()          # 生产路径：进程级单例
    ...
    compiled = builder.compile(checkpointer=checkpointer)
    return compiled
```

参数显式传入是为了测试：`tests/test_checkpointer_recovery.py` 传 `tmp_path` 下的临时库，
`test_retry_no_infinite_loop.py` 传 `MemorySaver()`，都不碰生产库。

### 1.2 thread_id 就是 task_id

每次执行图都带同一个 config（`core/runner.py` 225、255、278 行）：

```python
graph_config = {"configurable": {"thread_id": str(task_id)}}
```

LangGraph 的 checkpointer 按 `thread_id` 分区存快照：

- 任务 #3 的所有 superstep 都存在 `thread_id="3"` 这一条线里，和任务 #4 天然隔离；
- 续跑、`get_state`、人工恢复都只要这一个 id，不需要额外的映射表；
- 删除任务时 `delete_thread(str(task_id))` 顺手清快照（`main.py` 613-615 行），避免 `checkpoints.db` 无限增长。

### 1.3 挂掉之后怎么判断"能不能续跑"（`core/runner.py` 223-243 行）

```python
if mode == "resume":
    graph = get_workflow()
    graph_config = {"configurable": {"thread_id": str(task_id)}}
    snapshot = graph.get_state(graph_config)
    values = snapshot.values or {}
    if not values:                                    # 判据 1：没有快照
        _fail(task_id, "服务重启后未找到 checkpoint 快照，无法续跑；请点击重试")
        return
    if not snapshot.next:                             # 判据 2：图已跑完，只差落库
        status = str(values.get("status") or "failed")
        _persist(task_id, values, status=status if status in ("done", "failed") else "failed",
                 message="恢复：图已执行完毕，补写最终状态")
        return
    _stream(graph, None, graph_config, task_id)       # 判据 3：有 next → 从断点继续
```

| `snapshot.values` | `snapshot.next` | 含义 | 动作 |
|---|---|---|---|
| 空 `{}` | 空 | 这个 thread 从没跑过 / 已被删 | 标记失败，提示点"重试" |
| 非空 | 非空（如 `("translate",)`） | 停在某个节点前，可以续 | `graph.stream(None)` 从断点跑 |
| 非空 | 空 | 图已到 END，只是 DB 没写 | 按最终 state 补写一次，不重跑 |

`graph.stream(None, config)` 里第一个参数传 `None` 就是"从最近 checkpoint 继续"；
传 dict 是全新执行；传 `Command(resume=...)` 是人工恢复（第 04 篇）。

### 1.4 启动时的恢复扫描（`core/runner.py` 309-340 行）

```python
def recover_interrupted_tasks(executor: Executor) -> list[int]:
    tasks = database.list_tasks_by_status(["running", "pending"])
    for task in tasks:
        task_id = int(task["id"])
        mode = "resume" if task.get("status") == "running" else "start"
        database.append_event(task_id, stage="recovery", status="running",
                              message=f"服务重启：检测到 {task.get('status')} 任务，自动…",
                              version=int(task.get("version") or 0))
        executor.submit(execute_task, task_id, mode)
    return recovered
```

`main.py` 的 lifespan 在启动时调用它（272-273 行）。规则：

- `running`：走 resume，读 checkpoint 的 `next` 继续；
- `pending`：入参都在 DB 里，从头跑（幂等，重复执行无副作用）；
- `waiting_human`：**不自动恢复**——它在等人类，不能替用户点"通过"。

### 1.5 崩溃恢复测试锁死了什么

`tests/test_checkpointer_recovery.py::test_resume_from_checkpoint_after_crash` 做了四件事：

1. 用临时 sqlite 跑图，第一次 `translate_segment` 抛异常（模拟进程被 kill）；
2. 断言 `graph.get_state()` 里 `stage == "translate"` 且 `snapshot.next` 含 `translate`；
3. **换一条新 sqlite 连接、重新 compile 图**（模拟进程重启）；
4. `graph.invoke(None, config)` 续跑，断言最终 `done`、`retry_count == 1`
   （崩溃那次翻译没提交，续跑从第 1 次重算）、崩溃前的 `history` 仍在。

## 2. 为什么这样写（对比错写法）

### 2.1 没有 checkpointer 会怎样

```python
# ❌ 无 checkpointer：状态全在内存，进程一挂就没了
graph = build_workflow().compile()
result = graph.invoke(initial_state)     # 卡在某个 LLM 调用时被 kill
# → DB 里 status 还是 running，没有任何断点信息，任务永久卡住
```

这是 v1 的真实状况：任务表只会显示 `translating/reviewing`，既不失败也不能续，只能人工重跑。
现在每个 superstep（节点执行完）都会落一条快照，最坏情况只丢"当前这一次节点调用"。

### 2.2 每个请求 new 一个 saver 会怎样

```python
# ❌ 每请求一个连接/saver：并发一高就 "database is locked"
saver = SqliteSaver(sqlite3.connect("checkpoints.db", check_same_thread=False))
graph = workflow.build_workflow(checkpointer=saver)
```

多个连接同时写同一个 sqlite 文件会互相阻塞；即使开了 WAL，多个 writer 也要串行。
进程级单例把这层竞争消掉了——反正图执行由 4 个 worker 线程共享一个 saver，内部锁负责排队。

### 2.3 对照 deer-flow 的单例写法

deer-flow 的 `provider.py` 用的是同一个形状：

```python
_checkpointer = None
_checkpointer_ctx = None                       # 持有连接上下文的"开着的" context manager
_checkpointer_lock = threading.Lock()

def get_checkpointer() -> Checkpointer:
    global _checkpointer, _checkpointer_ctx
    if _checkpointer is not None:              # 快路径无锁
        return _checkpointer
    config = _get_checkpointer_config()        # 锁外解析配置，避免跨 provider 锁序反转
    with _checkpointer_lock:
        if _checkpointer is not None:          # 双重检查
            return _checkpointer
        ctx = _sync_checkpointer_cm(config)    # memory / sqlite / postgres 三种后端
        _checkpointer = ctx.__enter__()
        _checkpointer_ctx = ctx                # 保存 ctx，进程退出时统一 __exit__
    return _checkpointer
```

差异与借鉴点：

| | deer-flow | 本项目 |
|---|---|---|
| 单例形状 | 模块级变量 + `threading.Lock` + 双重检查 | 相同 |
| 资源生命周期 | `checkpointer_context()` 保存 ctx，另有 `reset_checkpointer()` | 保存 `_conn`，`close_checkpointer()` |
| 后端 | memory / sqlite / postgres 可配 | 只 sqlite（教学项目，单机） |
| 连接方式 | `SqliteSaver.from_conn_string()` 上下文 | 手动 `sqlite3.connect` + PRAGMA |
| 用途 | 生产级，多进程/多后端 | 本地演示 + 断点验收 |

我们**故意**只做 sqlite：部署成本为零、`checkpoints.db` 可直接删库重来。
真上生产会换成 `PostgresSaver`（`langgraph-checkpoint-postgres`），接口不变、`thread_id` 语义不变。

### 2.4 诚实的取舍

- **SQLite 只适合单机**：多副本部署时每个实例一个本地文件，恢复会读不到别的实例写的快照；
  生产要换 Postgres，或者至少把 `checkpoints.db` 放到共享盘（不推荐）。
- **WAL 文件会涨**：`checkpoints.db-wal` 可能比主库还大（本项目实测 3MB 级），
  靠 sqlite 自动 checkpoint 回收；删除任务时只删 thread 记录，文件不会立刻变小。
- **只保证"节点级"恢复**：如果进程在某个节点的 LLM 调用中途挂掉，这次调用白花钱，节点会整体重跑。
  要做到"调用级"幂等，得给每次 LLM 调用加 cache，成本更高。
- 恢复扫描是**启发式**的：`running` 状态可能来自"另一个进程还在跑"，
  单机项目没问题；多实例需要引入租约/心跳（deer-flow 的 `run_ownership` 就是干这个的）。

## 3. 面试官可能怎么问

**Q1：断点续跑具体是怎么实现的？**
A：编译图时挂 `SqliteSaver`，每个 superstep 落一条快照；`thread_id` 用 `task_id`；
重启后 `get_state()` 读快照，`next` 非空就 `graph.stream(None)` 从断点继续，`next` 为空就补写终态。

**Q2：为什么 `thread_id` 用 `task_id`，不另建一张会话表？**
A：任务 id 本身就是天然的唯一隔离键，省一张映射表；删除任务、查详情、恢复都能直接用 DB 主键定位，
不会出现"会话和任务对不上"的一致性问题。

**Q3：为什么 checkpointer 要做成单例？线程安全怎么保证？**
A：SqliteSaver 内部有锁，整个进程共用一个连接可以避免多连接争抢 `database is locked`；
初始化用双重检查锁，业务读写交给 saver 内部串行化，我们只保证"只创建一次"。

---

**一句话总结**：checkpointer 把"图跑到哪了"变成磁盘上的一条快照，`thread_id=task_id` 让每个任务各有一条线，重启只是"再读一次线头"。