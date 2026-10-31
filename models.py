"""
models.py - Pydantic 数据模型

这里放三类模型：
1. 结构化输出模型（Glossary / ReviewResult）：给 LLM 的 with_structured_output 用，
   好处是「模型没按格式返回」会在解析层直接暴露，而不是被正则兜底成 50 分（旧版隐患）；
2. 新版 /api/* 的请求体（ResumeRequest / GlossaryUpdateRequest）；
3. 旧版接口的响应模型（TranslateResponse 等），保留是为了旧前端/旧脚本不 500。
"""
from pydantic import BaseModel, Field


# ============================================
# LLM 结构化输出模型
# ============================================

class GlossaryTerm(BaseModel):
    """一个术语条目：原文术语 + 固定译法 + 备注（为什么这么译）"""
    term: str = Field(..., min_length=1, max_length=200, description="源语言术语")
    translation: str = Field(..., min_length=1, max_length=200, description="目标语言固定译法")
    note: str = Field(default="", max_length=500, description="备注：领域/出处/为什么这样译")


class Glossary(BaseModel):
    """术语抽取的结构化输出"""
    terms: list[GlossaryTerm] = Field(default_factory=list, description="领域术语表，没有术语时返回空数组")


class ReviewResult(BaseModel):
    """
    审校的结构化输出（4 维评分）。
    维度分开打分是刻意的：总分由代码按权重合成，模型只负责「给出可核对的证据」，
    这样「为什么 82 分」能拆开解释，而不是一句「模型觉得还行」。
    """
    fidelity: int = Field(..., ge=0, le=100, description="忠实度：是否准确传达原文，不增不减")
    terminology: int = Field(..., ge=0, le=100, description="术语一致性：术语/固定译法是否符合术语表与领域惯例")
    fluency: int = Field(..., ge=0, le=100, description="流畅度：目标语言是否自然、地道")
    completeness: int = Field(..., ge=0, le=100, description="完整性：是否漏译、漏句、漏信息")
    issues: list[str] = Field(default_factory=list, description="具体问题列表，每条指出问题位置与修改方向")
    needs_retranslation: bool = Field(default=False, description="是否存在必须整段重译的严重问题")


# ============================================
# 新版 /api/* 请求模型
# ============================================

class ResumeRequest(BaseModel):
    """人工审核结果：通过 / 打回，可附带人工修改后的译文"""
    approved: bool = Field(..., description="是否通过审校")
    edited_translation: str | None = Field(default=None, description="人工修改后的整篇译文；不填则沿用当前译文")
    comment: str | None = Field(default=None, max_length=2000, description="人工审核意见")


class GlossaryUpdateRequest(BaseModel):
    """人工编辑术语表：整表替换，前端拿到的是完整列表"""
    glossary: list[GlossaryTerm] = Field(default_factory=list)


# ============================================
# 旧版接口模型（保留兼容）
# ============================================

class TranslateRequest(BaseModel):
    """旧版翻译请求（POST /translate）"""
    source_text: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description="待翻译的原文",
        examples=["人工智能正在改变世界的方方面面。"],
    )
    source_lang: str = Field(default="zh", description="源语言代码")
    target_lang: str = Field(default="en", description="目标语言代码")


class SendEmailRequest(BaseModel):
    """旧版发送邮件请求"""
    to: str = Field(..., description="收件人邮箱地址", examples=["user@example.com"])


class TranslateResponse(BaseModel):
    """旧版翻译任务响应"""
    success: bool
    task_id: int
    message: str


class TaskDetailResponse(BaseModel):
    """旧版任务详情响应"""
    success: bool = True
    task: dict | None = None
    message: str = ""


class TaskListResponse(BaseModel):
    """旧版任务列表响应"""
    success: bool = True
    tasks: list[dict] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class StatusResponse(BaseModel):
    """旧版系统状态响应（node_models 是新增的四节点模型分工，旧前端忽略即可）"""
    database: str
    translator_model: str
    reviewer_model: str
    email_configured: str
    total_tasks: int
    node_models: dict[str, str] = Field(default_factory=dict)