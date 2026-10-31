"""
agent.py - 四个专家节点背后的 LLM 能力

与 v1 的差别（也是这次重构的核心教学点）：
1. 审校不再「正则抠 JSON + 失败默认 50 分」，而是用 Pydantic 结构化输出（ReviewResult），
   解析连续失败会抛 ReviewUnavailableError，由节点标记 review_unavailable，
   交给 supervisor 按「重试一次 → 人工」处理，绝不静默污染评分数据；
2. 翻译按段调用并注入术语表，术语一致性有据可查；
3. 润色只针对审校给出的具体 issues 做定向修改，不再整篇重译；
4. LLM 客户端懒加载：import 本模块不应要求 API key 可用（pytest 才能离线跑状态机测试）。
"""
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText

from langchain_openai import ChatOpenAI

import config
from core.review_vote import merge_reviews, summarize_vote
from models import Glossary, ReviewResult


class ReviewUnavailableError(RuntimeError):
    """审校模型连续两次结构化解析失败：调用方必须降级为 review_unavailable，不能给默认分。"""


# ============================================
# LLM 客户端（懒加载 + 进程级复用）
# ============================================

_LLM_CACHE: dict[tuple, ChatOpenAI] = {}
_LLM_LOCK = threading.Lock()

# 不同节点的采样温度：抽取/评判要稳（低温度），生成/润色要一点灵活性。
NODE_TEMPERATURE = {"terminology": 0.2, "translate": 0.3, "review": 0.2, "polish": 0.3}


def get_llm(provider: str, model: str, temperature: float = 0.2) -> ChatOpenAI:
    """
    按 (provider, model, temperature) 缓存客户端。

    为什么要缓存：每节点一个模型意味着同时存在多个客户端，不缓存会每次调用都新建连接池；
    但缓存 key 必须带上 model，否则"术语用 glm-4-flash、润色用 glm-4.6"会被错误地复用成同一个客户端。
    """
    key = (provider, model, temperature)
    with _LLM_LOCK:
        llm = _LLM_CACHE.get(key)
        if llm is None:
            cred = config.provider_credentials(provider)
            if not (cred["api_key"] and cred["base_url"]):
                raise RuntimeError(
                    f"{provider} 未配置 API Key：请在 .env 里补 {provider.upper()}_API_KEY / {provider.upper()}_BASE_URL"
                )
            llm = ChatOpenAI(
                model=model,
                api_key=cred["api_key"],
                base_url=cred["base_url"],
                temperature=temperature,
                max_tokens=cred["max_tokens"],
                timeout=cred["timeout"],
            )
            _LLM_CACHE[key] = llm
        return llm


def llm_for(node: str) -> ChatOpenAI:
    """取某个专家节点该用的模型（含"provider 没 key 就降级"的逻辑，见 config.resolve_node_model）。"""
    info = config.resolve_node_model(node)
    return get_llm(info["provider"], info["model"], temperature=NODE_TEMPERATURE.get(node, 0.2))


# --- 兼容旧调用点/旧脚本：等价于"用该 provider 的默认模型" ---

def get_deepseek_llm() -> ChatOpenAI:
    return get_llm("deepseek", config.DEEPSEEK_MODEL_NAME, temperature=0.3)


def get_qwen_llm() -> ChatOpenAI:
    return get_llm("qwen", config.QWEN_MODEL_NAME, temperature=0.2)


def get_glm_llm() -> ChatOpenAI:
    return get_llm("glm", config.GLM_MODEL_NAME, temperature=0.2)


# ============================================
# 公共 prompt 片段
# ============================================

def format_glossary(glossary: list[dict] | None) -> str:
    """
    把术语表渲染成 prompt 片段。空表返回空串，避免 prompt 里出现一个空标题。
    术语表是「翻译节点 → 审校节点 → 润色节点」共享的一致性约束，
    所以渲染逻辑只写一处，三个节点用同一份文本。
    """
    if not glossary:
        return ""
    lines = ["## 术语表（必须严格使用以下译法，不得自行改写）"]
    for item in glossary:
        term = str(item.get("term") or "").strip()
        translation = str(item.get("translation") or "").strip()
        if not term or not translation:
            continue
        note = str(item.get("note") or "").strip()
        lines.append(f"- {term} => {translation}" + (f"（{note}）" if note else ""))
    return "\n".join(lines) if len(lines) > 1 else ""


