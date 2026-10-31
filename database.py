"""
database.py - MySQL 连接池与数据访问层

为什么不用 SQLAlchemy：
本项目只有 3 张表、十几条 SQL，用 SQLAlchemy 会引入 ~20MB 依赖和 ORM 心智负担，
收益（关系映射/迁移/多方言）在这个教学项目里为零。所以选择「自建轻量连接池 + 手写 SQL」：
- 池化解决旧版「每个函数 pymysql.connect() = 每次 TCP 握手」的压测瓶颈；
- 取连接时 ping(reconnect=True)，等价于 SQLAlchemy 的 pool_pre_ping，
  治 MySQL 8 小时空闲断连这个经典坑；
- 池大小 / 等待超时可配置，等不到连接会抛错而不是无限阻塞。

并发安全：一个连接同一时刻只借给一个线程（LifoQueue 天然互斥），归还时 commit/rollback，
所以调用方不需要（也不允许）跨函数持有连接。
"""
import json
import queue
import threading
from contextlib import contextmanager
from typing import Any

import pymysql

import config


# ============================================
# 轻量连接池
# ============================================

class _ConnectionPool:
    """固定大小的 pymysql 连接池。

    - 空闲连接放 LifoQueue：最近用过的连接更可能还活着，命中率更高；
    - 池满时阻塞等待 DB_POOL_TIMEOUT 秒，超时抛 RuntimeError（快速失败，便于定位慢查询）；
    - 取连接时 ping(reconnect=True)：连接被 MySQL 服务端回收（wait_timeout）后自动重连。
    """

    def __init__(self, size: int):
        if size <= 0:
            raise ValueError("DB_POOL_SIZE 必须为正整数")
        self._size = size
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _new_connection(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,  # 返回 dict，直接能转 JSON
            autocommit=False,
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
        )

    def acquire(self) -> pymysql.connections.Connection:
        if self._closed:
            raise RuntimeError("数据库连接池已关闭")
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            # 还有扩容额度就直接新建；否则说明并发已经打满，进入有限等待。
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    return self._new_connection()
            try:
                conn = self._idle.get(timeout=config.DB_POOL_TIMEOUT)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"数据库连接池等待超时（{config.DB_POOL_TIMEOUT}s），可能存在慢查询或连接泄漏"
                ) from exc

        try:
            conn.ping(reconnect=True)  # 探活 + 自动重连，等价 pool_pre_ping
            return conn
        except Exception:
            # 这条连接已经救不回来：换一条新的，保持池的 created 计数准确。
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)
                self._created += 1
            return self._new_connection()

    def release(self, conn: pymysql.connections.Connection) -> None:
        if conn is None:
            return
        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            # 理论上不会发生（借出数量受 size 限制），兜底关闭避免泄漏。
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def connection(self):
        """借一条连接：正常结束 commit，异常 rollback，无论如何都归还。"""
        conn = self.acquire()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass


_pool: _ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> _ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _ConnectionPool(config.DB_POOL_SIZE)
    return _pool


def get_connection():
    """对外暴露的连接上下文管理器（给需要手写事务的调用方用）。"""
    return _get_pool().connection()


