# 译笔工作台 · 前端

多智能体翻译与审校系统的三栏工作台。Vue 3 + Vite + 原生 CSS，无 UI 框架、无 Pinia。

## 运行

```bash
npm install
npm run dev      # http://127.0.0.1:5273
npm run build    # 产物输出到 dist/
```

开发环境由 `vite.config.js` 的 `/api` 代理转发到后端 `http://127.0.0.1:8001`；
生产部署时前后端同源即可，无需修改前端代码（可用 `VITE_API_BASE` 覆盖前缀）。

## 目录

```
src/
├── main.js                     入口，按 tokens → base → layout 顺序引入样式
├── App.vue                     三栏工作台：列表/详情状态、SSE 事件、审核与术语操作
├── api/client.js               REST 客户端（错误信息归一化、表单/JSON 提交）
├── composables/
│   ├── useTheme.js             跟随系统 + localStorage 手动主题
│   ├── useNotifications.js     通知栈（替代 alert）
│   └── useTaskStream.js        EventSource 订阅：退避重连、心跳看门狗、终态关闭
├── styles/
│   ├── tokens.css              设计变量（亮/暗两套）
│   ├── base.css                reset、按钮/输入/徽标/卡片等原语
│   └── layout.css              工作台外壳、三栏栅格、弹层、通知
├── utils/format.js             状态/阶段文案、时间与耗时、评分归一化、分段
└── components/
    ├── AppHeader.vue           品牌、后端连接状态、主题切换
    ├── TaskSidebar.vue         新建任务 + 任务列表（徽标/进度/评分/悬停删除）
    ├── CreateTaskModal.vue     新建任务弹层表单
    ├── BilingualPanel.vue      原文/译文对照（并排/上下，译文可编辑）
    ├── StageTimeline.vue       六阶段时间线，当前阶段高亮并显示耗时
    ├── ScoreCard.vue           四维条形 + overall + issues
    ├── GlossaryPanel.vue       术语表（增行/编辑/保存）
    ├── ReviewPanel.vue         通过 / 编辑后通过 / 打回重译 / 失败重试
    ├── WorkspaceEmpty.vue      未选中任务的空状态
    └── StatusBadge.vue         状态徽标
```

## 交互与状态

- 列表每 6s 静默轮询（仅当存在运行中任务且页面可见），SSE 负责单任务实时进度。
- SSE 断线后按 1s→8s 退避重连；30s 无任何数据视为半开连接，主动重连；重连成功后补拉一次详情。
- `done / failed / waiting_human` 视为稳定态：关闭 SSE，等待重试或人工操作后重新订阅。
- 删除、打回等破坏性操作均为两步确认，不使用 `alert`。

## 后端契约对接说明（已按 8001 真实后端实现校准）

- 列表项用 `id`（数字），详情用 `task_id`；前端内部统一转字符串 id 对齐 SSE。
- 状态取值：`pending / running / waiting_human / done / failed`；阶段取值：`init / terminology / translate / review / polish / human_review / final(ize)`，并兼容 `translated / reviewed / polished` 等完成态别名。
- SSE 为具名事件 `event: progress`（不是默认 message），已同时监听具名事件与默认 message。
- 时间为后端本地时间字符串 `YYYY-MM-DD HH:mm:ss` 与带时区 ISO 混用，前端统一解析。
- `POST /api/tasks` 以 `application/x-www-form-urlencoded` 提交；`source_lang` / `target_lang` 传可读语言名（`中文` / `English`），因为后端会直接写入提示词与落库展示；`enable_human_review` 传 `"true"`/`"false"`。
- `POST /api/tasks/{id}/resume` 提交 JSON `{approved, edited_translation?, comment?}`；`edited_translation` 为整篇译文（前端按段落以空行拼接）。
- `POST /api/tasks/{id}/glossary` 提交 `{glossary:[{term,translation,note}]}`，术语与译文均必填（后端模型约束），前端保存前校验。
- `review.issues` 为字符串数组，评分与分段字段兼容对象/字符串两种形态。
- 导出通过浏览器直接访问 `/api/tasks/{id}/export?fmt=md|docx` 触发下载。
- 部署：`npm run build` 后把 `dist/` 交给后端 `StaticFiles` 单端口托管，接口同源无需改配置。