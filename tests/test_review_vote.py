"""
双模型审校投票的纯函数测试。

为什么这些用例值得写：
  「多模型」最容易变成只能嘴上说的 feature——两个模型跑起来，结果怎么合并没人验证，
  面试官一问「取严还是取平均、分歧怎么定义、一个挂了怎么办」就露馅。
  合并策略全部写成纯函数以后，这些规则就变成可断言的事实。
"""
import pytest

from core.review_vote import merge_reviews, summarize_vote, _ordered_union


def _review(provider: str, **kw) -> dict:
    base = {
        "provider": provider,
        "fidelity": 90,
        "terminology": 90,
        "fluency": 90,
        "completeness": 90,
        "overall": 90.0,
        "issues": [],
        "needs_retranslation": False,
        "unavailable": False,
    }
    base.update(kw)
    return base


WEIGHTS = {"fidelity": 0.4, "terminology": 0.25, "fluency": 0.2, "completeness": 0.15}


# ============================================
# 单模型：必须与旧行为完全一致（不能因为加了投票把老逻辑改坏）
# ============================================

def test_single_review_passthrough():
    merged = merge_reviews([_review("qwen", overall=88.5, issues=["A 句漏译"])], weights=WEIGHTS)
    assert merged["vote_mode"] == "single"
    assert merged["providers"] == ["qwen"]
    assert merged["overall"] == 88.5
    assert merged["issues"] == ["A 句漏译"]
    assert merged["agreement_rate"] == 1.0
    assert merged["disagreements"] == []


def test_unavailable_results_are_ignored():
    """一个模型挂了、另一个正常 → 退化成单模型，而不是把 0 分算进投票。"""
    merged = merge_reviews(
        [
            _review("qwen", unavailable=True, fidelity=0, terminology=0, fluency=0, completeness=0),
            _review("glm", overall=77.0),
        ],
        weights=WEIGHTS,
    )
    assert merged["vote_mode"] == "single"
    assert merged["providers"] == ["glm"]
    assert merged["overall"] == 77.0


def test_all_unavailable_raises():
    with pytest.raises(ValueError):
        merge_reviews([_review("qwen", unavailable=True), _review("glm", unavailable=True)])


# ============================================
# 双模型：取严、重算总分、并集去重、分歧检测
# ============================================

def test_takes_stricter_score_per_dimension():
    merged = merge_reviews(
        [
            _review("qwen", fidelity=90, terminology=60, fluency=85, completeness=95),
            _review("glm", fidelity=80, terminology=70, fluency=90, completeness=90),
        ],
        weights=WEIGHTS,
    )
    assert merged["fidelity"] == 80          # min(90, 80)
    assert merged["terminology"] == 60       # min(60, 70) —— 术语维度取严，宁可让人看一眼
    assert merged["fluency"] == 85           # min(85, 90)
    assert merged["completeness"] == 90      # min(95, 90)


def test_overall_is_recomputed_from_merged_dims():
    merged = merge_reviews(
        [
            _review("qwen", fidelity=100, terminology=100, fluency=100, completeness=100),
            _review("glm", fidelity=80, terminology=80, fluency=80, completeness=80),
        ],
        weights=WEIGHTS,
    )
    assert merged["overall"] == pytest.approx(80.0)
    assert merged["model_scores"] == {"qwen": 90.0, "glm": 90.0}
    assert merged["dimension_scores"]["fidelity"] == {"qwen": 100, "glm": 80}


def test_issues_union_dedupes_and_puts_shared_first():
    merged = merge_reviews(
        [
            _review("qwen", issues=["第一句漏译", "术语 payment 未统一"]),
            _review("glm", issues=["术语 payment 未统一。", "第三段语气生硬"]),
        ],
        weights=WEIGHTS,
    )
    issues = merged["issues"]
    assert len(issues) == 3, issues                      # 去重后 3 条
    assert issues[0].startswith("术语 payment")          # 两家都提到的排最前
    assert "第三段语气生硬" in issues


def test_disagreement_detected_above_threshold():
    merged = merge_reviews(
        [
            _review("qwen", fidelity=95, terminology=95, fluency=95, completeness=95),
            _review("glm", fidelity=70, terminology=92, fluency=94, completeness=95),
        ],
        weights=WEIGHTS,
        diff_threshold=10,
    )
    assert len(merged["disagreements"]) == 1
    gap = merged["disagreements"][0]
    assert gap["dimension"] == "fidelity"
    assert gap["gap"] == 25
    assert gap["values"] == {"qwen": 95, "glm": 70}
    assert merged["agreement_rate"] == 0.75


def test_small_gap_is_not_a_disagreement():
    merged = merge_reviews(
        [
            _review("qwen", fidelity=90),
            _review("glm", fidelity=85),
        ],
        weights=WEIGHTS,
        diff_threshold=10,
    )
    assert merged["disagreements"] == []
    assert merged["agreement_rate"] == 1.0


def test_needs_retranslation_is_any():
    merged = merge_reviews(
        [_review("qwen", needs_retranslation=False), _review("glm", needs_retranslation=True)],
        weights=WEIGHTS,
    )
    assert merged["needs_retranslation"] is True


# ============================================
# 工具函数
# ============================================

def test_ordered_union_limits_and_strips_punctuation():
    lists = [["问题A。", "问题B"], ["问题A", "问题C"], ["问题B!"]]
    out = _ordered_union(lists, limit=2)
    assert len(out) == 2


def test_summarize_vote_texts():
    single = merge_reviews([_review("qwen", overall=88.0)], weights=WEIGHTS)
    assert "qwen 审校 88.0 分" == summarize_vote(single)

    vote = merge_reviews(
        [_review("qwen", overall=88.0, fidelity=90), _review("glm", overall=79.0, fidelity=70)],
        weights=WEIGHTS,
    )
    text = summarize_vote(vote)
    assert "双模型投票" in text and "qwen" in text and "glm" in text


# ============================================
# 配置：投票默认关闭；配了第二个 provider 才开，缺 key 时自动退化
# ============================================

def test_review_vote_is_off_by_default(monkeypatch):
    """默认（REVIEW_PROVIDERS 为空）不开投票——主线是"四节点四模型"，投票是可选增强。"""
    import config

    monkeypatch.setattr(config, "REVIEW_PROVIDERS", [], raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qwen-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://example.com/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", "glm-key", raising=False)
    assert config.active_review_providers() == []


def test_active_review_providers_skips_missing_key(monkeypatch):
    import config

    monkeypatch.setattr(config, "REVIEW_PROVIDERS", ["qwen", "glm"], raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qwen-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://example.com/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", None, raising=False)
    assert config.active_review_providers() == ["qwen"]


def test_active_review_providers_uses_two_when_both_keys_present(monkeypatch):
    import config

    monkeypatch.setattr(config, "REVIEW_PROVIDERS", ["qwen", "glm"], raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qwen-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://example.com/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", "glm-key", raising=False)
    assert config.active_review_providers() == ["qwen", "glm"]
    assert len(config.reviewer_model_names()) == 2