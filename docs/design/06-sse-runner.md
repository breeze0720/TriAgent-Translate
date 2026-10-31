# 06 · SSE 与后台执行：线程池、事件队列、推送与重连

一个翻译任务要调 5~8 次大模型、跑一两分钟。HTTP 请求不能等它跑完，
所以系统拆成三段：**接口立即返回 → 线程池后台执行 → SSE（Server-Sent Events，服务器发送事件）把进度推给浏览器**。
本篇讲这三段怎么接起来，以及其中最容易被写错的地方：跨线程操作 asyncio 队列。

对应代码：`main.py` 的 `_get_executor`（75-85 行）与 SSE 接口（354-384 行）、
`core/events.py`、`core/runner.py` 的 `_stream`、前端 `useTaskStream.js`。

## 1. 实现概览

### 1.1 整体数据流

```
POST /api/tasks
   │  database.create_task() → pending，立刻返回 {task_id}
   ▼
ThreadPoolExecutor(max_workers=4)          ← 后台线程，跑同步 LangGraph 图
   │  execute_task → graph.stream(...)
   ▼
每执行完一个节点 → core.runner._persist()
   │  ├─ 写 MySQL（tasks / task_events / glossary）
   │  └─ core.events.publish(task_id, event)
   ▼
loop.call_soon_threadsafe(queue.put_nowait, ...)    ← 把事件投递回主事件循环
   ▼
asyncio.Queue（每任务一个，maxsize=200）
   ▼
GET /api/tasks/{id}/events  → EventSourceResponse(event="progress")
   ▼
浏览器 EventSource → Vue 更新进度条/时间线
```

### 1.2 后台执行池（`main.py` 75-85 行）

```python
def _get_executor() -> ThreadPoolExecutor:
    """后台执行池：限制并发，避免 100 个请求同时打 LLM。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS,
                                       thread_name_prefix="translation")
    return _executor

def _submit(fn, *args) -> None:
    _get_executor().submit(fn, *args)
```

`MAX_WORKERS=4`：同时最多 4 个任务在跑。为什么用线程而不是 asyncio 任务？

- LangGraph 图执行是**同步 API**（`graph.stream`、`graph.invoke`），
  内部还有 sqlite checkpoint 的阻塞 IO；
- `pymysql` 也是同步阻塞的，没有 async 驱动；
- 把这些阻塞调用丢进线程池，事件循环就不会被卡住，SSE 和详情接口照常响应。

线程池还天然给了"背压"：第 5 个任务在队列里等着，不会瞬间打出 5 路 LLM 并发。

### 1.3 进程内事件总线（`core/events.py` 17-51 行）

```python
_loop: asyncio.AbstractEventLoop | None = None
_subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)
_lock = threading.Lock()

def bind_loop(loop):            # FastAPI lifespan 启动时绑定主事件循环
    global _loop
    _loop = loop

def publish(task_id: int, event: dict) -> None:
    """从任意线程发布事件（线程安全）。"""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    with _lock:
        queues = list(_subscribers.get(task_id, []))
    if not queues:
        return                                       # 没人在听：直接丢，不落库也不报错
    try:
        for queue in queues:
            loop.call_soon_threadsafe(_put_nowait, queue, event)   # ★ 关键一行
    except RuntimeError:
        pass                                         # 进程正在关闭
```

`asyncio.Queue` **只能在事件循环线程里操作**。`publish()` 是在 worker 线程里被调用的，
所以必须用 `loop.call_soon_threadsafe()` 把 `put_nowait` 排队回主循环执行。
这是 FastAPI + 后台线程做 SSE 最容易踩的坑：直接 `queue.put_nowait()` 在调试时可能"看起来能用"，
高并发下会偶发丢事件甚至 RuntimeError。

`_put_nowait` 对 `QueueFull` 的处理是**丢事件**（`maxsize=200`）：SSE 是"最新状态优先"的语义，
不能因为一个卡住的浏览器连接把后台线程阻塞住。

### 1.4 订阅与 SSE 接口（`core/events.py` 54-69 行 + `main.py` 354-384 行）

```python
async def subscribe(task_id: int):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    with _lock:
        _subscribers[task_id].append(queue)
    try:
        while True:
            event = await queue.get()          # 挂起等事件，不占 CPU
            yield event
    finally:                                   # 生成器结束（客户端断开）自动退订
        ...
```

```python
@app.get("/api/tasks/{task_id}/events")
async def api_task_events(task_id: int):
    detail = _task_detail(task_id)             # 先读一次 DB 拼快照
    initial = {"task_id": task_id, "stage": detail["stage"], "status": detail["status"],
               "progress": detail["progress"], "message": "当前状态快照",
               "version": detail["version"]}
    async def event_generator():
        yield ServerSentEvent(data=json.dumps(initial, ensure_ascii=False, default=str),
                              event="progress")          # ★ 第一帧永远是快照
        if detail["status"] in ("done", "failed"):
            return                                       # 已终态：只给快照就关流
        async for event in subscribe(task_id):
            yield ServerSentEvent(data=json.dumps(event, ensure_ascii=False, default=str),
                                  event="progress")
            if event.get("status") in ("done", "failed"):
                return                                   # 终态：主动关流，前端不再重连
    return EventSourceResponse(event_generator())
```

三个设计点：

1. **具名事件 `event: progress`**：浏览器要用 `source.addEventListener('progress', ...)` 接，前端同时监听 `message` 兜底（`NAMED_EVENTS = ['progress', 'message']`）。
2. **第一帧是快照**：断线重连的第 2 次、第 3 次连接也能立刻对齐当前状态，不依赖历史事件。
3. **终态主动关流**：前端收到 `done/failed` 后 `markSettled()`，不触发重连。

