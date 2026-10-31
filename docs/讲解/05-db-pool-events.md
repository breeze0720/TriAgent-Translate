# 05 · 连接池与事件表：MySQL 这一层怎么扛住并发与重放

多智能体系统的"业务真相"最终落在 MySQL：任务快照、阶段事件、术语表。
本篇讲三件事：自建连接池（为什么不用 SQLAlchemy）、三张表怎么分工、以及**幂等 upsert（幂等写入）**
如何保证图重放时不会写出重复数据。

对应代码：`database.py`、`config.py` 的 `DB_POOL_*`、`init_db.py`、`translate_agent.sql`、`core/runner.py` 的 `_persist`。

## 1. 这段代码在做什么

### 1.1 连接池参数与取连接流程（`database.py` 30-123 行）

配置只有两个旋钮（`config.py` 60-61 行）：

```python
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))        # 池里最多 5 条连接
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))  # 等不到连接最多等 10 秒
```

```python
def acquire(self):
    if self._closed:
        raise RuntimeError("数据库连接池已关闭")
    try:
        conn = self._idle.get_nowait()                 # 1) 先拿空闲连接（LIFO）
    except queue.Empty:
        with self._lock:                               # 2) 没空闲：还没到上限就新建
            if self._created < self._size:
                self._created += 1
                return self._new_connection()
        try:
            conn = self._idle.get(timeout=config.DB_POOL_TIMEOUT)   # 3) 打满则有限等待
        except queue.Empty as exc:
            raise RuntimeError(f"数据库连接池等待超时（{config.DB_POOL_TIMEOUT}s）…") from exc
    try:
        conn.ping(reconnect=True)                      # 4) 探活 + 自动重连
        return conn
    except Exception:
        ...                                            # 5) 救不回来就换一条新连接
```

`connection()` 上下文管理器负责事务边界：正常 `commit`、异常 `rollback`、`finally` 归还（112-123 行）。

### 1.2 三张表的分工

| 表 | 主键/唯一键 | 存什么 | 谁写 |
|---|---|---|---|
| `tasks` | `id`（自增） | 入参 + 运行态 + 结果快照（JSON 列） | `_persist` 整份覆盖 UPDATE |
| `task_events` | `(task_id, stage, version)` 唯一 | 阶段时间线：谁在什么时候走到哪 | `append_event` 幂等 upsert |
| `glossary` | `(task_id, term)` 唯一，FK 级联删除 | 人工可编辑的术语表 | `replace_glossary` 先删后插 |

`tasks` 的列分三类：

- **入参**：`source_text / source_lang / target_lang / style / domain / enable_human_review`；
- **运行态**：`status / stage / progress / retry_count / version / error_message`；
- **结果快照**：`translation / polished / final_result / review_json / glossary_json /
  segments_json / translations_json / history_json`。

JSON 列是刻意的：state 里的结构化数据（4 维评分、分段、历史）不需要被 SQL 查询，
直接整块存 JSON，读的时候反序列化即可。

### 1.3 幂等写入的两个 SQL

**任务快照：按主键整份 UPDATE**（`database.py` 277-294 行）：

```python
def save_task_snapshot(task_id: int, snapshot: dict) -> bool:
    data = {key: value for key, value in snapshot.items() if key in _SNAPSHOT_COLUMNS}
    assignments = ", ".join(f"{column} = %s" for column in data)
    params = list(data.values()) + [task_id]
    _execute(f"UPDATE tasks SET {assignments}, updated_at = NOW() WHERE id = %s", tuple(params))
```

`_SNAPSHOT_COLUMNS` 是**白名单**（270-274 行），既防注入也防误写（比如不许图执行改入参）。
整份覆盖的好处：同一 superstep 重放多少次，写进去的都是同一份 state，结果必然一致。

**事件：唯一键 + ON DUPLICATE KEY UPDATE**（334-348 行）：

```python
INSERT INTO task_events (task_id, stage, status, message, version)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE status = VALUES(status), message = VALUES(message)
```

同一个 `(task_id, stage, version)` 重复插入时只更新状态和文案，不会多出一行——
前端进度流不会因为图重放而"刷两遍"。

### 1.4 `_persist`：所有落库的唯一入口（`core/runner.py` 80-131 行）

```python
def _persist(task_id, values, *, status=None, message=""):
    stage = str(values.get("stage") or "init")
    effective_status = status or str(values.get("status") or "running")
    progress = _progress_for(stage, effective_status)     # 阶段 → 百分比，统一口径
    ...
    database.save_task_snapshot(task_id, snapshot)        # 1) 整份快照
    database.replace_glossary(task_id, values.get("glossary") or [])  # 2) 术语表整表替换
    database.append_event(task_id, stage, effective_status, message, version)  # 3) 事件
    event = {"task_id": task_id, "stage": stage, "status": effective_status,
             "progress": progress, "message": message or _last_note(values), "version": version}
    if review:
        event["review"] = review                          # 有评分才带，省带宽
    publish(task_id, event)                               # 4) SSE 实时推
```

