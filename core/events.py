"""
core/events.py - 进程内 SSE 事件总线

为什么用「每任务一个 asyncio.Queue 列表」而不是 Redis pub/sub：
单进程部署，最多几个并发任务；引入 Redis 只会多一个部署依赖。
关键点是线程安全：图执行在 ThreadPoolExecutor 的工作线程里，而 asyncio.Queue
只能在事件循环线程里操作，所以 publish() 用 loop.call_soon_threadsafe 把
put_nowait 投递回主循环。这是 FastAPI + 后台线程做 SSE 最容易踩的坑。

事件只做「实时通知」，持久化由 task_events 表负责：
SSE 断线重连后先发一条当前快照（来自 DB），再订阅后续增量。
"""
import asyncio
import threading
from collections import defaultdict

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """FastAPI lifespan 启动时绑定主事件循环。"""
    global _loop
    _loop = loop


def _put_nowait(queue: asyncio.Queue, event: dict) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # 消费端（浏览器）太慢时丢掉旧事件即可，SSE 是「最新状态优先」的语义，
        # 不能因为一个卡住的连接把后台线程阻塞住。
        pass


def publish(task_id: int, event: dict) -> None:
    """从任意线程发布事件（线程安全）。"""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    with _lock:
        queues = list(_subscribers.get(task_id, []))
    if not queues:
        return
    try:
        for queue in queues:
            loop.call_soon_threadsafe(_put_nowait, queue, event)
    except RuntimeError:
        # 进程正在关闭，事件循环已停：静默丢弃。
        pass


async def subscribe(task_id: int):
    """订阅某任务的实时事件；生成器结束时（客户端断开）自动退订。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    with _lock:
        _subscribers[task_id].append(queue)
    try:
        while True:
            event = await queue.get()
            yield event
    finally:
        with _lock:
            queues = _subscribers.get(task_id, [])
            if queue in queues:
                queues.remove(queue)
            if not queues:
                _subscribers.pop(task_id, None)


def subscriber_count(task_id: int) -> int:
    """测试/排查用：当前有多少个 SSE 连接。"""
    with _lock:
        return len(_subscribers.get(task_id, []))