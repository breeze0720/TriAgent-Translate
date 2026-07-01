"""
main.py - FastAPI 主入口
多智能体协作翻译与审校系统

启动: python main.py
地址: http://127.0.0.1:8000
文档: http://127.0.0.1:8000/docs
"""
# 抑制 PyCharm 调试模式下 NumPy 重复导入的警告
import warnings
warnings.filterwarnings("ignore", message=".*The NumPy module was reloaded.*")

import sys
import io
import os
import uuid
import threading
from pathlib import Path
from contextlib import asynccontextmanager

# Force UTF-8 output on Windows to avoid encoding issues
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import config
from models import (
    TranslateRequest,
    SendEmailRequest,
    TranslateResponse,
    TaskDetailResponse,
    TaskListResponse,
    StatusResponse,
)
from database import (
    create_task,
    get_task,
    get_all_tasks,
    get_task_count,
    delete_task,
    test_connection,
)
from workflow import run_translation
from agent import send_email
from document_loader import extract_text

# ============================================
# Config
# ============================================
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt"}

# ============================================
# Application lifespan
# ============================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    print("=" * 60)
    print("  Multi-Agent Translation System Starting...")
    print("=" * 60)

    # Ensure upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    db_ok = test_connection()
    if db_ok:
        print(f"[Startup] [OK] MySQL connected ({config.MYSQL_DATABASE})")
    else:
        print(f"[Startup] [FAIL] MySQL connection failed! Check database config")

    print(f"[Startup] Translator/Polisher model: {config.DEEPSEEK_MODEL_NAME} (DeepSeek)")
    print(f"[Startup] Reviewer model: {config.QWEN_MODEL_NAME} (Qwen)")

    email_ok = bool(config.EMAIL_FROM and config.EMAIL_FROM != "your_email@qq.com")
    print(f"[Startup] Email: {'[OK] configured' if email_ok else '[WARN] not configured'}")

    print("=" * 60)
    print("  Server is ready!")
    print("  API docs: http://127.0.0.1:8000/docs")
    print("=" * 60)

    yield

    print("Server shutting down...")


# ============================================
# Create FastAPI app
# ============================================
app = FastAPI(
    title="Multi-Agent Translation System",
    description="LangGraph + DeepSeek + Qwen based multi-agent translation system with review and polish stages",
    version="1.0.0",
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
# API Endpoints - Translation
# ============================================

@app.post("/translate", response_model=TranslateResponse)
async def api_translate(req: TranslateRequest):
    """
    Submit a text translation task.
    Returns immediately with task_id, workflow runs in background.
    """
    task_id = create_task(req.source_text, req.source_lang, req.target_lang)
    print(f"\n[API] Created translation task #{task_id}")

    thread = threading.Thread(
        target=run_translation,
        args=(task_id, req.source_text, req.source_lang, req.target_lang),
        daemon=True,
    )
    thread.start()

    return TranslateResponse(
        success=True,
        task_id=task_id,
        message=f"Translation task created (ID: {task_id}), processing in background...",
    )


@app.post("/translate-file", response_model=TranslateResponse)
async def api_translate_file(
    file: UploadFile = File(...),
    source_lang: str = Form(default="zh"),
    target_lang: str = Form(default="en"),
):
    """
    Upload a document for translation.
    Supports PDF, Word (.docx), Markdown (.md), and TXT files.
    Extracts text from the document, then runs the full translation workflow.
    """
    # Validate file type
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": f"Unsupported file format '{ext}'. Supported: {list(ALLOWED_EXTENSIONS)}"
            }
        )

    # Save uploaded file to disk
    unique_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = UPLOAD_DIR / unique_name

    try:
        content = await file.read()
        with open(save_path, "wb") as f:
            f.write(content)
        print(f"[API] File saved: {save_path} ({len(content)} bytes)")
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"File save failed: {str(e)}"}
        )

    # Extract text from document
    try:
        source_text = extract_text(str(save_path))
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Document parsing failed: {str(e)}"}
        )

    if not source_text or not source_text.strip():
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Document content is empty, cannot process"}
        )

    print(f"[API] Extracted {len(source_text)} chars from {file.filename}")

    # Create task and run workflow
    task_id = create_task(source_text, source_lang, target_lang)
    print(f"[API] Created file translation task #{task_id}")

    thread = threading.Thread(
        target=run_translation,
        args=(task_id, source_text, source_lang, target_lang),
        daemon=True,
    )
    thread.start()

    return TranslateResponse(
        success=True,
        task_id=task_id,
        message=f"Document '{file.filename}' uploaded ({len(source_text)} chars). Task #{task_id} created.",
    )


