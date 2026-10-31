"""
main.py - FastAPI 主入口（端口 8001，8000 留给项目一）

两块 API：
1. 新版 /api/*：前端按这个契约开发；任务执行走 ThreadPoolExecutor + LangGraph，
   进度用 SSE（/api/tasks/{id}/events）实时推送，详情/列表读 DB + checkpoint 快照。
2. 旧版 /translate、/task/{id} 等：保留兼容，内部已改为写新 tasks 表，至少保证不 500。

启动：python main.py
文档：http://127.0.0.1:8001/docs
"""
# 抑制 PyCharm 调试模式下 NumPy 重复导入的警告（与原项目一致）
import warnings

warnings.filterwarnings("ignore", message=".*The NumPy module was reloaded.*")

import asyncio
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，中文日志会乱码；强制 UTF-8 输出。
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException as StarletteHTTPException

import agent
import config
import database
import upload_utils
from core.checkpointer import checkpointer_info, get_checkpointer
from core.events import bind_loop, publish, subscribe
from core.runner import execute_task, is_active, recover_interrupted_tasks, resume_task
from document_loader import extract_text
from models import (
    GlossaryTerm,
    GlossaryUpdateRequest,
    ResumeRequest,
    SendEmailRequest,
    StatusResponse,
    TaskDetailResponse,
    TaskListResponse,
    TranslateRequest,
    TranslateResponse,
)
from workflow import STAGE_PROGRESS, get_workflow, normalize_glossary

# ============================================
# 全局配置 / 后台线程池
# ============================================

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(config.UPLOAD_DIR)
# 前端构建产物目录：单端口演示时由 FastAPI 直接托管，避免另开 5173/3000。
DIST_DIR = BASE_DIR / "frontend" / "dist"
# 白名单来自 config，和 upload_utils 共用同一份，避免「校验一层、解析一层」两套标准
ALLOWED_EXTENSIONS = set(config.ALLOWED_UPLOAD_EXTENSIONS)

_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    """后台执行池：限制并发，避免 100 个请求同时打 LLM（旧版裸线程的问题）。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS, thread_name_prefix="translation")
    return _executor


def _submit(fn, *args) -> None:
    _get_executor().submit(fn, *args)


# ============================================
# 序列化小工具
# ============================================

def _dt_str(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _num(value: Any) -> float | None:
    """MySQL DECIMAL 会返回 Decimal，JSON 序列化前统一转 float。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_load(raw: Any, default: Any):
    if not raw:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _title(source_text: str) -> str:
    text = " ".join((source_text or "").split())
    return text[:40] + ("…" if len(text) > 40 else "")


def _task_brief(row: dict) -> dict:
    """GET /api/tasks 的列表项（字段和前端契约一致）。"""
    return {
        "id": int(row["id"]),
        "status": row.get("status") or "pending",
        "stage": row.get("stage") or "init",
        "progress": int(row.get("progress") or 0),
        "score": _num(row.get("score")),
        "retry_count": int(row.get("retry_count") or 0),
        "created_at": _dt_str(row.get("created_at")),
        "title": _title(row.get("source_text") or ""),
    }


# ============================================
# 任务详情（DB + checkpoint 合并）
# ============================================

