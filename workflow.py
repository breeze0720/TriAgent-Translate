"""
workflow.py - LangGraph「主控 + 专家」多智能体翻译工作流

拓扑（supervisor 模式）：
                    ┌──────────────┐
                    │  supervisor  │  ← 只做状态机决策，不调 LLM、不写库
                    └──────┬───────┘
        ┌────────┬─────────┼─────────┬────────────┬──────────────┐
        ▼        ▼         ▼         ▼            ▼              ▼
  terminology  translate  review   polish    human_review    finalize
        └────────┴─────────┴─────────┴────────────┴──────────────┘
                          （每个专家节点都回到 supervisor）

设计要点（对照 v1 的两个真 bug）：
1. 重试计数：translate 节点「每次进入就无条件 +1」，不再依赖 review_feedback 是否为空。
   v1 的分支结构导致首轮不自增，审校一直低分时 can_retry 条件永远成立 → 死循环。
2. 条件边纯函数：route_after_supervisor / decide_after_review 只读 state、只返回节点名，
   不写数据库、不改 state。所有落库由 core.runner 在图执行外侧统一做（幂等 upsert），
   这样 checkpointer 重放节点时不会重复写库。
3. 累积型 channel：history 用 Annotated[list[dict], operator.add]，每版译文/评分都留痕；
   version 是覆盖型 channel，每次节点写入 +1，前端可用它做增量刷新。
4. 人在环：human_review 节点用 interrupt() 暂停，/resume 用 Command(resume=...) 继续；
   暂停点也落在 checkpoint 里，所以「人工审核到一半进程挂了」同样能恢复。
"""
import operator
import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import agent
import config


# ============================================
# 状态定义
# ============================================

class TranslationState(TypedDict, total=False):
    """工作流状态。total=False 表示节点只需返回自己修改的字段（局部更新）。"""

    # ---- 入参（API 层写入，之后只读）----
    task_id: int
    source_lang: str
    target_lang: str
    source_text: str
    style: str
    domain: str
    enable_human_review: bool

    # ---- 业务数据 ----
    segments: list[dict]        # [{"index": 0, "text": "..."}]
    glossary: list[dict]        # [{"term", "translation", "note"}]
    translations: list[dict]    # [{"index", "source", "text", "attempt"}]
    translation: str            # 当前（最终）译文
    polished: str               # 润色节点输出
    review: dict                # 4 维评分 + overall + issues + needs_retranslation
    review_unavailable: bool    # 审校结构化输出不可用（不允许静默给 50 分）
    human_decision: dict | None  # {approved, edited, comment, at}

    # ---- 运行控制 ----
    retry_count: int
    max_retries: int
    status: str                 # pending/running/waiting_human/done/failed
    stage: str                  # init/terminology/translate/review/polish/human_review/final
    version: int                # 覆盖型：每次节点写入 +1
    error: str

    # ---- 累积型 channel：operator.add 保证每版记录都追加而不是覆盖 ----
    history: Annotated[list[dict], operator.add]


# 阶段 → 进度百分比。集中在一处，SSE、DB、前端用同一套口径。
STAGE_PROGRESS: dict[str, int] = {
    "init": 1,
    "terminology": 10,
    "terminology_done": 15,
    "translate": 30,
    "translated": 45,
    "review": 60,
    "reviewed": 70,
    "polish": 80,
    "polished": 88,
    "human_review": 90,
    "human_resolved": 95,
    "finalize": 97,
    "final": 100,
}


# ============================================
# 纯工具函数（可单测，不依赖 LLM / DB）
# ============================================