def _style_block(style: str = "", domain: str = "") -> str:
    """风格/领域约束：有才拼，没有就不占 prompt 预算。"""
    parts = []
    if domain.strip():
        parts.append(f"## 领域\n这是「{domain.strip()}」领域的文本，请使用该领域的规范表达。")
    if style.strip():
        parts.append(f"## 风格要求\n{style.strip()}")
    return "\n\n".join(parts)


# ============================================
# 术语抽取（DeepSeek，一次 LLM 调用）
# ============================================

TERMINOLOGY_PROMPT = """你是一位{source_lang}→{target_lang}的专业术语专家。

## 任务
从下面的原文中抽取需要固定译法的术语（专业名词、机构名、产品名、缩略语、领域惯用语）。

## 原文（{source_lang}）
{source_text}

{style_block}

## 输出要求
1. 最多 {max_terms} 个术语，宁缺毋滥；
2. term 必须逐字出现在原文中；
3. translation 给出{target_lang}的固定译法（专有名词保留官方写法）；
4. note 简要说明领域或为什么这样译，没有可留空；
5. 原文没有值得固定的术语时，返回空数组。"""


def extract_terms(
    source_text: str,
    source_lang: str,
    target_lang: str,
    style: str = "",
    domain: str = "",
    max_terms: int | None = None,
) -> list[dict]:
    """
    抽取术语表。失败时直接抛异常，由 terminology 节点决定「降到空表继续」还是中止，
    这里不做静默兜底（兜底策略属于流程决策，不属于模型调用层）。
    """
    max_terms = max_terms or config.MAX_GLOSSARY_TERMS
    prompt = TERMINOLOGY_PROMPT.format(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        style_block=_style_block(style, domain),
        max_terms=max_terms,
    )
    structured = get_deepseek_llm().with_structured_output(Glossary, method="function_calling")
    result = structured.invoke(prompt)
    if result is None:
        raise RuntimeError("术语抽取未返回结构化结果")

    terms: list[dict] = []
    seen: set[str] = set()
    for item in result.terms[:max_terms]:
        term = item.term.strip()
        translation = item.translation.strip()
        if not term or not translation or term in seen:
            continue
        seen.add(term)
        terms.append({"term": term, "translation": translation, "note": item.note.strip()})
    return terms


# ============================================
# 翻译（DeepSeek，按段调用）
# ============================================

TRANSLATE_PROMPT = """你是一位专业的翻译专家，精通{source_lang}和{target_lang}的互译。

## 任务
把下面这段{source_lang}文本翻译成{target_lang}。

{glossary_block}

{style_block}

## 翻译要求
1. 准确传达原意，不增不减，不解释、不总结；
2. 符合{target_lang}表达习惯，自然流畅；
3. 术语严格按术语表处理；
4. 只输出译文本身，不要任何前后缀、引号或说明。

## 原文（{source_lang}）
{source_text}

## 译文（{target_lang}）"""

TRANSLATE_RETRY_PROMPT = """你是一位专业的翻译专家，精通{source_lang}和{target_lang}的互译。

## 任务
根据审校意见，重新翻译下面这段文本（只改有问题的部分，其他保持原样）。

{glossary_block}

{style_block}

## 原文（{source_lang}）
{source_text}

## 上一版译文（{target_lang}）
{previous_translation}

## 审校意见（必须逐条修正）
{review_issues}

## 输出要求
只输出修正后的译文，不要解释修改过程。

## 修正后的译文（{target_lang}）"""


def translate_segment(
    source_text: str,
    source_lang: str,
    target_lang: str,
    glossary: list[dict] | None = None,
    style: str = "",
    domain: str = "",
    review_issues: list[str] | None = None,
    previous_translation: str = "",
) -> str:
    """翻译一段文本。有审校意见/上一版译文时走「定向修正」prompt，否则走首译 prompt。"""
    glossary_block = format_glossary(glossary)
    style_block = _style_block(style, domain)
    issues = [issue.strip() for issue in (review_issues or []) if issue and issue.strip()]

    if issues and previous_translation:
        prompt = TRANSLATE_RETRY_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            glossary_block=glossary_block,
            style_block=style_block,
            source_text=source_text,
            previous_translation=previous_translation,
            review_issues="\n".join(f"- {issue}" for issue in issues),
        )
    else:
        prompt = TRANSLATE_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            glossary_block=glossary_block,
            style_block=style_block,
            source_text=source_text,
        )

    response = llm_for("translate").invoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    content = content.strip()
    if not content:
        raise RuntimeError("翻译模型返回空内容")
    return content


