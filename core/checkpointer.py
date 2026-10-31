"""
core/checkpointer.py - LangGraph 的 SqliteSaver 进程级单例

为什么要 checkpointer：
没有它，进程被 kill 后内存里的图状态全部消失，任务会永久卡在 running；
有它之后，每个 superstep（节点执行完）都会落一条快照，重启时用 thread_id 找回最新状态继续跑。

为什么「进程级单例」：
SqliteSaver 内部自带 threading.Lock 串行化 checkpoint 读写（官方文档明确：
check_same_thread=False 是安全的，因为实现里加锁了），所以整个进程共用一个 saver +
一个 sqlite 连接即可；每个请求各开连接反而更容易遇到 "database is locked"。
thread_id 用 task_id，天然把不同任务的状态隔离在不同 thread 里。

WAL 模式：读写不互相阻塞，SSE/详情接口在读快照的同时后台线程可以继续写。
"""
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import config

_saver: SqliteSaver | None = None
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def get_checkpointer() -> SqliteSaver:
    """懒加载单例；首次调用时创建 sqlite 连接并初始化表结构（SqliteSaver.setup 自动建表）。"""
    global _saver, _conn
    if _saver is not None:
        return _saver
    with _lock:
        if _saver is None:
            path = Path(config.CHECKPOINT_DB)
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False 的前提是 SqliteSaver 自己用锁串行化访问（已确认源码如此）。
            _conn = sqlite3.connect(str(path), check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA busy_timeout=30000")
            _saver = SqliteSaver(_conn)
            print(f"[checkpointer] SqliteSaver 就绪: {path}")
    return _saver


def checkpointer_info() -> dict:
    """给 /api/health 用：返回类型和落盘路径，方便排查「重启后为什么没恢复」。"""
    return {
        "type": "sqlite",
        "path": config.CHECKPOINT_DB,
        "ready": _saver is not None,
    }


def delete_thread(task_id: int) -> None:
    """删除任务时同步清掉它的 checkpoint，避免 checkpoints.db 无限增长。"""
    try:
        get_checkpointer().delete_thread(str(task_id))
    except Exception as exc:
        # 清理失败不应该让删除任务失败：DB 行已删，残留快照不会被再次读取。
        print(f"[checkpointer] 删除 thread {task_id} 快照失败（可忽略）: {exc}")


def close_checkpointer() -> None:
    """进程退出时关闭 sqlite 连接。"""
    global _saver, _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _saver = None