# ============================================
# API Endpoints - Task Management
# ============================================

@app.get("/task/{task_id}", response_model=TaskDetailResponse)
async def api_get_task(task_id: int):
    """Get task details including all stage outputs and current status."""
    task = get_task(task_id)
    if task is None:
        return TaskDetailResponse(success=False, message=f"Task #{task_id} not found")

    if task.get("created_at"):
        task["created_at"] = task["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    if task.get("updated_at"):
        task["updated_at"] = task["updated_at"].strftime("%Y-%m-%d %H:%M:%S")

    return TaskDetailResponse(success=True, task=task)


@app.get("/tasks", response_model=TaskListResponse)
async def api_get_tasks(page: int = 1, page_size: int = 20):
    """Get paginated list of all translation tasks (newest first)."""
    tasks = get_all_tasks(page=page, page_size=page_size)
    total = get_task_count()

    for task in tasks:
        if task.get("created_at"):
            task["created_at"] = task["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if task.get("updated_at"):
            task["updated_at"] = task["updated_at"].strftime("%Y-%m-%d %H:%M:%S")

    return TaskListResponse(success=True, tasks=tasks, total=total, page=page, page_size=page_size)


@app.delete("/task/{task_id}")
async def api_delete_task(task_id: int):
    """Delete a task (cascade deletes associated review records)."""
    deleted = delete_task(task_id)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content={"success": False, "message": f"Task #{task_id} not found"},
        )
    return {"success": True, "message": f"Task #{task_id} deleted"}


# ============================================
# API Endpoints - Email
# ============================================

@app.post("/task/{task_id}/send-email")
async def api_send_email(task_id: int, req: SendEmailRequest):
    """Send translation result to specified email address."""
    task = get_task(task_id)
    if task is None:
        return JSONResponse(
            status_code=404,
            content={"success": False, "message": f"Task #{task_id} not found"},
        )

    if task["status"] != "done":
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": f"Task not completed yet (status: {task['status']}), please wait"
            },
        )

    final_result = task.get("final_result") or task.get("polished") or task.get("translation") or ""
    review_score = task.get("review_score", 0)

    email_body = f"""Hello!

Here is the translation result:

========================================
Source ({task['source_lang']})
========================================
{task['source_text'][:2000]}

========================================
Final Result ({task['target_lang']})
========================================
{final_result}

========================================
Quality Info
========================================
Review Score: {review_score}/100
Review Feedback: {task.get('review_feedback', 'N/A')}
Retry Count: {task.get('retry_count', 0)}

========================================

Sent by Multi-Agent Translation Assistant.
"""

    subject = f"Translation Result - {task['source_lang']} to {task['target_lang']}"

    success, message = send_email(req.to, subject, email_body)

    return {"success": success, "message": message}


# ============================================
# API Endpoints - System
# ============================================

@app.get("/status", response_model=StatusResponse)
async def api_status():
    """Get system status: database, models, email, task count"""
    return StatusResponse(
        database="Connected" if test_connection() else "Disconnected",
        translator_model=config.DEEPSEEK_MODEL_NAME,
        reviewer_model=config.QWEN_MODEL_NAME,
        email_configured="Configured" if (config.EMAIL_FROM and config.EMAIL_FROM != "your_email@qq.com") else "Not configured",
        total_tasks=get_task_count(),
    )


# ============================================
# Direct run entry
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
