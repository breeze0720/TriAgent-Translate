"""
workflow.py - LangGraph 多智能体翻译工作流
使用 LangGraph StateGraph 编排 "翻译 → 审校 → 润色" 三阶段流程。
审校评分 ≥ 80 分进入润色阶段，低于 80 分回退重译（最多 3 次）。

工作流拓扑：
    START → translator → reviewer → [评分 >= 80?]
                                        ├─ 是 → polisher → END
                                        └─ 否 → [重试 < 3?]
                                                ├─ 是 → translator（带反馈）
                                                └─ 否 → END（标记失败）
"""
from typing import TypedDict, Literal

from langgraph.graph import StateGraph, END

from agent import translate, review, polish
from database import (
    update_task_status,
    save_translation,
    save_review,
    save_polished,
    mark_failed,
    increment_retry,
    save_review_record,
)


# ============================================
# 工作流状态定义
# ============================================

class TranslationState(TypedDict):
    """LangGraph 工作流的状态，在各节点间流转"""
    task_id: int              # 数据库任务ID
    source_text: str          # 原文
    source_lang: str          # 源语言
    target_lang: str          # 目标语言
    translation: str          # 翻译Agent输出
    review_feedback: str      # 审校Agent意见
    review_score: float       # 审校评分 (0-100)
    polished: str             # 润色Agent输出
    retry_count: int          # 当前重试次数
    status: str               # 任务状态


# ============================================
# 节点函数
# ============================================

def translator_node(state: TranslationState) -> TranslationState:
    """翻译节点：调用翻译Agent（DeepSeek）生成译文"""
    print(f"\n{'='*40}")
    print(f"[工作流] 翻译阶段 (任务#{state['task_id']})")
    print(f"{'='*40}")

    update_task_status(state["task_id"], "translating")

    if state["retry_count"] > 0:
        # 携带审校反馈重新翻译
        result = translate(
            source_text=state["source_text"],
            source_lang=state["source_lang"],
            target_lang=state["target_lang"],
            review_feedback=state["review_feedback"],
            previous_translation=state["translation"],
        )
        new_retry = increment_retry(state["task_id"])
        state["retry_count"] = new_retry
    else:
        result = translate(
            source_text=state["source_text"],
            source_lang=state["source_lang"],
            target_lang=state["target_lang"],
        )

    state["translation"] = result
    save_translation(state["task_id"], result)
    return state


def reviewer_node(state: TranslationState) -> TranslationState:
    """审校节点：调用审校Agent（Qwen）评估翻译质量"""
    print(f"\n{'='*40}")
    print(f"[工作流] 审校阶段 (任务#{state['task_id']})")
    print(f"{'='*40}")

    update_task_status(state["task_id"], "reviewing")

    score, feedback = review(
        source_text=state["source_text"],
        translation=state["translation"],
        source_lang=state["source_lang"],
        target_lang=state["target_lang"],
    )

    state["review_score"] = score
    state["review_feedback"] = feedback

    save_review(state["task_id"], feedback, score)

    # 保存审校历史记录（用于复盘追溯）
    version = state["retry_count"] + 1
    save_review_record(
        task_id=state["task_id"],
        version=version,
        translation_text=state["translation"],
        review_content=feedback,
        score=score,
    )

    return state


def polisher_node(state: TranslationState) -> TranslationState:
    """润色节点：调用润色Agent（DeepSeek）对通过审校的翻译进行最终润色"""
    print(f"\n{'='*40}")
    print(f"[工作流] 润色阶段 (任务#{state['task_id']})")
    print(f"{'='*40}")

    update_task_status(state["task_id"], "polishing")

    result = polish(
        source_text=state["source_text"],
        translation=state["translation"],
        review_feedback=state["review_feedback"],
        source_lang=state["source_lang"],
        target_lang=state["target_lang"],
    )

    state["polished"] = result
    state["status"] = "done"
    save_polished(state["task_id"], result)

    return state


# ============================================
# 条件路由
# ============================================

def should_continue(state: TranslationState) -> Literal["polisher", "translator", "end"]:
    """
    审校后的条件判断：
    - 评分 ≥ 80 → 进入润色阶段
    - 评分 < 80 且重试 < 3 次 → 返回翻译阶段重新翻译
    - 评分 < 80 且重试 ≥ 3 次 → 标记失败，结束流程
    """
    score = state["review_score"]
    retry = state["retry_count"]
    max_retries = 3

    print(f"\n[工作流] 条件判断: 评分={score}, 重试次数={retry}/{max_retries}")

    if score >= 80:
        print(f"[工作流] [通过] 审校通过 (评分 {score} >= 80)，进入润色阶段")
        return "polisher"
    elif retry < max_retries:
        print(f"[工作流] [重试] 审校未通过 (评分 {score} < 80)，第{retry+1}次重试")
        return "translator"
    else:
        print(f"[工作流] [失败] 已达最大重试次数 ({max_retries})，任务标记为失败")
        state["status"] = "failed"
        mark_failed(state["task_id"], f"审校评分 {score} 未达标，已重试 {max_retries} 次")
        return "end"


# ============================================
# 构建工作流图
# ============================================

def build_workflow() -> StateGraph:
    """
    构建并编译 LangGraph 工作流图。
    返回编译后的 StateGraph（可调用 .invoke() 执行）。
    """
    workflow = StateGraph(TranslationState)

    workflow.add_node("translator", translator_node)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("polisher", polisher_node)

    workflow.set_entry_point("translator")
    workflow.add_edge("translator", "reviewer")

    workflow.add_conditional_edges(
        "reviewer",
        should_continue,
        {
            "translator": "translator",  # 评分低，回退重译
            "polisher": "polisher",      # 评分高，进入润色
            "end": END,                  # 重试耗尽，结束
        }
    )

    workflow.add_edge("polisher", END)

    compiled = workflow.compile()
    print("[工作流] LangGraph 工作流图构建完成")
    print("[工作流] 节点: translator → reviewer → [条件判断] → polisher → END")
    print("[工作流] 回退条件: 评分 < 80 且重试 < 3 次时回退到 translator")
    return compiled


# ============================================
# 工作流执行入口
# ============================================

_translation_workflow = None


def get_workflow() -> StateGraph:
    """获取编译好的工作流实例（懒加载，首次调用时初始化）"""
    global _translation_workflow
    if _translation_workflow is None:
        _translation_workflow = build_workflow()
    return _translation_workflow


def run_translation(task_id: int, source_text: str, source_lang: str, target_lang: str) -> TranslationState:
    """
    执行完整的翻译工作流。

    参数:
        task_id: 数据库任务ID
        source_text: 待翻译的原文
        source_lang: 源语言代码
        target_lang: 目标语言代码

    返回:
        工作流最终状态
    """
    print(f"\n{'#'*60}")
    print(f"  启动翻译工作流 - 任务#{task_id}")
    print(f"  源语言: {source_lang} → 目标语言: {target_lang}")
    print(f"  原文预览: {source_text[:60]}...")
    print(f"{'#'*60}")

    initial_state: TranslationState = {
        "task_id": task_id,
        "source_text": source_text,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "translation": "",
        "review_feedback": "",
        "review_score": 0.0,
        "polished": "",
        "retry_count": 0,
        "status": "running",
    }

    workflow = get_workflow()
    final_state = workflow.invoke(initial_state)

    print(f"\n{'#'*60}")
    print(f"  工作流执行完毕 - 最终状态: {final_state.get('status')}")
    print(f"{'#'*60}\n")

    return final_state
