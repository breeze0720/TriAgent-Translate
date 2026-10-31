# 项目二 · 教学讲解文档索引

这套文档把 `02-多智能体翻译-LangGraph/` 的**最终代码**拆成 10 篇讲解，目标是让基础薄弱的同学
能对着代码读懂 LangGraph 多智能体，并且能在面试里讲清楚"为什么这么设计"。

- 只讲代码里**真实存在**的东西：数字、阶段名、表名、函数名都来自最终代码与真实运行数据；
- 每篇固定三段：`1. 这段代码在做什么` → `2. 为什么这样写（对比错写法）` → `3. 面试官可能怎么问`；
- 术语第一次出现给中英对照；代码片段控制在 10-25 行，逐段解释"这行在做什么"；
- 不吹：每个设计都写了取舍（为什么不用纯协商、为什么 SQLite 而不是 Postgres）。

## 篇目

| # | 文件 | 一句话 |
|---|------|--------|
| 00 | [总览与图结构](讲解/00-总览与图结构.md) | 主控-专家星型拓扑长什么样、一次完整执行经过哪些节点、为什么不做纯辩论式多智能体 |
| 01 | [State 设计](讲解/01-state-设计.md) | 覆盖型 vs 累积型字段、`Annotated[list, operator.add]` 的意义、`version` 与 `history` 各自的作用 |
| 02 | [六个专家节点](讲解/02-nodes.md) | 术语/翻译/审校/润色/人工/收尾各自干什么、prompt 要点、4 维 rubric 与结构化输出 |
| 03 | [Checkpointer](讲解/03-checkpointer.md) | SqliteSaver 进程级单例、`thread_id=task_id`、`get_state` 判断能否续跑、挂掉为什么不丢任务 |
| 04 | [Human-in-the-loop](讲解/04-human-in-loop.md) | `interrupt()` 暂停到前端"通过"的完整调用链、待审核怎么落库、为什么不用轮询 |
| 05 | [连接池与事件表](讲解/05-db-pool-events.md) | 5 条连接的池子怎么借还、三张表分工、幂等 upsert、时间线与 SSE 的关系 |
| 06 | [SSE 与后台执行](讲解/06-sse-runner.md) | 线程池 + 事件队列 + SSE 的数据流、跨线程安全、断线重连、为什么不用 WebSocket |
| 07 | [三个 bug 复盘](讲解/07-bugfix-log.md) | 死循环、条件边写库、失败原因丢失：原代码 → 问题 → 修复 → 回归测试 |
| 08 | [四节点四模型](讲解/08-四节点四模型.md) | 四个专家各配一个模型（provider:model）、按节点候选链降级、日志/时间线/接口三处可核对；附可选双审校投票 |
| 09 | [文档上传](讲解/09-文档上传.md) | 粘贴文本之外的第二种输入：五道校验（白名单/体积/zip 炸弹/解析/抽取结果）、为什么单独开端点、扫描件边界 |

## 建议阅读顺序

**第一次读（约 2 小时）**：00 → 01 → 02 → 03 → 04 → 06 → 05 → 07 → 08 → 09

- 先看 00 建立"图长什么样"的整体印象；
- 01/02 是读代码的主干：state 是数据结构，节点是业务逻辑；
- 03/04 是两个"面试亮点"：断点恢复 + 人在环；
- 06 讲执行与推送（工程部分），05 讲存储（可以稍微放后面）；
- 07 最后读，因为它会对照旧代码，先看完新代码才有感觉；
- 09 讲输入侧：上传只是"多一种输入形态"，校验链与边界都在这里。

**只准备面试（约 45 分钟）**：00（图与三条理由）→ 03（断点续跑）→ 04（interrupt）→ 07（三个 bug）→ 08（四节点四模型）→ 09（上传的五道校验，30 秒能讲完）。

**要排障**：05（数据在哪张表）→ 06（为什么没收到推送）→ 03（为什么没恢复）。

## 三句话讲完这个项目

1. 用 LangGraph 的 `StateGraph` 把翻译拆成"术语 → 翻译 → 审校 → 润色"四个专家，
   由一个不调 LLM 的 supervisor 按 `stage` 做确定性路由，审校不过就带意见重译，最多 3 次；
2. `SqliteSaver` 每个 superstep 落快照、`thread_id` 用 `task_id`，所以进程挂了能续跑，
   人工审核用 `interrupt()` 暂停、`Command(resume=...)` 恢复；
3. 后台用 4 线程的 `ThreadPoolExecutor` 跑同步图，跨线程用 `call_soon_threadsafe` 投递事件，
   通过 SSE 推进度，DB 里 `tasks / task_events / glossary` 三张表负责持久化与幂等。

## 关键数字速查（都来自 `config.py`，别记错）

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
| `ALLOWED_UPLOAD_EXTENSIONS` | `.pdf/.docx/.md/.txt` | 上传格式白名单（刻意不含表格/幻灯片） |

## 常用验证命令

```powershell
# 健康检查（数据库 + checkpointer + 上传限制 upload.formats/max_mb）
curl.exe -s http://127.0.0.1:8001/api/health

# 最近 5 个任务
curl.exe -s "http://127.0.0.1:8001/api/tasks?limit=5"

# 看某个任务的时间线与历史（events / history 两个数组）
curl.exe -s http://127.0.0.1:8001/api/tasks/3

# 上传文档建任务（multipart；注意不要手写 Content-Type）
curl.exe -s -X POST http://127.0.0.1:8001/api/tasks/upload -F "file=@员工手册.docx" -F "source_lang=中文" -F "target_lang=English"
```

跑测试（不调真实模型，全 mock）：

```powershell
cd "02-多智能体翻译-LangGraph"
python -m pytest tests/ -q
# 7 个文件、66 个用例：状态机纯函数、重试上限、术语一致性、checkpoint 恢复、
# 四节点模型路由、双模型投票合并、文档上传校验（19 个）
```

## 说明

- 文档只描述现有代码，**不包含任何代码修改**；
- 真实数据示例来自本机运行的 `127.0.0.1:8001`（任务 #2 / #3 / #4 / #11 / #12 / #13）；
- 历史 bug 的旧代码来自 `_archive/old-annotate_backup/`（v1 备份），只用于 07 篇对照；
- 如果代码后续有改动，以代码为准，欢迎回来改文档。