"""
test_retry_no_infinite_loop.py - 回归测试：重试计数与有限轮次协商

v1 的致命 bug：translator_node 只在「已有审校反馈」分支自增 retry_count，
首轮不自增，于是「评分 < 80 且 retry < 3」永远成立 → 无限循环。
这里用 mock 的 LLM（审校恒 60 分）把「最多翻译 max_retries 次后 failed」钉死。
"""
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import agent
import config
import workflow


def _initial_state(**overrides):
    task = {
        "id": 101,
        "source_text": "人工智能正在改变世界的方方面面。",
        "source_lang": "中文",
        "target_lang": "English",
        "style": "",
        "domain": "",
        "enable_human_review": False,
    }
    task.update(overrides)
    return workflow.build_initial_state(task)


def _patch_agents(monkeypatch, review_factory):
    """把 4 个 LLM 能力替换成确定性假实现，并统计调用次数。"""
    calls = {"terminology": 0, "translate": 0, "review": 0, "polish": 0}

    def fake_extract_terms(**kwargs):
        calls["terminology"] += 1
        return [{"term": "人工智能", "translation": "artificial intelligence", "note": "领域术语"}]

    def fake_translate_segment(**kwargs):
        calls["translate"] += 1
        if calls["translate"] > 20:
            # 真正的死循环会让测试挂住；这里主动爆炸，让失败信息直接指向 bug。
            raise AssertionError("翻译节点调用次数超过 20，疑似重试死循环")
        return f"[attempt {calls['translate']}] {kwargs['source_text']}"

    def fake_review(**kwargs):
        calls["review"] += 1
        return review_factory(calls)

    def fake_polish(**kwargs):
        calls["polish"] += 1
        return "polished: " + kwargs["translation"]

    monkeypatch.setattr(agent, "extract_terms", fake_extract_terms)
    monkeypatch.setattr(agent, "translate_segment", fake_translate_segment)
    monkeypatch.setattr(agent, "review_translation", fake_review)
    monkeypatch.setattr(agent, "polish_translation", fake_polish)
    return calls


def _low_review(_calls):
    return {
        "fidelity": 60,
        "terminology": 60,
        "fluency": 60,
        "completeness": 60,
        "overall": 60.0,
        "issues": ["译文中漏掉了限定词"],
        "needs_retranslation": True,
        "unavailable": False,
    }


def _pass_review(issues=None):
    def factory(_calls):
        return {
            "fidelity": 95,
            "terminology": 92,
            "fluency": 90,
            "completeness": 94,
            "overall": 92.0,
            "issues": issues or [],
            "needs_retranslation": False,
            "unavailable": False,
        }

    return factory


def _compile_graph():
    return workflow.build_workflow(checkpointer=MemorySaver())


def test_retry_stops_after_max_retries_and_fails(monkeypatch):
    calls = _patch_agents(monkeypatch, _low_review)
    graph = _compile_graph()
    graph_config = {"configurable": {"thread_id": "retry-loop-test"}}

    result = graph.invoke(_initial_state(), config=graph_config)

    # 1) 有限轮次：恰好翻译 max_retries 次，不会无限循环
    assert calls["translate"] == config.MAX_RETRY
    assert calls["review"] == config.MAX_RETRY
    assert calls["polish"] == 0
    assert result["retry_count"] == config.MAX_RETRY
    assert result["status"] == "failed"
    assert result["stage"] == "final"

    # 2) 每次进入翻译都 +1（首轮也必须自增，这正是 v1 的 bug）
    translate_entries = [item for item in result["history"] if item["stage"] == "translate"]
    assert len(translate_entries) == config.MAX_RETRY
    assert "第 1 次翻译完成" in translate_entries[0]["note"]

    # 3) 累积型 history + 递增 version
    assert len(result["history"]) >= config.MAX_RETRY * 3
    assert result["version"] > config.MAX_RETRY


def test_pass_first_attempt_skips_polish_when_no_issues(monkeypatch):
    calls = _patch_agents(monkeypatch, _pass_review())
    graph = _compile_graph()
    result = graph.invoke(_initial_state(), config={"configurable": {"thread_id": "pass-test"}})

    assert result["status"] == "done"
    assert calls["translate"] == 1
    assert calls["review"] == 1
    assert calls["polish"] == 0  # 没有具体 issues → 不润色，省 token
    assert result["retry_count"] == 1


def test_pass_with_issues_triggers_targeted_polish(monkeypatch):
    calls = _patch_agents(monkeypatch, _pass_review(issues=["第二句术语不一致"]))
    graph = _compile_graph()
    result = graph.invoke(_initial_state(), config={"configurable": {"thread_id": "polish-test"}})

    assert result["status"] == "done"
    assert calls["polish"] == 1
    assert result["translation"].startswith("polished:")
    assert result["polished"].startswith("polished:")


def test_review_unavailable_does_not_fake_score_and_is_bounded(monkeypatch):
    calls = {"translate": 0, "review": 0}

    def fake_translate_segment(**kwargs):
        calls["translate"] += 1
        return "translation"

    def fake_review(**kwargs):
        calls["review"] += 1
        raise agent.ReviewUnavailableError("模拟结构化输出连续失败")

    monkeypatch.setattr(agent, "extract_terms", lambda **kwargs: [])
    monkeypatch.setattr(agent, "translate_segment", fake_translate_segment)
    monkeypatch.setattr(agent, "review_translation", fake_review)

    graph = _compile_graph()
    result = graph.invoke(_initial_state(), config={"configurable": {"thread_id": "unavailable-test"}})

    # 审校不可用：允许再试一次；仍不可用则 failed，而不是伪造 50 分继续润色
    assert calls["translate"] == 2
    assert calls["review"] == 2
    assert result["status"] == "failed"
    assert result["review_unavailable"] is True
    assert result["review"]["overall"] == 0.0


def test_human_review_interrupts_and_resumes(monkeypatch):
    calls = _patch_agents(monkeypatch, _pass_review())
    graph = _compile_graph()
    graph_config = {"configurable": {"thread_id": "human-test"}}

    result = graph.invoke(_initial_state(enable_human_review=True), config=graph_config)

    # 开了人工审核：图应停在 human_review，而不是直接 done
    assert "__interrupt__" in result
    assert calls["translate"] == 1

    final = graph.invoke(
        Command(resume={"approved": True, "edited_translation": "人工修改后的译文", "comment": "可以"}),
        config=graph_config,
    )

    assert final["status"] == "done"
    assert final["translation"] == "人工修改后的译文"
    assert final["human_decision"]["approved"] is True
    assert any(item["stage"] == "human_review" for item in final["history"])


def test_human_reject_triggers_retry_then_finalize(monkeypatch):
    calls = _patch_agents(monkeypatch, _pass_review())
    graph = _compile_graph()
    graph_config = {"configurable": {"thread_id": "human-reject-test"}}

    graph.invoke(_initial_state(enable_human_review=True), config=graph_config)
    final = graph.invoke(Command(resume={"approved": False, "comment": "重译"}), config=graph_config)

    # 人工驳回 + 还有重试预算 → 再翻译一次；human_decision 已存在，不会再次 interrupt
    assert calls["translate"] == 2
    assert final["status"] == "done"  # 第二次翻译通过审校且不再人工拦截
    assert final["human_decision"]["approved"] is False