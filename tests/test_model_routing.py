"""
四节点四模型的「路由与降级」测试。

为什么这些用例重要：
  "四个专家用四个模型"最容易变成配置写了四个、实际全走了同一个——
  因为某一个 provider 的 key 没配、或者解析写错，程序照样能跑，看不出问题。
  这些用例断言的是"配置 → 实际生效模型"的映射，以及缺 key 时必须显式标记降级（fallback=True）。
"""
import pytest

import config
from core.review_vote import merge_reviews  # noqa: F401  （确保 core 包可导入，路由测试不依赖它）


# ============================================
# 配置解析
# ============================================

def test_split_model_spec():
    assert config.split_model_spec("glm:glm-4-flash") == ("glm", "glm-4-flash")
    assert config.split_model_spec("deepseek") == ("deepseek", None)
    assert config.split_model_spec(" Qwen:qwen-plus ") == ("qwen", "qwen-plus")


def test_defaults_are_four_distinct_models():
    """默认配置必须是四个不同的模型名，否则"四模型"就名不副实。"""
    models = [config.split_model_spec(config.NODE_MODEL_DEFAULTS[n])[1] for n in ("terminology", "translate", "review", "polish")]
    assert all(models), models
    assert len(set(models)) == 4, f"默认四节点模型有重复: {models}"


# ============================================
# 有 key：按配置生效
# ============================================

@pytest.fixture
def all_keys(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "ds-key", raising=False)
    monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", "https://ds.example/v1", raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qw-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://qw.example/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", "glm-key", raising=False)


def test_resolve_uses_configured_provider_and_model(all_keys, monkeypatch):
    monkeypatch.setattr(config, "NODE_MODELS", {
        "terminology": "glm:glm-4-flash",
        "translate": "deepseek:deepseek-chat",
        "review": "qwen:qwen3.7-max",
        "polish": "glm:glm-4.6",
    }, raising=False)
    plan = config.node_model_plan()
    assert plan["terminology"] == {
        "node": "terminology", "provider": "glm", "model": "glm-4-flash",
        "requested": "glm:glm-4-flash", "fallback": False,
    }
    assert plan["translate"]["model"] == "deepseek-chat"
    assert plan["review"]["model"] == "qwen3.7-max"
    assert plan["polish"]["model"] == "glm-4.6"
    assert all(not info["fallback"] for info in plan.values())


def test_summary_mentions_four_nodes(all_keys, monkeypatch):
    monkeypatch.setattr(config, "NODE_MODELS", {
        "terminology": "glm:glm-4-flash",
        "translate": "deepseek:deepseek-chat",
        "review": "qwen:qwen3.7-max",
        "polish": "glm:glm-4.6",
    }, raising=False)
    summary = config.node_model_summary()
    for word in ("术语=", "翻译=", "审校=", "润色="):
        assert word in summary, summary
    assert "glm-4-flash" in summary and "qwen3.7-max" in summary


# ============================================
# 缺 key：降级必须显式、可观测
# ============================================

def test_missing_glm_key_falls_back_and_marks_it(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "ds-key", raising=False)
    monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", "https://ds.example/v1", raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qw-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://qw.example/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", None, raising=False)
    monkeypatch.setattr(config, "NODE_MODELS", {
        "terminology": "glm:glm-4-flash",
        "translate": "deepseek:deepseek-chat",
        "review": "qwen:qwen3.7-max",
        "polish": "glm:glm-4.6",
    }, raising=False)

    plan = config.node_model_plan()
    # 术语/润色配的是 GLM，没 key → 走各自节点的降级候选（术语用 qwen-turbo，润色用 qwen-plus），
    # 保证"四个节点仍是四个不同模型"，且 fallback=True（不静默）
    assert plan["terminology"]["provider"] == "qwen"
    assert plan["terminology"]["model"] == "qwen-turbo"
    assert plan["terminology"]["fallback"] is True
    assert plan["terminology"]["requested"] == "glm:glm-4-flash"
    assert plan["polish"]["model"] == "qwen-plus"
    assert plan["polish"]["fallback"] is True
    # 翻译/审校自己的 provider 有 key，不受影响
    assert plan["translate"]["fallback"] is False
    assert plan["review"]["fallback"] is False


def test_only_two_providers_still_gives_four_distinct_models(monkeypatch):
    """只有 DeepSeek + Qwen 两家 key 时，降级后仍然应该是四个不同模型（这是这次改动的核心目标）。"""
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "ds-key", raising=False)
    monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", "https://ds.example/v1", raising=False)
    monkeypatch.setattr(config, "QWEN_API_KEY", "qw-key", raising=False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "https://qw.example/v1", raising=False)
    monkeypatch.setattr(config, "GLM_API_KEY", None, raising=False)
    models = [info["model"] for info in config.node_model_plan().values()]
    assert len(set(models)) == 4, models


def test_no_keys_at_all_does_not_crash(monkeypatch):
    for attr in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "GLM_API_KEY"):
        monkeypatch.setattr(config, attr, None, raising=False)
    plan = config.node_model_plan()
    # 不抛异常，仍然返回"配置里写的模型"，由真正调用时报可读的鉴权错误
    assert plan["translate"]["model"] == "deepseek-chat"
    assert all(info["fallback"] is False for info in plan.values())


# ============================================
# 客户端实例：不同节点必须拿到不同 model 的客户端
# ============================================

def test_llm_for_returns_client_with_expected_model(all_keys, monkeypatch):
    import agent

    monkeypatch.setattr(config, "NODE_MODELS", {
        "terminology": "glm:glm-4-flash",
        "translate": "deepseek:deepseek-chat",
        "review": "qwen:qwen3.7-max",
        "polish": "glm:glm-4.6",
    }, raising=False)
    assert agent.llm_for("terminology").model_name == "glm-4-flash"
    assert agent.llm_for("translate").model_name == "deepseek-chat"
    assert agent.llm_for("review").model_name == "qwen3.7-max"
    assert agent.llm_for("polish").model_name == "glm-4.6"
    # 两个都用 GLM provider，但模型不同 → 必须是两个不同的客户端实例（缓存 key 带 model）
    assert agent.llm_for("terminology") is not agent.llm_for("polish")