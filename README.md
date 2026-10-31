# 多智能体翻译工作台（LangGraph 主控-专家架构）

中文长文 → 英文，走一条**可观测、可中断、可恢复**的 Agent 流水线：

```
Supervisor（主控）
   ├─ terminology  术语抽取 → 结构化术语表
   ├─ translate    分段翻译（注入术语表，保证全文一致）
   ├─ review       4 维 rubric 审校 + issues 列表
   ├─ polish       只改 issue 指向的问题，不整篇重译
   ├─ human_review interrupt() 暂停，等人审核（可改译文）后 resume
   └─ finalize     合并、评分落库、导出
```

**技术栈**：FastAPI · LangGraph（`StateGraph` + `interrupt` + checkpointer）· DeepSeek / Qwen / GLM（四节点各配一个模型，异质模型互挑错）· MySQL（pymysql 连接池）· Vue 3 + Vite（三栏工作台，后端单端口托管）

---

## 1. 30 秒跑起来

```powershell
# 1) 建库（只需一次）
cd 02-多智能体翻译-LangGraph
mysql -u root -p < translate_agent.sql
python init_db.py

# 2) 起服务（同时托管前端）
D:\Anaconda\envs\xhu\python.exe main.py
# 浏览器打开 http://127.0.0.1:8001/app/
```

`.env` 需要 `DEEPSEEK_API_KEY`、`QWEN_API_KEY`、`GLM_API_KEY`（四节点四模型要三家 key 都齐才是四个不同模型；缺哪家，对应节点按候选链降级并在启动日志打 `*`）以及 MySQL 连接信息。
前端开发模式：`cd frontend && npm run dev`（Vite 代理 `/api` 到 8001），构建 `npm run build` 产出 `dist/` 由后端托管。

---

## 2. 为什么是"主控-专家"而不是纯协商

| 方案 | 成本 | 可控性 | 可测性 | 结论 |
|---|---|---|---|---|
| 单 Agent 一条 prompt 全干 | 1x | 差（出错无法定位） | 差 | 做不到人工介入/断点续跑 |
| 纯协商/辩论（多版译文本互评投票） | Nx | 差（轮次不可控） | 差（哪版更好仍要人判） | 演示易翻车 |
| **主控-专家 + 有限回环**（本项目） | 1.2-1.5x | 好（每步可观测） | 好（每阶段有分数） | ✅ |

协商没有被丢掉，而是**收敛成一条边**：审校意见回流到翻译节点，最多重试 3 次（超限转人工/失败），既有闭环又不会失控。

## 3. 关键机制

| 机制 | 实现 |
|---|---|
| 状态设计 | 覆盖型字段（译文/评分/version）+ 累积型字段（`history: Annotated[list, operator.add]`，防止重放冲掉日志） |
| 断点续跑 | `SqliteSaver` 进程级单例，`thread_id = task_id`，每步自动存快照；重启后 `get_state()` 判断能否续跑 |
| 人在环 | `human_review` 节点 `interrupt()` 挂起（`status=waiting_human, progress=90`），`Command(resume=...)` 恢复，人工修改的译文被采纳 |
| 防死循环 | 重试计数在翻译节点**无条件自增**，条件边只做判断；有专门回归测试断言"最多 3 次" |
| 纯函数条件边 | 条件边只返回下一节点名，绝不写库（写库在节点内做幂等 upsert） |
| 幂等落库 | 三表 `tasks / task_events / glossary`，唯一键 `(task_id, stage, version)` |
| 实时进度 | SSE `/api/tasks/{id}/events`（`event: progress`），任务结束立即推终态快照再关闭 |
| 四节点四模型 | 术语/翻译/审校/润色分别用不同模型（`provider:model` 配置）；缺 key 时按**每个节点自己的候选链**降级，并在启动日志标记，不静默 |
| 可选双审校投票 | `REVIEW_PROVIDERS=qwen,glm` 时审校并行问两家，每维取严、issues 并集、分差 ≥10 记分歧（默认关闭） |
| 文档上传 | 支持 PDF / Word / Markdown / TXT：五道校验（白名单 → 体积 → zip 炸弹 → 解析 → 抽取结果）后转成 `source_text`，之后走**同一条**流水线 |
| 单端口 | 后端所有 `/api/*` 之后挂 `StaticFiles(frontend/dist)`，`/app/` 打开工作台，前端路由刷新回退 index.html |