# ============================================
# 审校（Qwen，结构化 4 维评分）
# ============================================

REVIEW_RUBRIC = """## 评分维度（每项 0-100 的整数，必须给出可核对的理由）
- fidelity 忠实度：是否准确传达原文信息，有无增译、漏译、曲解；
- terminology 术语一致性：术语/固定译法是否符合术语表与{target_lang}领域惯例；
- fluency 流畅度：目标语言是否自然地道，有无生硬直译、语法错误；
- completeness 完整性：是否漏句、漏段、漏掉关键限定信息。

## issues 要求
- 每条 issue 必须指出「问题位置 + 问题是什么 + 应该怎么改」，不要写空泛评语；
- 最多 8 条，按严重程度排序；
- 没有问题时返回空数组。

## needs_retranslation
只有在「整段逻辑错误、必须重写」时才为 true；小修小补为 false。"""

REVIEWER_PROMPT = """你是一位严格的翻译审校专家，精通{source_lang}和{target_lang}。

## 任务
审校下面的译文，按 4 个维度打分并给出具体问题。

## 原文（{source_lang}）
{source_text}

## 译文（{target_lang}）
{translation}

{glossary_block}

{rubric}

## 审校要求
1. 先逐句对照原文和译文，再打分；
2. 分数要能由 issues 支撑，不要凭整体印象给分；
3. 术语表里的术语如果译错，terminology 不得高于 60 分。"""


def _build_review(result: ReviewResult) -> dict:
    """把结构化结果合成为 review dict：总分由配置权重计算，保证可解释、可复现。"""
    weights = config.REVIEW_WEIGHTS
    total_weight = sum(weights.values()) or 1.0
    overall = (
        result.fidelity * weights["fidelity"]
        + result.terminology * weights["terminology"]
        + result.fluency * weights["fluency"]
        + result.completeness * weights["completeness"]
    ) / total_weight

    issues: list[str] = []
    for issue in result.issues:
        text = str(issue).strip()
        if text and text not in issues:
            issues.append(text[:500])
    return {
        "fidelity": int(result.fidelity),
        "terminology": int(result.terminology),
        "fluency": int(result.fluency),
        "completeness": int(result.completeness),
        "overall": round(float(overall), 2),
        "issues": issues[:8],
        "needs_retranslation": bool(result.needs_retranslation),
        "unavailable": False,
    }


def _review_once(provider: str, model: str, prompt: str) -> dict:
    """
    单个模型的审校调用（结构化输出 + 一次重试）。
    失败抛 ReviewUnavailableError，由外层决定是降级还是换模型。
    """
    structured = get_llm(provider, model, temperature=NODE_TEMPERATURE["review"]).with_structured_output(
        ReviewResult, method="function_calling"
    )

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            retry_hint = "" if attempt == 1 else "\n\n注意：上一次输出无法解析，请严格按给定 schema 返回结构化结果。"
            result = structured.invoke(prompt + retry_hint)
            if result is None:
                raise ValueError("模型未返回结构化结果")
            if isinstance(result, dict):
                result = ReviewResult.model_validate(result)
            review = _build_review(result)
            review["provider"] = provider
            return review
        except Exception as exc:  # 网络错误/解析错误都重试一次，仍失败则交由上层降级
            last_error = exc
            print(f"[审校Agent:{provider}] 第 {attempt} 次结构化解析失败: {exc}")

    raise ReviewUnavailableError(f"{provider} 结构化输出连续 2 次失败: {last_error}")


