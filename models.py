"""
models.py - Pydantic 数据模型
定义 API 请求和响应的数据结构。
"""
from pydantic import BaseModel, Field


# ============================================
# 请求模型
# ============================================

class TranslateRequest(BaseModel):
    """翻译请求"""
    source_text: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="待翻译的原文",
        examples=["人工智能正在改变世界的方方面面。"]
    )
    source_lang: str = Field(
        default="zh",
        description="源语言代码",
        examples=["zh", "en", "ja"]
    )
    target_lang: str = Field(
        default="en",
        description="目标语言代码",
        examples=["en", "zh", "ja"]
    )


class SendEmailRequest(BaseModel):
    """发送邮件请求"""
    to: str = Field(
        ...,
        description="收件人邮箱地址",
        examples=["user@example.com"]
    )


# ============================================
# 响应模型
# ============================================

class TranslateResponse(BaseModel):
    """翻译任务响应"""
    success: bool
    task_id: int
    message: str


class TaskDetailResponse(BaseModel):
    """任务详情响应"""
    success: bool = True
    task: dict | None = None
    message: str = ""


class TaskListResponse(BaseModel):
    """任务列表响应"""
    success: bool = True
    tasks: list[dict] = []
    total: int = 0
    page: int = 1
    page_size: int = 20


class StatusResponse(BaseModel):
    """系统状态响应"""
    database: str  # "已连接" 或 "未连接"
    translator_model: str
    reviewer_model: str
    email_configured: str  # "已配置" 或 "未配置"
    total_tasks: int