def _task_detail(task_id: int) -> dict | None:
    """
    详情以 DB 行为主，checkpoint 的 state 作为补充：
    DB 负责「服务重启后也能看到最近一次落库的结果」，checkpoint 负责
    「图刚跑完某个节点、DB 还没写的极短窗口内也能读到最新数据」。
    """
    row = database.get_task(task_id)
    if row is None:
        return None

    review = _json_load(row.get("review_json"), {})
    glossary = database.get_glossary(task_id) or _json_load(row.get("glossary_json"), [])
    detail = {
        "task_id": int(row["id"]),
        "title": _title(row.get("source_text") or ""),
        "source_text": row.get("source_text") or "",
        "source_lang": row.get("source_lang") or "",
        "target_lang": row.get("target_lang") or "",
        "style": row.get("style") or "",
        "domain": row.get("domain") or "",
        "enable_human_review": bool(row.get("enable_human_review")),
        "status": row.get("status") or "pending",
        "stage": row.get("stage") or "init",
        "progress": int(row.get("progress") or 0),
        "score": _num(row.get("score")),
        "retry_count": int(row.get("retry_count") or 0),
        "max_retries": config.MAX_RETRY,
        "version": int(row.get("version") or 0),
        "segments": _json_load(row.get("segments_json"), []),
        "glossary": glossary,
        "translations": _json_load(row.get("translations_json"), []),
        "translation": row.get("translation") or "",
        "polished": row.get("polished") or "",
        "review": review,
        "history": _json_load(row.get("history_json"), []),
        "error": row.get("error_message") or "",
        "created_at": _dt_str(row.get("created_at")),
        "updated_at": _dt_str(row.get("updated_at")),
        "events": [
            {
                "stage": event.get("stage"),
                "status": event.get("status"),
                "message": event.get("message"),
                "version": int(event.get("version") or 0),
                "created_at": _dt_str(event.get("created_at")),
            }
            for event in database.get_events(task_id)
        ],
    }

    try:
        snapshot = get_workflow().get_state({"configurable": {"thread_id": str(task_id)}})
        values = snapshot.values or {}
    except Exception as exc:
        print(f"[API] 读取任务 #{task_id} checkpoint 失败（降级为 DB 数据）: {exc}")
        return detail

    if values:
        # state 里的业务字段更新；status 仍以 DB / 暂停点判断为准。
        for key in (
            "segments", "glossary", "translations", "translation", "polished",
            "review", "history", "retry_count", "version", "stage", "error",
        ):
            if key in values and values[key] not in (None, "", [], {}):
                detail[key] = values[key]
        if values.get("review"):
            detail["score"] = _num(values["review"].get("overall"))
        # checkpoint 里的 stage 比 DB 新一个 superstep 时，进度取两者较大值，避免出现
        # 「stage 已经是 review、progress 还停在 translated」的短暂错位。
        if values.get("stage") and detail["status"] == "running":
            detail["progress"] = max(detail["progress"], STAGE_PROGRESS.get(str(values["stage"]), 0))
        if snapshot.next and "human_review" in snapshot.next:
            detail["status"] = "waiting_human"
            detail["stage"] = "human_review"
            detail["progress"] = max(detail["progress"], 90)

    return detail


def _legacy_task(row: dict) -> dict:
    """旧版 /task/{id} 的返回结构（字段名沿用 v1，避免旧前端报错）。"""
    review = _json_load(row.get("review_json"), {})
    issues = review.get("issues") or []
    feedback = review.get("legacy_feedback") or ("\n".join(str(item) for item in issues) if issues else "")
    final_result = row.get("final_result") or row.get("polished") or row.get("translation") or ""
    return {
        "id": int(row["id"]),
        "source_text": row.get("source_text") or "",
        "source_lang": row.get("source_lang") or "",
        "target_lang": row.get("target_lang") or "",
        "translation": row.get("translation") or "",
        "review_feedback": feedback,
        "review_score": _num(row.get("score")),
        "polished": row.get("polished") or "",
        "final_result": final_result,
        "status": row.get("status") or "pending",
        "retry_count": int(row.get("retry_count") or 0),
        "error_message": row.get("error_message") or "",
        "created_at": _dt_str(row.get("created_at")),
        "updated_at": _dt_str(row.get("updated_at")),
    }


# ============================================
# 应用生命周期
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("  Multi-Agent Translation System (LangGraph supervisor) 启动")
    print("=" * 60)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # SSE 的队列必须由事件循环线程消费，这里把主循环交给事件总线。
    bind_loop(asyncio.get_running_loop())

    db_ok = database.test_connection()
    print(f"[Startup] MySQL {'已连接' if db_ok else '连接失败'}（{config.MYSQL_DATABASE}）")
    try:
        get_checkpointer()
        print(f"[Startup] checkpointer: sqlite {config.CHECKPOINT_DB}")
    except Exception as exc:
        print(f"[Startup] checkpointer 初始化失败: {exc}")

    print(f"[Startup] 四节点模型：{config.node_model_summary()}")
    _plan = config.node_model_plan()
    _fallback = [f"{k}→{v['model']}" for k, v in _plan.items() if v["fallback"]]
    if _fallback:
        print(f"[Startup] ⚠ 以下节点因缺少对应 provider 的 Key 已降级：{', '.join(_fallback)}（.env 里补 key 即自动生效）")
    _reviewers = config.reviewer_model_names()
    if len(_reviewers) >= 2:
        print(f"[Startup] 审校投票：{' + '.join(_reviewers)}（分歧阈值 {config.REVIEW_VOTE_DIFF} 分）")
    else:
        print(f"[Startup] 审校投票：关闭（只用 MODEL_REVIEW={_plan['review']['model']}；要开双审校设 REVIEW_PROVIDERS=qwen,glm）")

    # 关键恢复入口：把上次进程退出时没跑完的任务重新拉起来。
    if db_ok:
        recover_interrupted_tasks(_get_executor())

    print("=" * 60)
    print(f"  Server ready: http://127.0.0.1:{config.PORT}   docs: /docs")
    print("=" * 60)

    yield

    print("Server shutting down...")
    # 不阻塞等待长 LLM 调用：把线程池标记为关闭即可，进程退出由 uvicorn 负责。
    # 这里也不主动关闭 DB 池 / sqlite 连接，避免正在收尾的任务写到一半连接被关掉。
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