def close_pool() -> None:
    """进程退出时释放所有空闲连接。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ============================================
# 通用 SQL 小工具
# ============================================

def _execute(sql: str, params: tuple | None = None) -> tuple[int, int]:
    """执行写 SQL，返回 (rowcount, lastrowid)。"""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount, cursor.lastrowid or 0


def _fetch_one(sql: str, params: tuple | None = None) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()


def _fetch_all(sql: str, params: tuple | None = None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())


def _json_dumps(value: Any) -> str:
    """统一 JSON 序列化：中文不转义，方便直接看数据库排查问题。"""
    return json.dumps(value, ensure_ascii=False, default=str)


# ============================================
# 任务 CRUD（新版 /api/* 使用）
# ============================================

def create_task(
    source_text: str,
    source_lang: str = "中文",
    target_lang: str = "English",
    style: str = "",
    domain: str = "",
    enable_human_review: bool = False,
) -> int:
    """新建任务（pending），返回自增 task_id。"""
    sql = """
        INSERT INTO tasks
            (source_text, source_lang, target_lang, style, domain, enable_human_review,
             status, stage, progress, retry_count, version)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'init', 0, 0, 0)
    """
    _, task_id = _execute(
        sql,
        (source_text, source_lang, target_lang, style, domain, 1 if enable_human_review else 0),
    )
    return task_id


def get_task(task_id: int) -> dict | None:
    return _fetch_one("SELECT * FROM tasks WHERE id = %s", (task_id,))


def list_tasks(limit: int = 20) -> list[dict]:
    """任务列表：按 id 倒序（created_at 同秒时 id 更可靠）。"""
    sql = """
        SELECT id, source_text, source_lang, target_lang, status, stage, progress,
               score, retry_count, error_message, version, created_at, updated_at
        FROM tasks
        ORDER BY id DESC
        LIMIT %s
    """
    return _fetch_all(sql, (max(1, min(limit, 100)),))


def list_tasks_by_status(statuses: list[str]) -> list[dict]:
    """启动恢复用：扫描需要续跑的任务。"""
    if not statuses:
        return []
    placeholders = ",".join(["%s"] * len(statuses))
    sql = f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id ASC"
    return _fetch_all(sql, tuple(statuses))


def update_task_runtime(
    task_id: int,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    error: str | None = None,
) -> bool:
    """只更新运行态字段（开始执行 / 刚入队时用）。"""
    fields: list[str] = []
    params: list[Any] = []
    for column, value in (("status", status), ("stage", stage), ("progress", progress), ("error_message", error)):
        if value is not None:
            fields.append(f"{column} = %s")
            params.append(value)
    if not fields:
        return False
    fields.append("updated_at = NOW()")
    params.append(task_id)
    rowcount, _ = _execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = %s", tuple(params))
    return rowcount > 0


# tasks 表允许被图执行结果覆盖的列；白名单防止动态 SQL 被注入。
_SNAPSHOT_COLUMNS = {
    "status", "stage", "progress", "retry_count", "score", "translation", "polished",
    "final_result", "review_json", "glossary_json", "segments_json", "translations_json",
    "history_json", "version", "error_message",
}


def save_task_snapshot(task_id: int, snapshot: dict) -> bool:
    """
    幂等落库：以 task_id 为主键 UPDATE 整份快照。

    为什么是「整份快照」而不是逐节点插入：checkpointer 重放/恢复到某一步时，
    重复执行同一节点只会把同一份 state 再写一次，结果必然一致（幂等）；
    前端读到的永远是最新完整状态，不会出现「只更新了一半」的中间态。
    """
    data = {key: value for key, value in snapshot.items() if key in _SNAPSHOT_COLUMNS}
    if not data:
        return False
    assignments = ", ".join(f"{column} = %s" for column in data)
    params = list(data.values()) + [task_id]
    rowcount, _ = _execute(
        f"UPDATE tasks SET {assignments}, updated_at = NOW() WHERE id = %s",
        tuple(params),
    )
    return rowcount > 0


def mark_failed(task_id: int, error: str) -> bool:
    """
    标记失败。注意 error 参数必须落库（旧版 SQL 只 SET status='failed'，
    失败原因整条丢失，前端既不能展示也没法判断该不该重试）。
    """
    rowcount, _ = _execute(
        "UPDATE tasks SET status = 'failed', error_message = %s, progress = 100, updated_at = NOW() WHERE id = %s",
        (str(error)[:1000], task_id),
    )
    return rowcount > 0


def reset_task_for_retry(task_id: int) -> bool:
    """/api/tasks/{id}/retry：清掉运行态后重新入队。"""
    rowcount, _ = _execute(
        """
        UPDATE tasks
        SET status = 'pending', stage = 'init', progress = 0, retry_count = 0,
            score = NULL, error_message = '', translation = '', polished = '', final_result = '',
            updated_at = NOW()
        WHERE id = %s
        """,
        (task_id,),
    )
    return rowcount > 0


def delete_task(task_id: int) -> bool:
    """删除任务；task_events / glossary 由外键 ON DELETE CASCADE 一并清理。"""
    rowcount, _ = _execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    return rowcount > 0


# ============================================
# 事件（task_events，幂等：task_id + stage + version 唯一）
# ============================================

def append_event(task_id: int, stage: str, status: str, message: str = "", version: int = 0) -> None:
    """
    追加一条阶段事件。

    唯一键 (task_id, stage, version) + ON DUPLICATE KEY UPDATE：
    图重放时同一版本的同一阶段只会更新那一条，而不是插出重复记录（前端进度流不会重复刷）。
    """
    _execute(
        """
        INSERT INTO task_events (task_id, stage, status, message, version)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE status = VALUES(status), message = VALUES(message)
        """,
        (task_id, stage[:30], status[:30], (message or "")[:1000], int(version or 0)),
    )


def get_events(task_id: int, limit: int = 200) -> list[dict]:
    sql = """
        SELECT stage, status, message, version, created_at
        FROM task_events WHERE task_id = %s ORDER BY id ASC LIMIT %s
    """
    return _fetch_all(sql, (task_id, max(1, min(limit, 500))))


# ============================================
# 术语表（glossary，人工可编辑）
# ============================================

def replace_glossary(task_id: int, glossary: list[dict]) -> None:
    """整表替换：先删后插，保证和前端提交的列表完全一致（而不是残留旧术语）。"""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM glossary WHERE task_id = %s", (task_id,))
            for item in glossary:
                term = str(item.get("term") or "").strip()
                translation = str(item.get("translation") or "").strip()
                if not term or not translation:
                    continue
                cursor.execute(
                    """
                    INSERT INTO glossary (task_id, term, translation, note)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (task_id, term[:200], translation[:200], str(item.get("note") or "")[:500]),
                )