def _now_iso() -> str:
    """带时区的本地时间：history 里每条都记录，方便前端按时间轴展示。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _history(stage: str, note: str) -> dict:
    return {"stage": stage, "at": _now_iso(), "note": note}


def split_segments(source_text: str, max_chars: int | None = None) -> list[dict]:
    """
    把原文切成适合逐段翻译的小块。

    为什么按句子装箱而不是硬切字符：硬切会把词/句子切断，模型容易漏译；
    按「段落 → 句子 → 装箱到 <= max_chars」既能控制 prompt 长度，又不破坏语义边界。
    """
    text = (source_text or "").strip()
    if not text:
        return []
    max_chars = max_chars or config.SEGMENT_MAX_CHARS
    paragraphs = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]

    # 中文按句末标点切；没有标点的长段落就整段作为一个单位。
    sentence_pattern = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")

    segments: list[str] = []
    for paragraph in paragraphs:
        sentences = [match.group(0).strip() for match in sentence_pattern.finditer(paragraph)]
        if not sentences:
            sentences = [paragraph]
        # 每段单独起一个 buffer：段落是最自然的语义边界，跨段合并会丢段落结构。
        buffer = ""
        for sentence in sentences:
            if not sentence:
                continue
            if not buffer:
                buffer = sentence
            elif len(buffer) + len(sentence) <= max_chars:
                buffer += sentence
            else:
                segments.append(buffer.strip())
                buffer = sentence
        if buffer.strip():
            segments.append(buffer.strip())

    return [{"index": index, "text": text_piece} for index, text_piece in enumerate(segments)]


def normalize_glossary(raw_glossary: Any, max_terms: int | None = None) -> list[dict]:
    """
    统一术语表格式：去空、去重（按 term）、截断数量。
    术语抽取节点和「人工编辑术语表」接口都走这里，保证 DB / state / prompt 三处结构一致。
    """
    max_terms = max_terms or config.MAX_GLOSSARY_TERMS
    if not raw_glossary:
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in raw_glossary:
        if isinstance(item, dict):
            term = str(item.get("term") or "").strip()
            translation = str(item.get("translation") or "").strip()
            note = str(item.get("note") or "").strip()
        else:  # 兼容 Pydantic GlossaryTerm
            term = str(getattr(item, "term", "") or "").strip()
            translation = str(getattr(item, "translation", "") or "").strip()
            note = str(getattr(item, "note", "") or "").strip()
        if not term or not translation or term in seen:
            continue
        seen.add(term)
        result.append({"term": term[:200], "translation": translation[:200], "note": note[:500]})
        if len(result) >= max_terms:
            break
    return result


def _version_bump(state: TranslationState) -> int:
    return int(state.get("version") or 0) + 1


# ============================================
# 条件边用的纯决策函数（全部只读 state，返回节点名）
# ============================================

def decide_after_review(state: TranslationState) -> Literal["translate", "polish", "human_review", "finalize"]:
    """
    审校之后的决策。注意这是纯函数：不写库、不改 state，只返回下一跳。
    """
    review = state.get("review") or {}
    overall = float(review.get("overall") or 0.0)
    retry = int(state.get("retry_count") or 0)
    max_retries = int(state.get("max_retries") or config.MAX_RETRY)
    human_enabled = bool(state.get("enable_human_review"))
    human_done = bool(state.get("human_decision"))
    unavailable = bool(state.get("review_unavailable") or review.get("unavailable"))

    if unavailable:
        # 审校不可用：不假装有分数，允许「再试一次」，之后转人工或失败收尾。
        if retry < max_retries and retry < 2:
            return "translate"
        return "human_review" if (human_enabled and not human_done) else "finalize"

    if overall >= config.PASS_SCORE:
        issues = review.get("issues") or []
        # 只有存在具体 issues 才值得润色；没有问题时润色纯属浪费 token。
        if issues:
            return "polish"
        return "human_review" if (human_enabled and not human_done) else "finalize"

    # 未达标：重试预算内继续重译，预算用尽后按开关转人工或失败。
    if retry < max_retries:
        return "translate"
    return "human_review" if (human_enabled and not human_done) else "finalize"


def decide_after_polish(state: TranslationState) -> Literal["human_review", "finalize"]:
    """润色完成：如果用户开了人工审核且还没审核过，先给人看润色后的版本。"""
    if state.get("enable_human_review") and not state.get("human_decision"):
        return "human_review"
    return "finalize"


def decide_after_human(state: TranslationState) -> Literal["translate", "finalize"]:
    """人工审核结果：通过 → 收尾；驳回且还有重试预算 → 重译；否则失败收尾。"""
    decision = state.get("human_decision") or {}
    if decision.get("approved"):
        return "finalize"
    retry = int(state.get("retry_count") or 0)
    max_retries = int(state.get("max_retries") or config.MAX_RETRY)
    return "translate" if retry < max_retries else "finalize"


_ROUTES: dict[str, str] = {
    "terminology": "terminology",
    "translate": "translate",
    "review": "review",
    "polish": "polish",
    "human_review": "human_review",
    "finalize": "finalize",
    "final": "end",
}


def route_after_supervisor(state: TranslationState) -> str:
    """条件边只做映射：supervisor 已经把决策写进 stage，这里翻译成节点名/END。"""
    return _ROUTES.get(str(state.get("stage") or "init"), "end")


# ============================================
# 节点：supervisor（主控）
# ============================================

def supervisor_node(state: TranslationState) -> dict:
    """
    主控节点：只做「初始化分段 + 决定下一步」，不调用 LLM、不访问数据库。

    为什么不把决策写在条件边里：LangGraph 的条件边也可能被多次求值（分支解析/重放），
    里面写库就会重复写；把它拆成「节点决策（可写 state）+ 条件边路由（纯函数）」两层，
    副作用边界最清楚。
    """
    stage = str(state.get("stage") or "init")
    version = _version_bump(state)

    def go(next_stage: str, note: str) -> dict:
        return {
            "stage": next_stage,
            "status": "running",
            "version": version,
            "history": [_history("supervisor", note)],
        }

    # 终态优先判断：即使 state 被裁剪过（例如只看终态快照），也不能再被「初始化」逻辑拉回去。
    if stage in ("final", "done", "failed"):
        # 流程已收尾：只补一条结束记录，不能覆盖 finalize 写入的 status。
        return {
            "stage": "final",
            "version": version,
            "history": [_history("supervisor", f"工作流结束（{state.get('status')}）")],
        }

    # 首次进入：切分原文（纯计算，不花 token），然后进入术语抽取。
    if not state.get("segments"):
        segments = split_segments(state.get("source_text", ""))
        update = go(
            "terminology",
            f"初始化：原文切分为 {len(segments)} 段，进入术语抽取；模型分工：{config.node_model_summary()}",
        )
        update["segments"] = segments
        return update

    if stage == "init":
        return go("terminology", "进入术语抽取")

    if stage == "terminology_done":
        return go("translate", "术语表就绪，进入翻译")

    if stage == "translated":
        return go("review", "翻译完成，进入审校")

    if stage == "reviewed":
        next_stage = decide_after_review(state)
        review = state.get("review") or {}
        note = (
            f"审校总分 {review.get('overall', 0)}，问题 {len(review.get('issues') or [])} 条"
            f"（重试 {state.get('retry_count', 0)}/{state.get('max_retries', config.MAX_RETRY)}）"
            f" → {next_stage}"
        )
        if state.get("review_unavailable"):
            note = "审校不可用，降级决策 → " + next_stage
        return go(next_stage, note)

    if stage == "polished":
        return go(decide_after_polish(state), "润色完成，进入人工审核/收尾")

    if stage == "human_resolved":
        next_stage = decide_after_human(state)
        decision = state.get("human_decision") or {}
        note = "人工审核通过 → 收尾" if decision.get("approved") else f"人工审核驳回 → {next_stage}"
        return go(next_stage, note)

    # 兜底：未知阶段直接去收尾，避免图卡死。
    return go("finalize", f"未知阶段 {stage}，兜底进入收尾")


# ============================================
# 节点：terminology（术语专家）
# ============================================

def terminology_node(state: TranslationState) -> dict:
    """
    抽取领域术语 + 固定译法，一次 LLM 调用。
    失败时降级为空术语表继续（术语缺失只影响一致性，不应让整个任务失败），
    但会把失败原因写进 history，前端/复盘能看到，不是静默吞错。
    """
    version = _version_bump(state)
    glossary: list[dict] = []
    try:
        glossary = agent.extract_terms(
            source_text=state.get("source_text", ""),
            source_lang=state.get("source_lang", "中文"),
            target_lang=state.get("target_lang", "English"),
            style=state.get("style", ""),
            domain=state.get("domain", ""),
        )
        note = f"术语抽取完成：{len(glossary)} 个术语"
        if not glossary:
            note = "术语抽取完成：未发现需固定的术语，空表继续"
    except Exception as exc:
        note = f"术语抽取失败（{type(exc).__name__}），降级为空术语表继续"
        print(f"[工作流] {note}: {exc}")

    return {
        "glossary": normalize_glossary(glossary),
        "stage": "terminology_done",
        "version": version,
        "history": [_history("terminology", note)],
    }


# ============================================
# 节点：translate（翻译专家）
# ============================================

def translate_node(state: TranslationState) -> dict:
    """
    分段翻译，并注入术语表（首译）或审校意见（重译）。

    ★ bug ① 修复点：retry_count 在函数第一行无条件 +1。
    旧代码只有「已有 review_feedback」分支才自增，首轮不自增，
    于是「评分<80 且 retry<3」永远成立 → 死循环。
    """
    retry_count = int(state.get("retry_count") or 0) + 1  # 每次进入翻译都计数，无条件

    version = _version_bump(state)
    segments = state.get("segments") or split_segments(state.get("source_text", ""))
    if not segments:
        raise RuntimeError("原文为空，无法进入翻译")

    review = state.get("review") or {}
    issues = (review.get("issues") or []) if retry_count > 1 else []
    previous: dict[int, str] = {
        int(item.get("index", -1)): str(item.get("text") or "")
        for item in (state.get("translations") or [])
        if isinstance(item, dict)
    }
    glossary = state.get("glossary") or []

    translated: list[dict] = []
    for segment in segments:
        index = int(segment.get("index", 0))
        target_text = agent.translate_segment(
            source_text=segment.get("text", ""),
            source_lang=state.get("source_lang", "中文"),
            target_lang=state.get("target_lang", "English"),
            glossary=glossary,
            style=state.get("style", ""),
            domain=state.get("domain", ""),
            review_issues=issues,
            previous_translation=previous.get(index, ""),
        )
        translated.append(
            {"index": index, "source": segment.get("text", ""), "text": target_text, "attempt": retry_count}
        )

    translation = "\n\n".join(item["text"] for item in translated)
    note = f"第 {retry_count} 次翻译完成：{len(translated)} 段，术语 {len(glossary)} 个"
    if retry_count > 1:
        note += f"，按审校意见修正 {len(issues)} 条"

    return {
        "translations": translated,
        "translation": translation,
        "retry_count": retry_count,
        "stage": "translated",
        "status": "running",
        "version": version,
        "error": "",
        "history": [_history("translate", note)],
    }


# ============================================
# 节点：review（审校专家）
# ============================================

def _review_note(review: dict) -> str:
    """
    时间线上的一句话审校摘要。
    单模型：`审校完成：总分 91.25，问题 2 条`
    双模型：`审校完成（双模型投票）：82.5 分（qwen 88 / glm 79），3/4 维度一致，分歧 1 项，问题 5 条`
    """
    if review.get("vote_mode") == "vote":
        scores = review.get("model_scores") or {}
        detail = " / ".join(f"{k} {v}" for k, v in scores.items())
        agree = int(round(float(review.get("agreement_rate") or 0) * 4))
        gaps = len(review.get("disagreements") or [])
        return (
            f"审校完成（双模型投票）：{review.get('overall')} 分（{detail}），"
            f"{agree}/4 维度一致，分歧 {gaps} 项，问题 {len(review.get('issues') or [])} 条"
        )
    return f"审校完成：总分 {review.get('overall')}，问题 {len(review.get('issues') or [])} 条"


def review_node(state: TranslationState) -> dict:
    """
    结构化审校（4 维评分 + issues）。
    解析失败 → review_unavailable=True，不伪造分数；supervisor 会按「重试一次再人工」处理。
    """
    version = _version_bump(state)
    translation = state.get("translation", "")
    try:
        review = agent.review_translation(
            source_text=state.get("source_text", ""),
            translation=translation,
            source_lang=state.get("source_lang", "中文"),
            target_lang=state.get("target_lang", "English"),
            glossary=state.get("glossary") or [],
        )
        note = _review_note(review)
        unavailable = False
    except Exception as exc:
        # 连结构化输出都拿不到时，明确标记不可用；绝不给默认 50 分。
        review = {
            "fidelity": 0,
            "terminology": 0,
            "fluency": 0,
            "completeness": 0,
            "overall": 0.0,
            "issues": [f"审校不可用：{type(exc).__name__}: {exc}"],
            "needs_retranslation": False,
            "unavailable": True,
        }
        note = f"审校不可用（{type(exc).__name__}），降级处理"
        unavailable = True
        print(f"[工作流] {note}: {exc}")

    return {
        "review": review,
        "review_unavailable": unavailable,
        "stage": "reviewed",
        "status": "running",
        "version": version,
        "history": [_history("review", note)],
    }


# ============================================
# 节点：polish（润色专家）
# ============================================

def polish_node(state: TranslationState) -> dict:
    """只在审校给出具体 issues 时做定向修改，不再整篇重译（省 token 且避免改坏已通过的句子）。"""
    version = _version_bump(state)
    review = state.get("review") or {}
    issues = review.get("issues") or []
    current = state.get("translation", "")

    if not issues:
        return {
            "polished": current,
            "stage": "polished",
            "version": version,
            "history": [_history("polish", "审校无具体问题，跳过润色")],
        }

    polished = agent.polish_translation(
        source_text=state.get("source_text", ""),
        translation=current,
        issues=issues,
        source_lang=state.get("source_lang", "中文"),
        target_lang=state.get("target_lang", "English"),
        glossary=state.get("glossary") or [],
    )
    return {
        "polished": polished,
        "translation": polished,  # 最终交付使用润色后的版本
        "stage": "polished",
        "version": version,
        "history": [_history("polish", f"定向润色完成：处理 {len(issues)} 条问题")],
    }


# ============================================
# 节点：human_review（人在环）
# ============================================

def human_review_node(state: TranslationState) -> dict:
    """
    用 interrupt() 暂停，等待 POST /api/tasks/{id}/resume 把 {approved, edited_translation, comment} 送回来。

    注意：interrupt() 之前的代码在恢复时会重新执行，所以这里只做纯计算（组装展示载荷），
    不能有任何 DB 写入 / 计数自增，否则恢复一次就多写一次。
    """
    version = _version_bump(state)
    payload = {
        "type": "human_review",
        "task_id": state.get("task_id"),
        "source_lang": state.get("source_lang"),
        "target_lang": state.get("target_lang"),
        "source_text": state.get("source_text", ""),
        "translation": state.get("translation", ""),
        "review": state.get("review") or {},
        "glossary": state.get("glossary") or [],
        "question": "请确认译文是否可用；驳回后会在剩余重试预算内重新翻译。",
    }
    decision = interrupt(payload)
    if not isinstance(decision, dict):
        # 容错：如果客户端直接传了布尔值（脚本调用场景），也按 approved 处理。
        decision = {"approved": bool(decision)}

    approved = bool(decision.get("approved"))
    edited = str(decision.get("edited_translation") or "").strip()
    comment = str(decision.get("comment") or "").strip()
    final_translation = edited or state.get("translation", "")

    note = "人工审核通过" if approved else "人工审核驳回"
    if edited:
        note += "（含人工修改）"
    if comment:
        note += f"：{comment[:100]}"

    return {
        "human_decision": {"approved": approved, "edited": bool(edited), "comment": comment, "at": _now_iso()},
        "translation": final_translation,
        "stage": "human_resolved",
        "status": "running",
        "version": version,
        "history": [_history("human_review", note)],
    }


# ============================================
# 节点：finalize（收尾）
# ============================================

def finalize_node(state: TranslationState) -> dict:
    """
    收尾：判定 done/failed 并写失败原因。
    这里仍然不写数据库——落库由 core.runner 在 stream 的每个 update 后统一做，
    好处是节点可重放、测试里不需要 DB。
    """
    version = _version_bump(state)
    translation = str(state.get("translation") or "").strip()
    review = state.get("review") or {}
    overall = float(review.get("overall") or 0.0)
    human = state.get("human_decision") or {}
    unavailable = bool(state.get("review_unavailable") or review.get("unavailable"))
    retry_count = int(state.get("retry_count") or 0)

    if not translation:
        status, error = "failed", "最终译文为空"
    elif human.get("approved") is True:
        status, error = "done", ""
    elif overall >= config.PASS_SCORE and not unavailable:
        status, error = "done", ""
    elif human.get("approved") is False:
        status, error = "failed", "人工审核驳回，且已无剩余重试次数"
    elif unavailable:
        status, error = "failed", "审校模型持续不可用，任务未通过质量校验"
    else:
        status, error = "failed", (
            f"审校总分 {overall} 低于阈值 {config.PASS_SCORE}，已翻译 {retry_count} 次仍未达标"
        )

    note = f"任务收尾：{status}" + (f"（{error}）" if error else "")
    return {
        "status": status,
        "stage": "final",
        "error": error,
        "version": version,
        "history": [_history("finalize", note)],
    }


# ============================================
# 构建工作流图
# ============================================

_compiled_graph: Any | None = None


def build_workflow(checkpointer: Any | None = None):
    """
    编译图。checkpointer 显式传入便于测试（MemorySaver / tmp 文件 SqliteSaver）；
    不传则用进程级 SqliteSaver 单例（生产路径）。
    """
    if checkpointer is None:
        from core.checkpointer import get_checkpointer

        checkpointer = get_checkpointer()

    builder = StateGraph(TranslationState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("terminology", terminology_node)
    builder.add_node("translate", translate_node)
    builder.add_node("review", review_node)
    builder.add_node("polish", polish_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "terminology": "terminology",
            "translate": "translate",
            "review": "review",
            "polish": "polish",
            "human_review": "human_review",
            "finalize": "finalize",
            "end": END,
        },
    )
    # 专家节点执行完一律回到主控，由主控决定下一跳（星型拓扑，便于加新专家）。
    for node_name in ("terminology", "translate", "review", "polish", "human_review", "finalize"):
        builder.add_edge(node_name, "supervisor")

    compiled = builder.compile(checkpointer=checkpointer)
    print("[工作流] 图构建完成：supervisor → terminology → translate → review → polish/human_review → finalize")
    return compiled


def get_workflow():
    """生产路径的图单例（带进程级 SqliteSaver）。"""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_workflow()
    return _compiled_graph


# ============================================
# 状态初始化（纯函数，供 runner / 测试复用）
# ============================================

def build_initial_state(task: dict, *, restart: bool = False) -> TranslationState:
    """
    从 DB 任务行构造图的初始 state。

    restart=True（/retry）：沿用 DB 里的 version，保证版本号单调；
    其余运行态字段全部显式覆盖，避免上一次执行的 review/translation 残留。
    """
    return {
        "task_id": int(task["id"]),
        "source_text": task.get("source_text") or "",
        "source_lang": task.get("source_lang") or "中文",
        "target_lang": task.get("target_lang") or "English",
        "style": task.get("style") or "",
        "domain": task.get("domain") or "",
        "enable_human_review": bool(task.get("enable_human_review") or 0),
        "segments": [],
        "glossary": [],
        "translations": [],
        "translation": "",
        "polished": "",
        "review": {},
        "review_unavailable": False,
        "human_decision": None,
        "retry_count": 0,
        "max_retries": config.MAX_RETRY,
        "status": "running",
        "stage": "init",
        "version": int(task.get("version") or 0) if restart else 0,
        "error": "",
        "history": [],
    }


def run_translation(task_id: int, source_text: str, source_lang: str, target_lang: str) -> dict:
    """
    兼容旧代码的同步入口：只执行图并返回最终 state，不负责落库/SSE。
    生产路径请用 core.runner.execute_task（它带幂等落库与断点恢复）。
    """
    task = {"id": task_id, "source_text": source_text, "source_lang": source_lang, "target_lang": target_lang}
    graph = get_workflow()
    config_ = {"configurable": {"thread_id": str(task_id)}}
    return graph.invoke(build_initial_state(task), config=config_)