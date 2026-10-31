"""
test_state_machine.py - 状态机纯函数测试

这些测试不调用 LLM、不连数据库，专门锁住「主控决策」的正确性：
- 分段/术语表归一化；
- 审校后、润色后、人工审核后的下一跳；
- 条件边路由函数只返回字符串（纯函数）。
"""
import config
import workflow


# ============================================
# 分段 / 术语表
# ============================================

def test_split_segments_keeps_all_characters():
    text = "第一句话。第二句话！第三句话？第四句话；第五句话。" * 12
    segments = workflow.split_segments(text, max_chars=50)

    assert len(segments) > 1
    assert all(len(item["text"]) <= 50 for item in segments)
    # 装箱只是把句子拼起来，不应该丢字符
    assert "".join(item["text"] for item in segments) == text
    assert [item["index"] for item in segments] == list(range(len(segments)))


def test_split_segments_respects_paragraphs_and_empty_input():
    assert workflow.split_segments("   ") == []
    segments = workflow.split_segments("第一段。\n\n第二段。")
    assert len(segments) == 2
    assert segments[0]["text"] == "第一段。"
    assert segments[1]["text"] == "第二段。"


def test_normalize_glossary_dedupes_and_truncates():
    raw = [
        {"term": " 人工智能 ", "translation": " artificial intelligence ", "note": "  "},
        {"term": "人工智能", "translation": "AI", "note": "重复术语应被丢弃"},
        {"term": "", "translation": "空术语应被丢弃"},
        {"term": "大模型", "translation": "large language model", "note": "领域术语"},
    ]
    result = workflow.normalize_glossary(raw)

    assert result == [
        {"term": "人工智能", "translation": "artificial intelligence", "note": ""},
        {"term": "大模型", "translation": "large language model", "note": "领域术语"},
    ]
    assert len(workflow.normalize_glossary(raw, max_terms=1)) == 1


# ============================================
# supervisor 决策（审校之后）
# ============================================

def _state(**overrides):
    base = {
        "task_id": 1,
        "source_lang": "中文",
        "target_lang": "English",
        "source_text": "人工智能正在改变世界。",
        "enable_human_review": False,
        "retry_count": 1,
        "max_retries": config.MAX_RETRY,
        "review": {"overall": 60.0, "issues": ["漏译了限定词"]},
        "review_unavailable": False,
        "human_decision": None,
    }
    base.update(overrides)
    return base


def test_decide_after_review_low_score_retries_within_budget():
    assert workflow.decide_after_review(_state(retry_count=1)) == "translate"
    assert workflow.decide_after_review(_state(retry_count=1, max_retries=3)) == "translate"


def test_decide_after_review_low_score_exhausted_fails_or_goes_human():
    state = _state(retry_count=3, review={"overall": 70.0, "issues": []})
    assert workflow.decide_after_review(state) == "finalize"

    state_human = dict(state, enable_human_review=True)
    assert workflow.decide_after_review(state_human) == "human_review"


def test_decide_after_review_passed_with_issues_polishes():
    state = _state(retry_count=1, review={"overall": 88.0, "issues": ["术语不一致"]})
    assert workflow.decide_after_review(state) == "polish"


def test_decide_after_review_passed_without_issues_finishes_or_human():
    state = _state(retry_count=1, review={"overall": 92.0, "issues": []})
    assert workflow.decide_after_review(state) == "finalize"
    assert workflow.decide_after_review(dict(state, enable_human_review=True)) == "human_review"


def test_decide_after_review_unavailable_retries_once_then_degrades():
    unavailable_review = {
        "overall": 0.0,
        "issues": ["审校不可用"],
        "unavailable": True,
    }
    # 首次审校不可用：允许再试一次
    state = _state(review=unavailable_review, review_unavailable=True, retry_count=1)
    assert workflow.decide_after_review(state) == "translate"

    # 重试一次后仍不可用：不再无限重试 → 失败 / 转人工
    state_after_retry = dict(state, retry_count=2)
    assert workflow.decide_after_review(state_after_retry) == "finalize"
    assert workflow.decide_after_review(dict(state_after_retry, enable_human_review=True)) == "human_review"


def test_decide_after_polish_and_human():
    polished = _state()
    assert workflow.decide_after_polish(polished) == "finalize"
    assert workflow.decide_after_polish(dict(polished, enable_human_review=True)) == "human_review"

    approved = _state(human_decision={"approved": True})
    rejected = _state(human_decision={"approved": False}, retry_count=1)
    rejected_exhausted = _state(human_decision={"approved": False}, retry_count=3)

    assert workflow.decide_after_human(approved) == "finalize"
    assert workflow.decide_after_human(rejected) == "translate"
    assert workflow.decide_after_human(rejected_exhausted) == "finalize"


# ============================================
# 条件边路由 / supervisor 节点
# ============================================

def test_route_after_supervisor_is_pure_mapping():
    assert workflow.route_after_supervisor({"stage": "terminology"}) == "terminology"
    assert workflow.route_after_supervisor({"stage": "translate"}) == "translate"
    assert workflow.route_after_supervisor({"stage": "review"}) == "review"
    assert workflow.route_after_supervisor({"stage": "polish"}) == "polish"
    assert workflow.route_after_supervisor({"stage": "human_review"}) == "human_review"
    assert workflow.route_after_supervisor({"stage": "finalize"}) == "finalize"
    assert workflow.route_after_supervisor({"stage": "final"}) == "end"
    # 未知阶段兜底结束，避免图卡死
    assert workflow.route_after_supervisor({"stage": "something-wrong"}) == "end"


def test_supervisor_initializes_segments_and_advances_state_machine():
    update = workflow.supervisor_node({"task_id": 1, "source_text": "第一句。第二句。", "version": 0})

    assert update["stage"] == "terminology"
    assert update["version"] == 1
    assert len(update["segments"]) >= 1
    assert update["history"][0]["stage"] == "supervisor"

    update = workflow.supervisor_node(
        {
            "task_id": 1,
            "source_text": "第一句。",
            "segments": [{"index": 0, "text": "第一句。"}],
            "stage": "terminology_done",
            "version": 1,
        }
    )
    assert update["stage"] == "translate"


def test_supervisor_final_stage_does_not_overwrite_status():
    update = workflow.supervisor_node({"stage": "final", "status": "done", "version": 7})
    assert update["stage"] == "final"
    assert "status" not in update  # 不能把 finalize 写的 done 覆盖成 running
    assert update["version"] == 8