### 3.1 四节点四模型：为什么不是"一个模型干四份活"

```powershell
# .env（已写好默认值，按需改）
MODEL_TERMINOLOGY=glm:glm-4-flash      # 结构化抽取，量大、判错代价低 → 用便宜快模型
MODEL_TRANSLATE=deepseek:deepseek-chat # 语言质量是核心指标 → 生成主力
MODEL_REVIEW=qwen:qwen3.7-max          # 评判只调一次，但要求判断力 → 思考型
MODEL_POLISH=glm:glm-4.6               # 长文改写，要语感 → 写作向
```

- **配置格式** `provider:model`，provider ∈ `deepseek / qwen / glm`（都是 OpenAI 兼容端点，一个客户端类通吃）。
- **缺 key 不崩、也不静默**：每个节点有自己的降级候选链（`config.NODE_MODEL_FALLBACKS`），例如术语在没配 GLM 时降到 `qwen:qwen-turbo`、润色降到 `qwen:qwen-plus`；实际生效的四个模型会在启动日志和任务时间线的第一行打印，降级的模型带 `*`：

  ```text
  [Startup] 四节点模型：术语=qwen-turbo* · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=qwen-plus*
  [init] 初始化：原文切分为 1 段，进入术语抽取；模型分工：术语=qwen-turbo* · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=qwen-plus*
  ```

- **客户端缓存 key 带 model**：`get_llm(provider, model, temperature)`，避免"术语用 glm-4-flash、润色用 glm-4.6"被错误复用成同一个客户端。
- **`/status` 可核对**：`node_models` 字段直接返回四个节点实际生效的模型名。

> 可选增强：想再加一层"评审可信度"，把 `REVIEW_PROVIDERS` 设成 `qwen,glm` 就开启双模型审校投票（两个 provider 都要有 key），合并规则在 `core/review_vote.py`（纯函数、13 个用例）。默认关闭，因为"四节点四模型"本身已经能讲清模型分工。

## 4. 实测（2026-10-22 真实 API 调用）

- **端到端**：270 字中文 → English，`terminology → translate → review → polish → finalize`，抽出 **15 个术语**，retry=1，最终评分 **91.25**。
- **人工审核**：任务停在 `waiting_human / human_review / 90%`；`POST /api/tasks/{id}/resume` 带人工修改译文 → 继续跑完并**采用人工版本**。
- **断点续跑**：在 review 阶段 `Stop-Process -Force` 杀进程，重启日志 `[runner] 恢复任务 #4（mode=resume）`，续跑完成 score 95.05，事件表含 `recovery`，崩溃前 history 无重复。
- **测试**：`pytest -q` → **66 passed**（状态机、重试边界、checkpoint 恢复、术语一致性、四节点模型路由与降级 8 例、双模型投票合并 14 例、文档上传校验 19 例）。
- **四节点四模型**：任务 #11（`客户反馈显示，新版登录页面在弱网环境下加载较慢…`）实际调用 **qwen-turbo（术语）→ deepseek-chat（翻译）→ qwen3.7-max（审校）→ qwen-plus（润色）** 四个不同模型，抽出 6 个术语，最终 94.0 分；时间线第一行即打印模型分工。
- **文档上传（2026-10-25）**：任务 #12 上传 `客户案例集.pdf`（2 页 → 847 字符）→ 4 段、20 术语、审校 **90.0**、retry=1 → done；任务 #13 上传 `员工手册.docx`（1229 字符）→ 17 段、20 术语、**89.15 分**、retry=1 → polish → done。复制粘贴走的是同一条流水线（时间线第一行的模型分工完全一致）。
- **上传边界**：`.xlsx` → 400「不支持的文件格式」、0 字节 → 400「内容为空」、伪造 docx → 400「解析失败：File is not a zip file」、21 MB → 413「超过上限 20 MB」。

> 现场演示的"震撼点"就是断点续跑那一条：杀进程 → 重启 → 刷新页面，任务还停在原地等人工审核。

## 5. 目录结构

