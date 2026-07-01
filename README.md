# 多智能体协作翻译与审校系统

基于 **LangGraph 多智能体工作流** 的翻译系统。由三个 AI Agent 协作完成翻译任务：翻译 Agent（DeepSeek）-> 审校 Agent（Qwen）-> 润色 Agent（DeepSeek）。审校不通过自动回退重译（最多 3 次）。

**技术栈**: FastAPI + LangGraph + DeepSeek + Qwen (通义千问) + MySQL + Vue 3 (前端)

## 功能特性

- **三 Agent 协作**：翻译 -> 审校 -> 润色，LangGraph 编排工作流
- **自动回退重译**：审校评分 < 80 分自动携带反馈重译，最多 3 次
- **质量评分**：Qwen 模型对每次翻译进行 0-100 评分并给出详细修改建议
- **审校历史追溯**：每次审校记录单独存储，可复盘翻译质量演变
- **文档翻译**：支持上传 PDF/Word/Markdown/TXT，自动提取文本后翻译
- **邮件发送**：翻译完成后可将结果通过 QQ 邮箱发送给指定收件人
- **双模型架构**：DeepSeek 负责翻译和润色（中文能力强），Qwen 负责审校（推理能力强）

## 项目结构

```
Agent Project2 test/
├── main.py              # FastAPI 主入口，定义所有 API 接口
├── agent.py             # 三个 Agent 定义（翻译/审校/润色）+ 邮件工具
├── workflow.py          # LangGraph 工作流编排（StateGraph）
├── database.py          # MySQL 数据库操作（CRUD）
├── models.py            # Pydantic 请求/响应数据模型
├── document_loader.py   # 文档文本提取（PDF/Word/Markdown/TXT）
├── config.py            # 从 .env 加载双模型 + 邮件 + 数据库配置
├── init_db.py           # MySQL 数据库初始化脚本
├── requirements.txt     # Python 依赖
├── frontend/            # Vue 3 前端（Vite 构建）
│   ├── index.html
│   └── src/
│       └── main.js
└── uploads/             # 用户上传的文件（不上传 Git）
```

## 本地运行

### 前置条件

- **Python 3.12+**
- **MySQL 8.0+**（需提前安装并运行）
- **DeepSeek API Key**（注册地址：[platform.deepseek.com](https://platform.deepseek.com)）
- **Qwen (通义千问) API Key**（注册地址：[dashscope.aliyun.com](https://dashscope.aliyun.com)）
- **Node.js 18+**（前端开发需要）
- QQ 邮箱授权码（可选，用于邮件发送功能）

### 安装步骤

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd "Agent Project2 test"

# 2. 创建虚拟环境
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# 3. 安装 Python 依赖
pip install -r requirements.txt

# 4. 配置环境变量
# 复制下面的内容，创建 .env 文件并填入你的信息
```

### 配置 .env 文件

在项目根目录创建 `.env` 文件，内容如下：

```ini
# Qwen 模型（负责审校 Agent，必填）
QWEN_MODEL_NAME=qwen3.7-max
QWEN_API_KEY=sk-your-qwen-api-key-here
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# DeepSeek 模型（负责翻译和润色 Agent，必填）
DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_NAME=deepseek-chat

# 邮件发送配置（QQ邮箱 SMTP，可选）
EMAIL_HOST=smtp.qq.com
EMAIL_FROM=your_email@qq.com
EMAIL_PASSWORD=your_qq_authorization_code

# MySQL 数据库配置（必填，需提前创建好数据库）
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=agent_translate
```

> 安全提示：`.env` 文件已在 `.gitignore` 中排除，不会被上传到 GitHub。请勿将 API Key 硬编码在代码中。

### 初始化数据库

确保 MySQL 服务已运行，然后执行初始化脚本：

```bash
python init_db.py
```

这会自动创建 `agent_translate` 数据库和 `translation_tasks`、`review_records` 两张表。脚本通过 `.env` 文件读取数据库连接信息。

### 启动后端服务

```bash
python main.py
```

后端启动后访问：
- API 文档（Swagger）：http://127.0.0.1:8000/docs

### 启动前端（可选）

```bash
cd frontend
npm install
npm run dev
```

前端启动后访问 http://localhost:5173（默认 Vite 端口）。

### 使用指南

1. **文本翻译**：调用 `POST /translate` 接口，传入原文、源语言和目标语言
2. **文档翻译**：调用 `POST /translate-file` 接口，上传文档文件
3. **查看任务**：`GET /task/{task_id}` 查看翻译进度和各阶段输出
4. **任务列表**：`GET /tasks` 查看所有历史翻译任务
5. **发送邮件**：翻译完成后调用 `POST /task/{task_id}/send-email` 发送结果

## 工作流说明

```
START
  |
  v
翻译 Agent (DeepSeek)    <--------------+
  |                                      |
  v                                      |
审校 Agent (Qwen)        --- 评分 < 80 --+  (最多 3 次)
  |
  | 评分 >= 80
  v
润色 Agent (DeepSeek)
  |
  v
 END
```

- **翻译 Agent**：DeepSeek 负责初次翻译和根据审校反馈重译
- **审校 Agent**：Qwen 对译文评分（0-100）并给出具体修改建议
- **润色 Agent**：DeepSeek 对审校通过的译文进行文学性润色
- **回退机制**：审校评分 < 80 分时自动返回翻译阶段，最多重试 3 次

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/translate` | 提交文本翻译任务 |
| POST | `/translate-file` | 上传文档进行翻译 |
| GET | `/task/{task_id}` | 查询任务详情（含各阶段输出） |
| GET | `/tasks` | 分页查询任务列表 |
| DELETE | `/task/{task_id}` | 删除任务（级联删除审校记录） |
| POST | `/task/{task_id}/send-email` | 发送翻译结果到指定邮箱 |
| GET | `/status` | 查看系统状态 |
| GET | `/docs` | Swagger API 文档 |

## 注意事项

- 翻译和审校需要调用两次不同的 API（DeepSeek + Qwen），请确保两个 API Key 都有效
- QQ 邮箱需要使用**授权码**而非 QQ 密码，获取方式：QQ 邮箱 -> 设置 -> 账户 -> POP3/SMTP 服务 -> 生成授权码
- MySQL 需提前安装并创建数据库（`init_db.py` 会自动建表）
- 翻译任务是异步执行的，`/translate` 接口立即返回 task_id，工作流在后台线程中运行
- 前端为 Vue 3 + Vite 项目，需单独安装依赖和启动