app = FastAPI(
    title="Multi-Agent Translation System v2",
    description="LangGraph supervisor + 4 专家 + 有限轮次协商 + 人在环 + checkpoint 断点恢复",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# 文档上传：抽取纯文本（独立成函数，新老两套上传入口共用）
# ============================================

def _discard_upload(path: Path) -> None:
    """解析失败的原始文件是垃圾，删掉，别让 uploads/ 变成问题文件回收站。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _ingest_document(filename: str, content: bytes) -> tuple[str | None, str | None, int]:
    """
    上传文档 → 纯文本。返回 (text, 错误文案, HTTP 状态)；成功时错误为 None、状态 200。

    这里只做编排：四道防线 → 落盘 → 解析 → 清理。判断逻辑全在 upload_utils 里，
    所以这条链路能单测，也能被新旧两个上传接口复用。
    注意解析失败返回 400 而不是 500：文件损坏/加密是用户输入问题，
    返 500 会让前端提示「服务器出错」，用户不知道该换文件。
    """
    ext = Path((filename or "").replace("\\", "/")).suffix.lower()

    reason = upload_utils.check_extension(filename)
    if reason:
        return None, reason, 400
    if not content:
        return None, "文件内容为空（0 字节），请确认文件没有损坏", 400
    # 上传声明大小可能撒谎，落盘后按实际字节数再判一次
    reason = upload_utils.check_size(len(content))
    if reason:
        return None, reason, 413
    reason = upload_utils.zip_bomb_reason(content, ext)
    if reason:
        return None, reason, 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    save_path = UPLOAD_DIR / upload_utils.stored_filename(
        upload_utils.content_sha256(content), filename
    )
    try:
        save_path.write_bytes(content)
    except OSError as exc:
        return None, f"文件保存失败：{exc}", 500

    try:
        text = extract_text(str(save_path))
    except Exception as exc:  # noqa: BLE001 - 解析器可能抛各种库异常，统一转成用户可读原因
        _discard_upload(save_path)
        return None, f"文档解析失败：{exc}", 400

    reason = upload_utils.check_extracted_text(text)
    if reason:
        _discard_upload(save_path)
        return None, reason, 400
    return text.strip(), None, 200


# ============================================
# 新版 API - 任务
# ============================================

@app.post("/api/tasks")
def api_create_task(
    source_text: str = Form(...),
    source_lang: str = Form(default="中文"),
    target_lang: str = Form(default="English"),
    style: str = Form(default=""),
    domain: str = Form(default=""),
    enable_human_review: str = Form(default="false"),
):
    """创建任务并立即返回；真正执行在后台线程池里。"""
    text = (source_text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"success": False, "message": "source_text 不能为空"})
    if len(text) > config.MAX_SOURCE_CHARS:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": f"source_text 超过 {config.MAX_SOURCE_CHARS} 字符上限"},
        )

    human_review = str(enable_human_review).strip().lower() in {"1", "true", "yes", "on"}
    task_id = database.create_task(
        source_text=text,
        source_lang=source_lang,
        target_lang=target_lang,
        style=style,
        domain=domain,
        enable_human_review=human_review,
    )
    print(f"[API] 创建任务 #{task_id}（human_review={human_review}）")
    _submit(execute_task, task_id, "start")
    return {"task_id": task_id, "status": "pending"}


@app.post("/api/tasks/upload")
async def api_create_task_from_file(
    file: UploadFile = File(...),
    source_lang: str = Form(default="中文"),
    target_lang: str = Form(default="English"),
    style: str = Form(default=""),
    domain: str = Form(default=""),
    enable_human_review: str = Form(default="false"),
):
    """
    上传文档建任务（PDF/Word/Markdown/TXT）。

    为什么单独一个端点而不是给 /api/tasks 加可选 file：
    /api/tasks 现在吃 application/x-www-form-urlencoded，一旦声明 File(...)，FastAPI 会按
    multipart 解析，老前端/老脚本的请求体会直接失败。分开两个端点，两种 Content-Type 各自干净。
    建任务之后的所有流程（落库、进线程池、SSE 推进度）和粘贴文本完全共用一条路。
    """
    content = await file.read()
    filename = file.filename or ""
    text, error, status = _ingest_document(filename, content)
    if error:
        return JSONResponse(status_code=status, content={"success": False, "message": error})

    human_review = str(enable_human_review).strip().lower() in {"1", "true", "yes", "on"}
    safe_name = upload_utils.sanitize_filename(filename)
    task_id = database.create_task(
        source_text=text,
        source_lang=source_lang,
        target_lang=target_lang,
        style=style,
        domain=domain,
        enable_human_review=human_review,
    )
    print(f"[API] 上传建任务 #{task_id}（{safe_name}，{len(text)} 字符，human_review={human_review}）")
    _submit(execute_task, task_id, "start")
    return {
        "task_id": task_id,
        "status": "pending",
        "source_file": safe_name,
        "source_chars": len(text),
    }


@app.get("/api/tasks")
def api_list_tasks(limit: int = 20):
    limit = max(1, min(int(limit or 20), 100))
    return {"tasks": [_task_brief(row) for row in database.list_tasks(limit)]}


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    detail = _task_detail(task_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})
    return detail


@app.get("/api/tasks/{task_id}/events")
async def api_task_events(task_id: int):
    """
    SSE 进度流：先推一条当前快照（重连也能立即看到状态），再推实时增量。
    任务进入终态后主动关闭流，前端 EventSource 会自然结束。
    """
    detail = _task_detail(task_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})

    initial = {
        "task_id": task_id,
        "stage": detail["stage"],
        "status": detail["status"],
        "progress": detail["progress"],
        "message": "当前状态快照",
        "version": detail["version"],
    }
    if detail.get("review"):
        initial["review"] = detail["review"]

    async def event_generator():
        yield ServerSentEvent(data=json.dumps(initial, ensure_ascii=False, default=str), event="progress")
        if detail["status"] in ("done", "failed"):
            return
        async for event in subscribe(task_id):
            yield ServerSentEvent(data=json.dumps(event, ensure_ascii=False, default=str), event="progress")
            if event.get("status") in ("done", "failed"):
                return

    return EventSourceResponse(event_generator())


@app.post("/api/tasks/{task_id}/resume")
def api_resume_task(task_id: int, req: ResumeRequest):
    row = database.get_task(task_id)
    if row is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})
    if is_active(task_id):
        return JSONResponse(status_code=409, content={"success": False, "message": "任务正在执行，请稍候"})
    if row.get("status") != "waiting_human":
        return JSONResponse(
            status_code=409,
            content={"success": False, "message": f"任务当前状态为 {row.get('status')}，不在等待人工审核"},
        )

    decision: dict = {"approved": bool(req.approved)}
    if req.edited_translation and req.edited_translation.strip():
        decision["edited_translation"] = req.edited_translation.strip()
    if req.comment:
        decision["comment"] = req.comment.strip()

    _submit(resume_task, task_id, decision)
    return {"success": True, "status": "running"}


@app.post("/api/tasks/{task_id}/retry")
def api_retry_task(task_id: int):
    row = database.get_task(task_id)
    if row is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})
    if is_active(task_id):
        return JSONResponse(status_code=409, content={"success": False, "message": "任务正在执行，不能重试"})
    database.reset_task_for_retry(task_id)
    _submit(execute_task, task_id, "restart")
    return {"success": True, "status": "pending"}


@app.post("/api/tasks/{task_id}/glossary")
def api_update_glossary(task_id: int, req: GlossaryUpdateRequest):
    """
    人工编辑术语表：整表替换写库；如果任务暂停/已结束（没有线程在跑），
    同步写进 checkpoint 的 state，后续重译或前端展示立即生效。
    """
    row = database.get_task(task_id)
    if row is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})
    # 执行中的任务不要改术语表：runner 每个 superstep 会用 state 里的术语表覆盖 DB，
    # 此时写库会被下一拍覆盖，反而制造“改了不生效”的错觉。
    if is_active(task_id):
        return JSONResponse(
            status_code=409,
            content={"success": False, "message": "任务正在执行，请在人工审核暂停或任务结束后再编辑术语表"},
        )

    glossary = normalize_glossary([item.model_dump() for item in req.glossary])
    database.replace_glossary(task_id, glossary)

    try:
        graph = get_workflow()
        graph_config = {"configurable": {"thread_id": str(task_id)}}
        snapshot = graph.get_state(graph_config)
        if snapshot.values:
            new_version = int(snapshot.values.get("version") or 0) + 1
            graph.update_state(
                graph_config,
                {
                    "glossary": glossary,
                    "version": new_version,
                    "history": [
                        {
                            "stage": "glossary",
                            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "note": f"人工编辑术语表：{len(glossary)} 个术语",
                        }
                    ],
                },
            )
    except Exception as exc:
        print(f"[API] 任务 #{task_id} 术语表写入 checkpoint 失败（DB 已更新）: {exc}")

    publish(
        task_id,
        {
            "task_id": task_id,
            "stage": "glossary",
            "status": row.get("status") or "pending",
            "progress": int(row.get("progress") or 0),
            "message": f"术语表已更新（{len(glossary)} 个术语）",
        },
    )
    return {"success": True, "glossary": glossary, "count": len(glossary)}


# ============================================
# 新版 API - 导出 / 删除 / 健康检查
# ============================================

def _render_markdown(detail: dict) -> str:
    review = detail.get("review") or {}
    lines = [
        f"# 翻译任务 #{detail['task_id']}",
        "",
        f"- 语言：{detail['source_lang']} → {detail['target_lang']}",
        f"- 状态：{detail['status']}（阶段 {detail['stage']}）",
        f"- 审校总分：{detail.get('score') if detail.get('score') is not None else 'N/A'}",
        f"- 翻译轮次：{detail['retry_count']}/{detail['max_retries']}",
        f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 原文",
        "",
        detail.get("source_text") or "",
        "",
        "## 最终译文",
        "",
        detail.get("translation") or "",
        "",
        "## 术语表",
        "",
    ]
    glossary = detail.get("glossary") or []
    if glossary:
        lines += ["| 术语 | 译法 | 备注 |", "| --- | --- | --- |"]
        lines += [f"| {item.get('term', '')} | {item.get('translation', '')} | {item.get('note', '')} |" for item in glossary]
    else:
        lines.append("（无）")
    lines += ["", "## 审校明细", ""]
    if review:
        lines += [
            f"- 忠实度：{review.get('fidelity')}",
            f"- 术语一致性：{review.get('terminology')}",
            f"- 流畅度：{review.get('fluency')}",
            f"- 完整性：{review.get('completeness')}",
            f"- 加权总分：{review.get('overall')}",
        ]
        issues = review.get("issues") or []
        if issues:
            lines.append("")
            lines.append("### 问题清单")
            lines += [f"{index}. {issue}" for index, issue in enumerate(issues, 1)]
    else:
        lines.append("（无审校数据）")
    if detail.get("error"):
        lines += ["", "## 失败原因", "", detail["error"]]
    return "\n".join(lines).strip() + "\n"


def _render_docx(detail: dict) -> io.BytesIO:
    """用 python-docx 生成导出文件；结构尽量与 Markdown 版一致。"""
    from docx import Document

    document = Document()
    document.add_heading(f"翻译任务 #{detail['task_id']}", level=0)
    document.add_paragraph(f"语言：{detail['source_lang']} → {detail['target_lang']}")
    document.add_paragraph(f"状态：{detail['status']}（阶段 {detail['stage']}）")
    document.add_paragraph(f"审校总分：{detail.get('score') if detail.get('score') is not None else 'N/A'}")
    document.add_paragraph(f"翻译轮次：{detail['retry_count']}/{detail['max_retries']}")

    document.add_heading("原文", level=1)
    document.add_paragraph(detail.get("source_text") or "")
    document.add_heading("最终译文", level=1)
    document.add_paragraph(detail.get("translation") or "")

    document.add_heading("术语表", level=1)
    glossary = detail.get("glossary") or []
    if glossary:
        table = document.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ("术语", "译法", "备注")):
            cell.text = title
        for item in glossary:
            row_cells = table.add_row().cells
            row_cells[0].text = str(item.get("term") or "")
            row_cells[1].text = str(item.get("translation") or "")
            row_cells[2].text = str(item.get("note") or "")
    else:
        document.add_paragraph("（无）")

    review = detail.get("review") or {}
    document.add_heading("审校明细", level=1)
    if review:
        for label, key in (("忠实度", "fidelity"), ("术语一致性", "terminology"), ("流畅度", "fluency"), ("完整性", "completeness"), ("加权总分", "overall")):
            document.add_paragraph(f"{label}：{review.get(key)}", style="List Bullet")
        for index, issue in enumerate(review.get("issues") or [], 1):
            document.add_paragraph(f"{index}. {issue}", style="List Number")
    else:
        document.add_paragraph("（无审校数据）")

    if detail.get("error"):
        document.add_heading("失败原因", level=1)
        document.add_paragraph(detail["error"])

    buffer = io.BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


@app.get("/api/tasks/{task_id}/export")
def api_export_task(task_id: int, fmt: str = "md"):
    detail = _task_detail(task_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})

    fmt = (fmt or "md").lower()
    if fmt == "md":
        content = _render_markdown(detail)
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="task_{task_id}.md"'},
        )
    if fmt == "docx":
        buffer = _render_docx(detail)
        return Response(
            content=buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="task_{task_id}.docx"'},
        )
    return JSONResponse(status_code=400, content={"success": False, "message": "fmt 只支持 md 或 docx"})


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: int):
    if is_active(task_id):
        return JSONResponse(status_code=409, content={"success": False, "message": "任务正在执行，不能删除"})
    if not database.delete_task(task_id):
        return JSONResponse(status_code=404, content={"success": False, "message": f"任务 #{task_id} 不存在"})
    # 检查点单独清理：SQLite 里没有外键级联。
    from core.checkpointer import delete_thread

    delete_thread(task_id)
    return {"success": True}


@app.get("/api/health")
def api_health():
    db_ok = database.test_connection()
    try:
        get_checkpointer()
    except Exception as exc:
        print(f"[API] checkpointer 健康检查失败: {exc}")
    return {
        "ok": bool(db_ok),
        "db": "connected" if db_ok else "disconnected",
        "checkpointer": checkpointer_info(),
        # 前端据此渲染上传区提示，避免把格式/上限写死在 JS 里
        "upload": {
            "formats": list(config.ALLOWED_UPLOAD_EXTENSIONS),
            "max_mb": config.MAX_UPLOAD_MB,
            "max_chars": config.MAX_SOURCE_CHARS,
        },
    }


# ============================================
# 旧版 API（兼容层，保证旧前端/旧脚本不 500）
# ============================================

@app.post("/translate", response_model=TranslateResponse)
def api_translate(req: TranslateRequest):
    task_id = database.create_task(
        source_text=req.source_text,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
    )
    _submit(execute_task, task_id, "start")
    return TranslateResponse(
        success=True,
        task_id=task_id,
        message=f"Translation task created (ID: {task_id}), processing in background...",
    )


@app.post("/translate-file", response_model=TranslateResponse)
async def api_translate_file(
    file: UploadFile = File(...),
    source_lang: str = Form(default="中文"),
    target_lang: str = Form(default="English"),
):
    """旧版文件翻译接口（兼容层）：校验与解析复用新版 _ingest_document，只是返回体不同。"""
    content = await file.read()
    source_text, error, status = _ingest_document(file.filename or "", content)
    if error:
        return JSONResponse(status_code=status, content={"success": False, "message": error})

    task_id = database.create_task(source_text=source_text, source_lang=source_lang, target_lang=target_lang)
    _submit(execute_task, task_id, "start")
    return TranslateResponse(
        success=True,
        task_id=task_id,
        message=f"Document '{upload_utils.sanitize_filename(file.filename or '')}' uploaded"
        f" ({len(source_text)} chars). Task #{task_id} created.",
    )


@app.get("/task/{task_id}", response_model=TaskDetailResponse)
def api_get_task_legacy(task_id: int):
    row = database.get_task(task_id)
    if row is None:
        return TaskDetailResponse(success=False, message=f"Task #{task_id} not found")
    return TaskDetailResponse(success=True, task=_legacy_task(row))


@app.get("/tasks", response_model=TaskListResponse)
def api_get_tasks_legacy(page: int = 1, page_size: int = 20):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    tasks = [_legacy_task(row) for row in database.get_all_tasks(page=page, page_size=page_size)]
    return TaskListResponse(
        success=True,
        tasks=tasks,
        total=database.get_task_count(),
        page=page,
        page_size=page_size,
    )


@app.delete("/task/{task_id}")
def api_delete_task_legacy(task_id: int):
    if not database.delete_task(task_id):
        return JSONResponse(status_code=404, content={"success": False, "message": f"Task #{task_id} not found"})
    from core.checkpointer import delete_thread

    delete_thread(task_id)
    return {"success": True, "message": f"Task #{task_id} deleted"}


@app.post("/task/{task_id}/send-email")
def api_send_email(task_id: int, req: SendEmailRequest):
    row = database.get_task(task_id)
    if row is None:
        return JSONResponse(status_code=404, content={"success": False, "message": f"Task #{task_id} not found"})
    if row.get("status") != "done":
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": f"Task not completed yet (status: {row.get('status')})"},
        )

    task = _legacy_task(row)
    email_body = f"""Hello!

Here is the translation result:

========================================
Source ({task['source_lang']})
========================================
{task['source_text'][:2000]}

========================================
Final Result ({task['target_lang']})
========================================
{task['final_result']}

========================================
Quality Info
========================================
Review Score: {task['review_score']}
Review Feedback: {task['review_feedback']}
Retry Count: {task['retry_count']}

Sent by Multi-Agent Translation Assistant.
"""
    subject = f"Translation Result - {task['source_lang']} to {task['target_lang']}"
    success, message = agent.send_email(req.to, subject, email_body)
    return {"success": success, "message": message}


@app.get("/status", response_model=StatusResponse)
def api_status():
    plan = config.node_model_plan()
    reviewers = config.reviewer_model_names()
    review_model = plan["review"]["model"]
    if len(reviewers) >= 2:
        review_model = f"{review_model} + {reviewers[1]}"
    return StatusResponse(
        database="Connected" if database.test_connection() else "Disconnected",
        translator_model=plan["translate"]["model"],
        reviewer_model=review_model,
        email_configured="Configured" if (config.EMAIL_FROM and config.EMAIL_FROM != "your_email@qq.com") else "Not configured",
        total_tasks=database.get_task_count(),
        node_models={node: info["model"] for node, info in plan.items()},
    )


# ============================================
# 前端静态资源（单端口演示）
# ============================================

class _SPAStaticFiles(StaticFiles):
    """
    Vite 构建产物使用 history 路由：直接 StaticFiles(html=True) 只会在目录路径返回
    index.html，刷新 /app/tasks/2 这类前端路由会 404。这里做一次「404 回退 index.html」，
    保证 SPA 刷新不白屏。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = await super().get_response("index.html", scope)
        if response.status_code == 404:
            response = await super().get_response("index.html", scope)
        return response


# 关键顺序：mount 必须放在所有 /api 路由注册之后，否则 /app 前缀会先匹配、吞掉 API 路由。
if DIST_DIR.exists():
    # Vite 默认把 js/css 输出到绝对路径 /assets/...，所以单独挂载 /assets，
    # 否则 index.html 里的 <script src="/assets/..."> 会打到未注册的路径。
    if (DIST_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="ui-assets")
    app.mount("/app", _SPAStaticFiles(directory=str(DIST_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def ui_root():
        return RedirectResponse(url="/app/")
else:
    @app.get("/", include_in_schema=False)
    def ui_missing():
        return {"service": "multi-agent translation", "docs": "/docs", "ui": "frontend/dist 未构建"}


# ============================================
# 直接启动
# ============================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.PORT)