```
02-多智能体翻译-LangGraph/
├── main.py                 # FastAPI：全部 /api/*、SSE、resume/retry/glossary/export、托管 /app
├── workflow.py             # StateGraph 定义：节点、条件边、supervisor 调度
├── agent.py                # 各节点的 LLM 调用与结构化输出（术语/翻译/审校/润色）
├── core/
│   ├── checkpointer.py     # SqliteSaver 单例（双重检查锁）
│   ├── events.py           # SSE 事件总线
│   ├── review_vote.py      # 双模型审校的合并策略（纯函数，13 个单测）
│   └── runner.py           # 执行/落库/恢复（含进行中任务去重）
├── database.py             # pymysql 连接池 + 三表读写 + 幂等 upsert
├── models.py               # 请求/响应模型 + ReviewResult schema
├── config.py               # 配置（模型、端口、池大小、重试上限、上传限制）
├── document_loader.py      # PDF / DOCX / MD / TXT 抽文本（pypdf、docx2txt）
├── upload_utils.py         # 上传五道防线的纯函数（白名单/体积/zip 炸弹/抽取结果/文件名消毒）
├── init_db.py              # 建表/初始数据
├── translate_agent.sql     # 建表语句（含索引与唯一键）
├── frontend/               # Vue 3 + Vite 三栏工作台
│   ├── src/App.vue         # 任务列表 / 详情 / SSE / 审核 / 术语 / 导出
│   ├── src/components/     # 11 个组件（侧栏、对照、时间线、评分卡、新建任务…）
│   ├── src/composables/    # useTheme / useNotifications / useTaskStream
│   └── dist/               # 构建产物（后端托管用）
├── tests/                  # 66 个 pytest 用例（7 个文件）
└── docs/讲解/              # 逐模块中文讲解（10 篇，含 3 个真实 bug 的前后对照）
```

## 6. 主要端

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 新建任务（`text`、`source_lang`、`target_lang`、`enable_human_review`） |
| POST | `/api/tasks/upload` | 上传文档建任务（multipart：`file` = PDF/DOCX/MD/TXT + 同上参数），返回 `source_file` 与 `source_chars` |
| GET | `/api/tasks` | 任务列表（状态、阶段、评分、进度） |
| GET | `/api/tasks/{id}` | 任务详情（译文四段、时间线、评分卡、issues） |
| GET | `/api/tasks/{id}/events` | SSE 实时进度（结束立即推终态并关闭） |
| POST | `/api/tasks/{id}/resume` | 人工审核后恢复（`approved`、`edited_translation`、`comment`） |
| POST | `/api/tasks/{id}/retry` | 失败重试 |
| POST | `/api/tasks/{id}/glossary` | 运行中编辑/补充术语（任务已结束时返回 409，防被 runner 覆盖） |
| GET | `/api/tasks/{id}/export?fmt=md|docx` | 导出 |
| DELETE | `/api/tasks/{id}` | 删除任务（含事件与术语） |
| GET | `/api/health` | DB + checkpointer 健康检查，并下发 `upload`（格式/体积/长度上限） |

## 7. Roadmap

1. 分段**并行**翻译（需先做术语锁定，否则一致性会被破坏）；
2. checkpoint 换 Postgres + SSE 换 Redis pub/sub，支持多 worker；
3. 术语表跨任务复用（领域词库）与自动对齐；
4. 质量回归集：固定 20 篇人工参考译文，量化 rubric 与人工评分的相关性；
5. Docker Compose 一键起（api + mysql + 前端静态）。

## 8. 已知限制

- **仅支持单进程**（SqliteSaver + 进程内 SSE 队列），`--workers>1` 不可用；
- Qwen 思考型审校单次 30-60 秒，是端到端延迟主要来源；
- `task_events` 版本号偶有跳号（checkpoint 已提交但 DB 未落库时进程被 kill），重放幂等已保证不出错，但时间线会出现空档；
- **扫描版/图片版 PDF 不支持**（pypdf 只能抽文字层，没接 OCR），会返回 400 并提示换文件或直接粘贴原文；
- 上传只收 `.pdf/.docx/.md/.txt`：表格/幻灯片抽取后会丢结构，故意不做；
- 邮件通知接口未做真实链路验证。