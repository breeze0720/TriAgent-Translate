"""
test_glossary.py - 术语表一致性测试

术语表的价值在于「同一条术语在翻译/审校/润色三个环节用同一份约束」，
所以测试重点是：
- terminology 节点的输出被归一化后再进入 state；
- translate / review 节点确实把 glossary 传给了 LLM 能力；
- format_glossary 渲染出的文本包含术语和固定译法。
"""
import agent
import workflow

GLOSSARY = [{"term": "人工智能", "translation": "artificial intelligence", "note": "AI 领域"}]


def test_format_glossary_renders_terms_and_handles_empty():
    rendered = agent.format_glossary(GLOSSARY)
    assert "人工智能 => artificial intelligence" in rendered
    assert "AI 领域" in rendered
    assert agent.format_glossary([]) == ""
    assert agent.format_glossary(None) == ""


def test_terminology_node_normalizes_llm_output(monkeypatch):
    monkeypatch.setattr(
        agent,
        "extract_terms",
        lambda **kwargs: [
            {"term": " 人工智能 ", "translation": " artificial intelligence ", "note": ""},
            {"term": "人工智能", "translation": "AI", "note": "重复项应丢弃"},
        ],
    )
    update = workflow.terminology_node(
        {"task_id": 1, "source_text": "人工智能。", "source_lang": "中文", "target_lang": "English", "version": 0}
    )

    assert update["glossary"] == [
        {"term": "人工智能", "translation": "artificial intelligence", "note": ""}
    ]
    assert update["stage"] == "terminology_done"


def test_translate_node_injects_glossary_and_retry_feedback(monkeypatch):
    captured: dict = {}

    def fake_translate(**kwargs):
        captured.update(kwargs)
        return "Artificial intelligence is changing the world."

    monkeypatch.setattr(agent, "translate_segment", fake_translate)

    state = {
        "task_id": 1,
        "source_text": "人工智能正在改变世界。",
        "source_lang": "中文",
        "target_lang": "English",
        "segments": [{"index": 0, "text": "人工智能正在改变世界。"}],
        "glossary": GLOSSARY,
        "translations": [],
        "retry_count": 0,
        "version": 0,
    }
    update = workflow.translate_node(state)

    assert captured["glossary"] == GLOSSARY  # 首译就把术语表注入 prompt
    assert update["retry_count"] == 1  # 首次进入翻译也必须自增
    assert update["translations"][0]["index"] == 0
    assert update["translations"][0]["source"] == "人工智能正在改变世界。"
    assert update["stage"] == "translated"

    # 重译：必须带上审校 issues 和上一版译文，做定向修正
    retry_state = {
        **state,
        "retry_count": 1,
        "version": 3,
        "translations": [{"index": 0, "source": "人工智能正在改变世界。", "text": "旧译文"}],
        "review": {"overall": 60.0, "issues": ["术语译错：人工智能应译为 artificial intelligence"]},
    }
    update = workflow.translate_node(retry_state)

    assert captured["glossary"] == GLOSSARY
    assert captured["review_issues"] == ["术语译错：人工智能应译为 artificial intelligence"]
    assert captured["previous_translation"] == "旧译文"
    assert update["retry_count"] == 2
    assert update["translations"][0]["attempt"] == 2


def test_review_node_passes_glossary_and_builds_structured_review(monkeypatch):
    captured: dict = {}

    def fake_review(**kwargs):
        captured.update(kwargs)
        return {
            "fidelity": 90,
            "terminology": 88,
            "fluency": 92,
            "completeness": 90,
            "overall": 89.6,
            "issues": [],
            "needs_retranslation": False,
            "unavailable": False,
        }

    monkeypatch.setattr(agent, "review_translation", fake_review)

    update = workflow.review_node(
        {
            "task_id": 1,
            "source_text": "人工智能正在改变世界。",
            "translation": "Artificial intelligence is changing the world.",
            "source_lang": "中文",
            "target_lang": "English",
            "glossary": GLOSSARY,
            "version": 2,
        }
    )

    assert captured["glossary"] == GLOSSARY  # 审校也要按同一份术语表检查一致性
    assert update["review"]["overall"] == 89.6
    assert update["review_unavailable"] is False
    assert update["stage"] == "reviewed"
    assert update["version"] == 3


def test_review_node_marks_unavailable_instead_of_fake_score(monkeypatch):
    def boom(**kwargs):
        raise agent.ReviewUnavailableError("模拟结构化输出失败")

    monkeypatch.setattr(agent, "review_translation", boom)

    update = workflow.review_node(
        {
            "task_id": 1,
            "source_text": "人工智能。",
            "translation": "AI.",
            "source_lang": "中文",
            "target_lang": "English",
            "version": 1,
        }
    )

    assert update["review_unavailable"] is True
    assert update["review"]["overall"] == 0.0
    assert "审校不可用" in update["review"]["issues"][0]