### 1.5 前端重连与看门狗（`useTaskStream.js`）

```javascript
const LIVENESS_MS = 30000          // 30s 没有任何数据 → 视为半开连接
const WATCHDOG_TICK_MS = 5000      // 每 5s 检查一次

function scheduleReconnect() {
    attempts += 1
    const delay = Math.min(1000 * attempts, 8000)   // 1s→2s→…，封顶 8s
    timer = setTimeout(() => connect(), delay)
}
```

- **指数退避**：1s 起步、每次 +1s、最多 8s，避免服务端还没恢复就疯狂重连；
- **看门狗**：`state === 'open'` 且 30s 没收到**数据事件**时主动断开重连——处理"TCP 还活着但对端已经不推了"的半开连接；
- **切换任务**：`open(id)` 先 `close()` 旧连接，避免串台。

一个真实细节：`sse-starlette` 默认会周期发 SSE **注释行**（`: ping`）做心跳，但浏览器的 `EventSource` 不会把注释当 message 事件，所以前端 `lastActivity` 不会被刷新——一次耗时超过 30 秒的 LLM 调用期间，看门狗会重连一次。这**无害**：重连后第一帧就是快照。若要接真实心跳，后端发一条 `event: ping`、前端在 `handleMessage` 里刷新 `lastActivity` 即可。

## 2. 设计取舍与备选方案

### 2.1 为什么用 SSE 而不是 WebSocket / 轮询

| 方案 | 方向 | 连接成本 | 断线重连 | 适用 |
|---|---|---|---|---|
| 轮询（polling） | 客户端拉 | 每次一个 HTTP 请求 | 天然 | 状态变化慢、容忍延迟 |
| SSE | 服务端→客户端单向 | 一条长连接 | 浏览器内置 | 进度推送、日志流 |
| WebSocket | 双向 | 一条长连接 + 协议升级 | 要自己写 | 聊天、协同编辑 |

本项目的推送是**纯单向**（服务端推进度，客户端只发 HTTP 命令），SSE 正好：

- 走普通 HTTP，不需要协议升级，公司代理/网关普遍放行；
- `EventSource` 自带重连与 `Last-Event-ID`（我们没用后者，用快照替代）；
- 不需要心跳包和自定义帧格式，调试时浏览器 Network 面板直接可读。

WebSocket 在这里是过度设计：双向通道用不上，还要自己实现重连、心跳、鉴权；轮询则太慢、太费：一次翻译 2 分钟，1 秒轮询就是 120 次请求打 MySQL。

### 2.2 对比错写法：在 async 接口里直接跑图

```python
# ❌ 在 async def 里调用同步阻塞的 graph.invoke：整个事件循环被卡住
@app.post("/api/tasks")
async def create_task(...):
    result = graph.invoke(state)        # 2 分钟内所有其他请求都没响应
    return result
```

正确做法就是本项目：`def`（FastAPI 自动丢线程池）或显式 `_submit()` 到自己的线程池，HTTP 请求立即返回 `task_id`。

### 2.3 对比错写法：worker 线程直接操作 asyncio.Queue

```python
# ❌ asyncio.Queue 不是线程安全的，跨线程 put 会偶发丢事件/抛异常
def publish(task_id, event):
    for q in _subscribers[task_id]:
        q.put_nowait(event)
```

以及裸线程版本：

```python
# ❌ 每个任务一个 threading.Thread：100 个任务 = 100 路 LLM 并发，直接打爆配额
threading.Thread(target=run_task, args=(task_id,)).start()
```

### 2.4 诚实的取舍

- **单进程事件总线**：多副本部署时，A 实例收到的事件推不到连在 B 实例上的浏览器；生产要换成 Redis pub/sub 或专门的流服务（deer-flow 用的是 `StreamBridge`）。
- **没有事件回放**：断线期间的事件不会补发，只能靠 `task_events` 表和重连快照；如果要做"从上次位置继续"，需要给事件加自增 id 并支持 `Last-Event-ID`。
- **HTTP/1.1 每域 6 连接上限**：同时订阅 6 个以上任务会排队；本项目一次只看一个任务，够用。
- **软超时只在节点之间检查**（`TASK_TIMEOUT_SECONDS=600`）：单个超长的 LLM 调用不会被中断，要精确超时得在 LLM 客户端层设 `timeout`（本项目已设 180s）。
- **终态事件可能丢失**：如果浏览器正好在"任务完成"那一刻断线，它会重连、拿到快照里的 `done` 后关闭，所以不会卡住——这是"快照先行"带来的鲁棒性。

## 3. 常见问题（FAQ）

**Q1：后台执行模型怎么设计的？为什么不直接在接口里跑？**
A：接口只负责入库和提交任务，执行交给固定大小的线程池；LangGraph 和 pymysql 都是同步阻塞的，放线程池既不卡事件循环，又能限制并发，HTTP 请求立即拿到 task_id。

**Q2：worker 线程怎么安全地把事件推给 asyncio 的 SSE 消费者？**
A：用 `loop.call_soon_threadsafe()` 把 `put_nowait` 投递回主事件循环，事件循环里由异步生成器 `await queue.get()` 消费；队列满了就丢事件，保证后台线程永不被慢客户端阻塞。

**Q3：为什么用 SSE 而不是 WebSocket？断线怎么处理？**
A：这是单向进度推送，SSE 走普通 HTTP、浏览器自带重连，比 WebSocket 简单得多；断线后按 1s→8s 退避重连，第一帧固定是 DB 快照，所以不需要补齐历史事件。

---

**一句话总结**：接口负责"接单"，线程池负责"干活"，事件总线负责"跨线程投递"，SSE 负责"实时通知"，快照负责"断线兜底"。