**顺序很重要**：先落库再推 SSE。这样客户端收到事件后立刻 `GET /api/tasks/{id}`，
读到的状态一定不比事件旧。

### 1.5 时间线与 SSE 的关系

```
图每执行完一个节点
   │
   ├─► _persist ──► tasks 表（最新快照，可查询）
   │            ──► task_events 表（追加一条时间线，可回放）
   │            ──► publish() ──► 内存队列 ──► SSE（实时，断了就没有）
   │
浏览器
   ├─ 刷新/重连：GET 详情（tasks + task_events + checkpoint），立刻拿到完整状态
   └─ 在线：SSE 增量更新（只带 stage/status/progress/message/review/version）
```

一句话：**`task_events` 是"持久的事实"，SSE 是"实时的通知"**。
SSE 断了不补发历史，靠重连后的一次快照对齐；这也解释了为什么 SSE 接口第一帧永远是
`message="当前状态快照"`。

## 2. 为什么这样写（对比错写法）

### 2.1 为什么不用 SQLAlchemy

本项目只有 3 张表、十几条 SQL。SQLAlchemy（Python 的 ORM，对象关系映射框架）会带来约 20MB 依赖、
模型映射、会话生命周期、迁移工具一整套心智负担，而这个项目需要的能力（连接池、事务、幂等 upsert）
用 150 行 pymysql 就能覆盖。
面试如果被问"为什么不用 ORM"，回答重点不是"ORM 不好"，而是"**这个规模下 ORM 的收益为零**"。

### 2.2 对比错写法：每个函数自己 connect

v1 的写法：

```python
# ❌ 每次调用都 TCP 握手 + 认证，压测直接打满 MySQL 的 max_connections
def save_translation(task_id, translation):
    conn = get_connection()          # pymysql.connect(...)
    try:
        ...
    finally:
        conn.close()
```

连接池把它变成"借—用—还"：连接复用、上限可控、`ping(reconnect=True)` 解决 MySQL 8 小时
`wait_timeout` 断连。池满时的**快速失败**（抛 `RuntimeError` 而不是无限等）也比挂死好排查。

### 2.3 对比错写法：逐节点 INSERT 事件 / 逐字段 UPDATE 任务

```python
# ❌ 每次都可能插重复：重放一次就多一行，前端进度流出现两条一样的
cursor.execute("INSERT INTO task_events (task_id, stage, message) VALUES (%s,%s,%s)", ...)

# ❌ 只更新变化的列：并发/重放时可能写出"stage 已经 reviewed，translation 还是上一版"的中间态
cursor.execute("UPDATE tasks SET stage=%s WHERE id=%s", ...)
```

正确做法分别是：事件用唯一键 + `ON DUPLICATE KEY UPDATE`；任务用整份快照覆盖。
**幂等的本质是"把同一份输入写成同一份输出"，而不是"想办法不重复执行"。**

### 2.4 诚实取舍：为什么业务库是 MySQL，而 checkpoint 用 SQLite

| | 业务数据（`tasks` 等） | 图状态（checkpoint） |
|---|---|---|
| 存储 | MySQL | SQLite 文件 |
| 原因 | 多线程并发写、要按状态扫描、utf8mb4、部署机已有 MySQL | 单进程、按 thread 顺序写、零运维、删库即可重来 |
| 生产替代 | MySQL 足够；量再大再考虑分库 | 换 `PostgresSaver`，接口不变 |

其他取舍：

- 手写 SQL **没有迁移框架**，靠 `init_db.py` 的 `_ensure_columns()` 查 `information_schema` 补列（108-124 行），
  表结构一复杂就会失控；
- 连接池没有健康指标（等待时长、超时次数），只有一条错误日志，可观测性一般；
- `replace_glossary` 是"先 DELETE 再逐条 INSERT"，任务结束前每个 superstep 都会执行一次，
  写放大明显；正确做法是只在术语表真的变化时才写（目前靠图里变化不频繁来兜底）。

## 3. 面试官可能怎么问

**Q1：为什么自己写连接池？池大小和超时怎么定？**
A：项目只有 pymysql 和十几条 SQL，引 ORM 不划算；池大小按"后台 worker 数 + 少量 API 并发"取 5，
超时 10 秒是为了快速失败——等太久说明有慢查询或泄漏，应该报错让人看见，而不是把请求线程挂死。

**Q2：图重放时怎么保证不写出重复数据？**
A：任务表按主键整份快照覆盖写；事件表用 `(task_id, stage, version)` 唯一键 + `ON DUPLICATE KEY UPDATE`；
术语表整表替换。同一 superstep 重放多少次，结果都是同一份。

**Q3：三张表为什么这么分？时间线和 SSE 是什么关系？**
A：`tasks` 存最新事实、`task_events` 存过程、`glossary` 单独成表是为了人工编辑；
SSE 只做实时通知、不负责历史，断线重连时先读 DB 快照再订阅增量，二者靠 `version` 对齐。

---

**一句话总结**：连接池解决"连得上"，唯一键 + 整份快照解决"重放也不脏"，事件表 + SSE 解决"看得见"。