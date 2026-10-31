# 设计文档 · 多智能体翻译与审校

把 `02-多智能体翻译-LangGraph/` 的最终代码拆成 10 篇设计说明，重点说明各机制的实现方式、
设计取舍与边界条件。

- 只写代码中**真实存在**的内容：阶段名、表名、函数名、阈值都来自最终代码与真实运行数据；
- 每篇固定三段：`1. 实现概览` → `2. 设计取舍与备选方案` → `3. 常见问题（FAQ）`；
- 术语首次出现给中英对照；代码片段控制在 10–25 行；
- 每个设计都写明取舍（为什么不用纯协商、为什么 SQLite 而不是 Postgres）。

## 文档列表

| # | 文件 | 内容摘要 |
|---|------|--------|
| 00 | [overview](./00-overview.md) | 主控-专家星型拓扑、一次完整执行的节点轨迹、为什么不采用纯辩论式多智能体 |
| 01 | [state-design](./01-state-design.md) | 覆盖型 vs 累积型字段、`Annotated[list, operator.add]` 的意义、`version` 与 `history` 的作用 |
| 02 | [nodes](./02-nodes.md) | 术语/翻译/审校/润色/人工/收尾各自职责、prompt 要点、4 维 rubric 与结构化输出 |
| 03 | [checkpointer](./03-checkpointer.md) | SqliteSaver 进程级单例、`thread_id=task_id`、`get_state` 判断续跑、进程崩溃后的恢复扫描 |
| 04 | [human-in-loop](./04-human-in-loop.md) | `interrupt()` 暂停到前端「通过」的完整调用链、待审核状态如何落库、为什么不用轮询 |
| 05 | [db-pool-events](./05-db-pool-events.md) | 5 条连接的池子如何借还、三张表分工、幂等 upsert、时间线与 SSE 的关系 |
| 06 | [sse-runner](./06-sse-runner.md) | 线程池 + 事件队列 + SSE 的数据流、跨线程安全、断线重连、为什么不用 WebSocket |
| 07 | [bugfix-log](./07-bugfix-log.md) | 死循环、条件边写库、失败原因丢失：原代码 → 问题 → 修复 → 回归测试 |
| 08 | [model-routing](./08-model-routing.md) | 四个专家各配一个模型（`provider:model`）、按节点候选链降级、日志/时间线/接口三处核对；附可选双审校投票 |
| 09 | [upload](./09-upload.md) | 粘贴文本之外的第二种输入：五道校验（白名单/体积/zip 炸弹/解析/抽取结果）、为什么单独开端点、扫描件边界 |

## 阅读顺序

```
00 → 01 → 02 → 03 → 04 → 06 → 05 → 07 → 08 → 09
```

- 先看 `00` 建立「图长什么样」的整体印象；
- `01` / `02` 是主干：state 是数据结构，节点是业务逻辑；
- `03` / `04` 是两处核心机制：断点恢复与人在环；
- `06` 讲执行与推送（工程部分），`05` 讲存储（可稍后读）；
- `07` 建议最后读，它对照旧代码，先看完新代码才有感觉；
- `09` 是输入侧：上传只是「多一种输入形态」，校验链与边界都在这里。

排障入口：`05`（数据在哪张表）→ `06`（为什么没收到推送）→ `03`（为什么没恢复）。

## 项目概要

1. 用 LangGraph 的 `StateGraph` 把翻译拆成「术语 → 翻译 → 审校 → 润色」四个专家，
   由一个不调用 LLM 的 supervisor 按 `stage` 做确定性路由，审校不通过则携带意见重译，最多 3 次；
2. `SqliteSaver` 每个 superstep 落快照、`thread_id` 用 `task_id`，进程崩溃后可续跑；
   人工审核用 `interrupt()` 暂停、`Command(resume=...)` 恢复；
3. 后台用 4 线程的 `ThreadPoolExecutor` 跑同步图，跨线程用 `call_soon_threadsafe` 投递事件，
   通过 SSE 推进度；`tasks / task_events / glossary` 三张表负责持久化与幂等。

## 关键参数（均来自 `config.py`）

| 参数 | 值 | 含义 |
|---|---|---|
| `PASS_SCORE` | 80 | 审校加权总分 ≥ 80 视为通过 |
| `MAX_RETRY` | 3 | 翻译节点最多进入 3 次（首译 + 2 次重译） |
| `REVIEW_WEIGHTS` | 0.4 / 0.25 / 0.2 / 0.15 | 忠实度/术语/流畅/完整性 |
| `SEGMENT_MAX_CHARS` | 300 | 分段翻译的单段字符上限 |
| `MAX_GLOSSARY_TERMS` | 20 | 术语表最多保留条数 |
| `DB_POOL_SIZE` / `DB_POOL_TIMEOUT` | 5 / 10s | MySQL 连接池大小 / 取连接超时 |
| `MAX_WORKERS` | 4 | 后台执行线程数 |
| `TASK_TIMEOUT_SECONDS` | 600 | 任务软超时（节点之间检查） |
| `PORT` | 8001 | 服务端口（8000 留给项目一） |
| `MAX_UPLOAD_MB` | 20 | 上传文档体积上限（`/api/tasks/upload`） |
| `MAX_SOURCE_CHARS` | 20000 | 原文长度上限，粘贴文本与上传文档共用 |
| `ALLOWED_UPLOAD_EXTENSIONS` | `.pdf/.docx/.md/.txt` | 上传格式白名单（不含表格/幻灯片） |

## 常用验证命令

```powershell
# 健康检查（数据库 + checkpointer + 上传限制 upload.formats/max_mb）
curl.exe -s http://127.0.0.1:8001/api/health

# 最近 5 个任务
curl.exe -s "http://127.0.0.1:8001/api/tasks?limit=5"

# 某个任务的时间线与历史（events / history 两个数组）
curl.exe -s http://127.0.0.1:8001/api/tasks/3

# 上传文档建任务（multipart；不要手写 Content-Type）
curl.exe -s -X POST http://127.0.0.1:8001/api/tasks/upload -F "file=@员工手册.docx" -F "source_lang=中文" -F "target_lang=English"
```

测试（全 mock，不调用真实模型）：

```powershell
python -m pytest tests/ -q
# 7 个文件、66 个用例：状态机纯函数、重试上限、术语一致性、checkpoint 恢复、
# 四节点模型路由、双模型投票合并、文档上传校验（19 个）
```

## 说明

- 文档只描述现有代码，不修改代码；
- 真实数据示例来自本地运行的 `127.0.0.1:8001`（任务 #2 / #3 / #4 / #11 / #12 / #13）；
- 历史 bug 的旧代码来自开发期的 v1 备份（不随仓库发布），仅用于 07 篇的前后对照；
- 代码有改动时以代码为准，文档同步更新。