def get_glossary(task_id: int) -> list[dict]:
    rows = _fetch_all(
        "SELECT term, translation, note FROM glossary WHERE task_id = %s ORDER BY id ASC",
        (task_id,),
    )
    return rows


# ============================================
# 健康检查
# ============================================

def test_connection() -> bool:
    try:
        row = _fetch_one("SELECT 1 AS ok")
        return bool(row and row.get("ok") == 1)
    except Exception as exc:
        print(f"[数据库] 连接测试失败: {exc}")
        return False


# ============================================
# 旧版函数兼容层（旧接口 /translate、/task/{id} 仍会走到这里）
# ============================================

def get_all_tasks(page: int = 1, page_size: int = 20) -> list[dict]:
    offset = max(0, (page - 1) * page_size)
    sql = """
        SELECT id, source_text, source_lang, target_lang,
               score AS review_score, status, retry_count, created_at, updated_at
        FROM tasks
        ORDER BY id DESC
        LIMIT %s OFFSET %s
    """
    return _fetch_all(sql, (page_size, offset))


def get_task_count() -> int:
    row = _fetch_one("SELECT COUNT(*) AS cnt FROM tasks")
    return int(row["cnt"]) if row else 0


def update_task_status(task_id: int, status: str) -> None:
    update_task_runtime(task_id, status=status)


def save_translation(task_id: int, translation: str) -> None:
    _execute("UPDATE tasks SET translation = %s, updated_at = NOW() WHERE id = %s", (translation, task_id))


def save_review(task_id: int, feedback: str, score: float) -> None:
    review = {"overall": float(score), "issues": [feedback] if feedback else [], "legacy_feedback": feedback}
    _execute(
        "UPDATE tasks SET review_json = %s, score = %s, updated_at = NOW() WHERE id = %s",
        (_json_dumps(review), float(score), task_id),
    )


def save_polished(task_id: int, polished: str) -> None:
    _execute(
        """
        UPDATE tasks SET polished = %s, final_result = %s, translation = %s, status = 'done',
                         progress = 100, updated_at = NOW()
        WHERE id = %s
        """,
        (polished, polished, polished, task_id),
    )


def increment_retry(task_id: int) -> int:
    """旧接口兼容；新版重试计数在图 state 里维护，不再依赖这条 SQL。"""
    _execute(
        "UPDATE tasks SET retry_count = retry_count + 1, updated_at = NOW() WHERE id = %s",
        (task_id,),
    )
    row = _fetch_one("SELECT retry_count FROM tasks WHERE id = %s", (task_id,))
    return int(row["retry_count"]) if row else 0


def save_review_record(task_id: int, version: int, translation_text: str, review_content: str, score: float) -> None:
    """旧接口兼容：把审校记录转成一条幂等事件，不再写已废弃的 review_records 表。"""
    append_event(task_id, stage="review", status="reviewed", message=review_content[:1000], version=version)
    _execute(
        "UPDATE tasks SET translation = %s, score = %s, updated_at = NOW() WHERE id = %s",
        (translation_text, float(score), task_id),
    )