def review_translation(
    source_text: str,
    translation: str,
    source_lang: str,
    target_lang: str,
    glossary: list[dict] | None = None,
) -> dict:
    """
    结构化审校（支持单模型 / 双模型投票）。

    解析失败策略：抛 ReviewUnavailableError（每个模型最多尝试 2 次），由 review 节点标记
    review_unavailable=True。v1 的三层正则兜底 + return 50.0 会把「模型解析失败」
    伪装成「翻译质量中等」，做评测时数据全错，这是必须修掉的隐患。

    双模型模式（REVIEW_PROVIDERS 里配了两个且都有 key）：
      - 两个模型**并行**调用，延迟 ≈ 较慢的那个，而不是两者相加；
      - 任一模型失败不影响出分（另一个照常给结果），失败信息记在 failed_providers；
      - 合并策略见 core/review_vote.py（维度取严 + issues 并集 + 分歧项）。
    """
    prompt = REVIEWER_PROMPT.format(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        translation=translation,
        glossary_block=format_glossary(glossary),
        rubric=REVIEW_RUBRIC.format(target_lang=target_lang),
    )

    providers = config.active_review_providers()

    # ---------- 默认：只用 review 节点配置的那个模型（MODEL_REVIEW） ----------
    if len(providers) <= 1:
        info = config.resolve_node_model("review")
        review = _review_once(info["provider"], info["model"], prompt)
        # 补上与投票模式一致的字段，前端/下游不需要判断"有没有投票"
        review.update(vote_mode="single", providers=[info["provider"]], agreement_rate=1.0, disagreements=[])
        return review

    # ---------- 可选：REVIEW_PROVIDERS=qwen,glm 时两个模型并行投票 ----------
    plans = [(p, config.PROVIDER_DEFAULT_MODEL.get(p) or config.QWEN_MODEL_NAME) for p in providers]
    results: list[dict] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(plans)) as pool:
        futures = {pool.submit(_review_once, p, m, prompt): p for p, m in plans}
        for future, provider in ((f, futures[f]) for f in futures):
            try:
                results.append(future.result())
            except Exception as exc:
                errors[provider] = f"{type(exc).__name__}: {exc}"
                print(f"[审校Agent] {provider} 不可用，本次投票跳过: {exc}")

    if not results:
        raise ReviewUnavailableError(f"所有审校模型均失败: {errors}")

    merged = merge_reviews(
        results,
        weights=config.REVIEW_WEIGHTS,
        diff_threshold=config.REVIEW_VOTE_DIFF,
    )
    if errors:
        merged["failed_providers"] = errors
    print(f"[审校Agent] {summarize_vote(merged)}")
    return merged


# ============================================
# 润色（DeepSeek，只改 issues 指向的问题）
# ============================================

POLISHER_PROMPT = """你是一位专业的文字润色师，精通{target_lang}写作。

## 任务
下面这篇译文已经通过基础审校，请**只针对列出的问题**做定向修改。

## 原文（参考，{source_lang}）
{source_text}

## 当前译文（{target_lang}）
{translation}

{glossary_block}

## 必须修改的问题
{issues}

## 润色要求
1. 只改问题相关的句子，其余句子尽量保持原样，避免整篇重写；
2. 不改变原意，不增删信息；
3. 术语保持术语表译法；
4. 只输出修改后的完整译文，不要解释改了哪里。

## 润色后的译文（{target_lang}）"""


def polish_translation(
    source_text: str,
    translation: str,
    issues: list[str],
    source_lang: str,
    target_lang: str,
    glossary: list[dict] | None = None,
) -> str:
    prompt = POLISHER_PROMPT.format(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        translation=translation,
        glossary_block=format_glossary(glossary),
        issues="\n".join(f"{index}. {issue}" for index, issue in enumerate(issues, 1)),
    )
    response = llm_for("polish").invoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    content = content.strip()
    if not content:
        raise RuntimeError("润色模型返回空内容")
    return content


# ============================================
# 邮件发送（旧接口 /task/{id}/send-email 保留）
# ============================================

def send_email(to: str, subject: str, content: str) -> tuple[bool, str]:
    """通过 QQ 邮箱 SMTP 发送纯文本邮件。"""
    print(f"[邮件工具] 发送邮件 -> 收件人: {to}, 主题: {subject}")

    if not config.EMAIL_FROM or not config.EMAIL_PASSWORD:
        return False, "邮件发送失败：未配置发件人邮箱，请在 .env 中填写 EMAIL_FROM 和 EMAIL_PASSWORD"

    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["To"] = to
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_FROM

        smtp = smtplib.SMTP_SSL(config.EMAIL_HOST, port=465)
        smtp.login(config.EMAIL_FROM, config.EMAIL_PASSWORD)
        smtp.sendmail(config.EMAIL_FROM, to, msg.as_string())
        smtp.quit()
        print(f"[邮件工具] 邮件发送成功 -> {to}")
        return True, f"邮件已成功发送到 {to}"
    except smtplib.SMTPAuthenticationError:
        return False, "邮件发送失败：QQ邮箱认证失败，请检查 EMAIL_PASSWORD（需使用授权码，非QQ密码）"
    except Exception as exc:
        print(f"[邮件工具] 邮件发送失败: {exc}")
        return False, f"邮件发送失败: {exc}"