"""
config.py - 配置加载模块

设计原则：
1. 所有可调参数集中在这里，节点/服务层不写魔法数字（面试时被问「阈值改一下要动几个文件」能答上来）；
2. 用 pathlib 定位 .env，避免 uvicorn 从不同工作目录启动时读不到配置；
3. 保留原有的 MYSQL_* / QWEN_* 变量名，旧 .env 不需要改，新变量全部有默认值。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：以本文件所在目录为准，而不是进程 cwd。
# 因为 uvicorn / pytest / 计划任务启动时 cwd 可能不同，用绝对路径最稳。
BASE_DIR = Path(__file__).resolve().parent

# .env 放在项目根目录；load_dotenv 默认不会覆盖已存在的环境变量，
# 所以 CI/命令行临时改参数时用 `$env:PASS_SCORE=90` 优先级更高。
load_dotenv(BASE_DIR / ".env")

# ============================================
# Qwen 模型配置（审校 Agent）
# ============================================
QWEN_MODEL_NAME = os.getenv("QWEN_MODEL_NAME", "qwen3.7-max")
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL")
# 审校要看完整原文+译文并输出结构化 JSON，给足超时；qwen3.7-max 是思考型模型，
# 一部分 token 会花在 reasoning 上，所以 max_tokens 给到 8192，避免 JSON 被截断。
QWEN_MAX_TOKENS = int(os.getenv("QWEN_MAX_TOKENS", "8192"))
QWEN_TIMEOUT = int(os.getenv("QWEN_TIMEOUT", "180"))

# ============================================
# GLM 模型配置（第二审校，与 Qwen 做双模型投票）
# ============================================
# 为什么用两家不同厂商：审校是「评判」，同模型自评容易共谋（同样的盲区一起盲）；
# 异质模型（不同训练数据/不同对齐方式）在同一份译文上打分，分歧项恰好是"值得人看"的地方。
# 注意：GLM 不填 key 也能跑，review 会自动退化成单模型（不会报错）。
GLM_MODEL_NAME = os.getenv("GLM_MODEL_NAME", "glm-4.6")
GLM_API_KEY = os.getenv("GLM_API_KEY")
# 智谱的 OpenAI 兼容端点（v4），路径里带 /api/paas/v4，不是 /v1
GLM_BASE_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
GLM_MAX_TOKENS = int(os.getenv("GLM_MAX_TOKENS", "4096"))
GLM_TIMEOUT = int(os.getenv("GLM_TIMEOUT", "180"))

# 审校参与投票的模型，逗号分隔；**默认留空 = 不投票**（review 只用下面 MODEL_REVIEW 指定的那个模型）。
# 想开双审校就填 `REVIEW_PROVIDERS=qwen,glm`（两个 provider 都要有 key）。
REVIEW_PROVIDERS = [p.strip().lower() for p in os.getenv("REVIEW_PROVIDERS", "").split(",") if p.strip()]
# 两模型同一维度分差 ≥ 该值就算"分歧"，会写进 disagreements 供人工审核优先看。
REVIEW_VOTE_DIFF = int(os.getenv("REVIEW_VOTE_DIFF", "10"))


def active_review_providers() -> list[str]:
    """
    返回「真正可用」的审校模型列表（有 key 的），按 REVIEW_PROVIDERS 配置顺序，最多 2 个。
    只配一个就单模型（与旧版行为一致）；两个都配就双模型投票。
    """
    available = {
        "qwen": bool(QWEN_API_KEY and QWEN_BASE_URL),
        "glm": bool(GLM_API_KEY),
    }
    picked = [p for p in REVIEW_PROVIDERS if available.get(p)]
    return picked[:2]


def reviewer_model_names() -> list[str]:
    """给 /status、启动日志用：当前真正参与审校的模型名列表。"""
    names = {"qwen": QWEN_MODEL_NAME, "glm": GLM_MODEL_NAME}
    return [names[p] for p in active_review_providers()]


# ============================================
# DeepSeek 模型配置（术语抽取 / 翻译 Agent / 润色 Agent）
# ============================================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
DEEPSEEK_MODEL_NAME = os.getenv("DEEPSEEK_MODEL_NAME", "deepseek-chat")
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
DEEPSEEK_TIMEOUT = int(os.getenv("DEEPSEEK_TIMEOUT", "180"))

# ============================================
# 邮件配置（QQ邮箱 SMTP，旧接口 /task/{id}/send-email 仍在用）
# ============================================
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.qq.com")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# ============================================
# MySQL 数据库配置
# ============================================
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agent_translate")

# 连接池参数：不做成 SQLAlchemy 的原因见 database.py 顶部注释。
# 取连接最多等 10s，等不到就抛错而不是把请求线程挂死（这比「无限等待」可观测性好）。
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))

# ============================================
# 工作流参数（supervisor 状态机与专家节点共用）
# ============================================
# retry_count 的含义是「第几次进入翻译节点」，max_retries 即最多翻译几次。
# 默认 3 次：首译 + 2 次带审校反馈的重译；这是质量与成本的折中。
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
# 审校总分达到该阈值视为通过（review 的 overall 是 4 个维度的加权分）。
PASS_SCORE = float(os.getenv("PASS_SCORE", "80"))
# 分段长度：太长模型容易漏译/术语漂移，太短会丢上下文；300 字左右是教程场景的平衡点。
SEGMENT_MAX_CHARS = int(os.getenv("SEGMENT_MAX_CHARS", "300"))
# 术语表最多保留多少个术语，防止 prompt 过长把翻译预算挤爆。
MAX_GLOSSARY_TERMS = int(os.getenv("MAX_GLOSSARY_TERMS", "20"))
# 4 维评分的加权系数，必须和为 1；权重写死在配置里是为了「总分可解释」，
# 而不是让模型自己拍一个 0-100 分（旧版 review 的坑之一）。
REVIEW_WEIGHTS = {
    "fidelity": float(os.getenv("REVIEW_W_FIDELITY", "0.4")),
    "terminology": float(os.getenv("REVIEW_W_TERMINOLOGY", "0.25")),
    "fluency": float(os.getenv("REVIEW_W_FLUENCY", "0.2")),
    "completeness": float(os.getenv("REVIEW_W_COMPLETENESS", "0.15")),
}

# ============================================
# 服务 / 后台执行参数
# ============================================
# 项目一占用 8000，本项目固定 8001；前端按这个端口对接。
PORT = int(os.getenv("PORT", "8001"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "600"))

# ---- 文档上传（翻译的输入形态之一：直接粘贴文本 or 上传文档）----
# 只放翻译场景真正说得通的 4 种：PDF/Word 是主战场，md/txt 零成本顺手支持。
# 刻意不收 xlsx/pptx/csv：表格行列与幻灯片版式抽取后会丢结构，翻出来是乱序段落，
# 「做了但没做好」比不做更容易在面试里被追问到崩。
ALLOWED_UPLOAD_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")
# 上传体积上限：一个正常待译文档不会超过这个量级；超了基本是误传整本扫描书。
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
# 原文长度上限，与 /api/tasks 的 20000 字符保持一致：文档抽取后同样受这条约束，
# 否则「粘贴文本被拦、上传文件却能绕过去」就是两套标准。
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "20000"))
# 上传落盘目录。抽取文本后原始文件仍保留，便于复现「这次翻译到底吃的什么内容」。
UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))

# SqliteSaver 的落盘位置。放绝对路径，避免 cwd 变化后接上另一个空库（恢复会失效）。
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", str(BASE_DIR / "checkpoints.db"))


# ============================================
# 四节点四模型：按「能力」给每个专家配不同模型（provider:model）
# ============================================
# 为什么不是四个节点都用同一个模型：
#   术语抽取 = 结构化抽取，量大但判错代价低 → 用最便宜的快模型；
#   翻译     = 语言质量是核心指标 → 用擅长生成的主力模型；
#   审校     = "评判"，只调一次，但要求判断力 → 用思考型模型（贵也值）；
#   润色     = 长文改写，要求语感 → 用写作能力强的模型。
# 四者用同一模型当然能跑，但那是"一个模型干四份活"；分开配置才能讲清"为什么是这个模型"。
# 格式 provider:model，provider ∈ deepseek / qwen / glm；只写 provider 时用它自己的默认模型名。
NODE_MODEL_DEFAULTS = {
    "terminology": "glm:glm-4-flash",
    "translate": "deepseek:deepseek-chat",
    "review": "qwen:qwen3.7-max",
    "polish": "glm:glm-4.6",
}

NODE_MODELS = {
    "terminology": os.getenv("MODEL_TERMINOLOGY", NODE_MODEL_DEFAULTS["terminology"]),
    "translate": os.getenv("MODEL_TRANSLATE", NODE_MODEL_DEFAULTS["translate"]),
    "review": os.getenv("MODEL_REVIEW", NODE_MODEL_DEFAULTS["review"]),
    "polish": os.getenv("MODEL_POLISH", NODE_MODEL_DEFAULTS["polish"]),
}

# 配置的 provider 没配 key 时的兜底顺序
PROVIDER_FALLBACK_ORDER = ["deepseek", "qwen", "glm"]

# 每个节点自己的降级候选：即使只有两家 key，也尽量让「四个节点四个不同模型」。
# 顺序即优先级：术语抽取用便宜快模型，润色用偏写作的模型。
NODE_MODEL_FALLBACKS = {
    "terminology": ["qwen:qwen-turbo", "deepseek:deepseek-chat"],
    "translate": ["deepseek:deepseek-chat", "qwen:qwen-plus"],
    "review": ["qwen:qwen3.7-max", "deepseek:deepseek-chat"],
    "polish": ["qwen:qwen-plus", "deepseek:deepseek-chat"],
}

PROVIDER_DEFAULT_MODEL = {
    "deepseek": DEEPSEEK_MODEL_NAME,
    "qwen": QWEN_MODEL_NAME,
    "glm": GLM_MODEL_NAME,
}


def provider_credentials(provider: str) -> dict:
    """取某个 provider 的连接参数；未知 provider 抛 KeyError，让配置错误尽早暴露。"""
    if provider == "deepseek":
        return {
            "api_key": DEEPSEEK_API_KEY,
            "base_url": DEEPSEEK_BASE_URL,
            "max_tokens": DEEPSEEK_MAX_TOKENS,
            "timeout": DEEPSEEK_TIMEOUT,
        }
    if provider == "qwen":
        return {
            "api_key": QWEN_API_KEY,
            "base_url": QWEN_BASE_URL,
            "max_tokens": QWEN_MAX_TOKENS,
            "timeout": QWEN_TIMEOUT,
        }
    if provider == "glm":
        return {
            "api_key": GLM_API_KEY,
            "base_url": GLM_BASE_URL,
            "max_tokens": GLM_MAX_TOKENS,
            "timeout": GLM_TIMEOUT,
        }
    raise KeyError(f"未知的模型 provider: {provider}")


def provider_available(provider: str) -> bool:
    try:
        cred = provider_credentials(provider)
    except KeyError:
        return False
    return bool(cred["api_key"] and cred["base_url"])


def split_model_spec(spec: str) -> tuple[str, str | None]:
    """把 'glm:glm-4-flash' 拆成 (provider, model)；只写 provider 时 model 为 None。"""
    text = (spec or "").strip()
    if ":" in text:
        provider, model = text.split(":", 1)
        return provider.strip().lower(), model.strip() or None
    return text.lower(), None


def resolve_node_model(node: str) -> dict:
    """
    解析某个节点实际要用的模型，并在 provider 缺 key 时降级。

    返回 {"node","provider","model","requested","fallback"}：
      requested 是配置里写的，provider/model 是实际生效的——两者不同就说明发生了降级，
      启动日志会把这件事打印出来（不静默降级，否则"我明明配了四个模型"会变成幻觉）。
    """
    requested = NODE_MODELS.get(node) or NODE_MODEL_DEFAULTS.get(node) or "deepseek:deepseek-chat"
    provider, model = split_model_spec(requested)
    try:
        available = provider_available(provider)
    except Exception:
        available = False
    if not available:
        # 先走该节点自己的降级候选（尽量让四个节点用上四个不同模型），
        # 再走全局 provider 兜底顺序。
        for alt_spec in NODE_MODEL_FALLBACKS.get(node, []):
            alt_provider, alt_model = split_model_spec(alt_spec)
            if alt_provider == provider:
                continue
            if provider_available(alt_provider):
                return {
                    "node": node,
                    "provider": alt_provider,
                    "model": alt_model or PROVIDER_DEFAULT_MODEL.get(alt_provider, ""),
                    "requested": requested,
                    "fallback": True,
                }
        for alt in PROVIDER_FALLBACK_ORDER:
            if provider_available(alt):
                return {
                    "node": node,
                    "provider": alt,
                    "model": PROVIDER_DEFAULT_MODEL[alt],
                    "requested": requested,
                    "fallback": True,
                }
        # 一个都没有：仍然返回原配置，让调用时抛出可读的鉴权错误（比启动就崩更好排障）
        return {
            "node": node,
            "provider": provider,
            "model": model or PROVIDER_DEFAULT_MODEL.get(provider, ""),
            "requested": requested,
            "fallback": False,
        }
    return {
        "node": node,
        "provider": provider,
        "model": model or PROVIDER_DEFAULT_MODEL.get(provider, ""),
        "requested": requested,
        "fallback": False,
    }


def node_model_plan() -> dict:
    """四个节点实际生效的模型：{"terminology": {"provider","model","fallback"}, ...}"""
    return {node: resolve_node_model(node) for node in ("terminology", "translate", "review", "polish")}


def node_model_summary() -> str:
    """一行日志：术语=glm-4-flash · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=glm-4.6"""
    cn = {"terminology": "术语", "translate": "翻译", "review": "审校", "polish": "润色"}
    parts = []
    for node, info in node_model_plan().items():
        mark = "*" if info["fallback"] else ""
        parts.append(f"{cn[node]}={info['model']}{mark}")
    return " · ".join(parts)
