"""
core/review_vote.py - 双模型审校的「投票合并」纯函数

为什么单独拆一个模块：
  合并逻辑（取严、并集、分歧检测、一致率）是纯计算，不碰网络和数据库，
  可以完全用单测覆盖——这是「多模型」里唯一能被确定性验证的部分，
  面试时说"合并策略有 7 个测试用例"比说"我调了两个模型"有说服力得多。

合并策略（刻意保守）：
  1. 每个维度取两个模型的**较低分**（取严）——审校是用来卡质量的，
     宁可误判一次让人看，也不要把有明显问题的译文放过去；
  2. overall 不取两者平均，而是用合并后的维度分按配置权重重算，
     保证"总分能由维度分拆解出来"这个性质不被破坏；
  3. issues 取并集去重（两家都提的排前面），最多 8 条；
  4. 同一维度分差 ≥ REVIEW_VOTE_DIFF 记为 disagreement，供人工审核优先看；
  5. needs_retranslation 取「任一为真」。
"""
import re

DIMENSIONS = ("fidelity", "terminology", "fluency", "completeness")

DIM_CN = {
    "fidelity": "忠实度",
    "terminology": "术语一致性",
    "fluency": "流畅度",
    "completeness": "完整性",
}


def _norm_issue(text) -> str:
    """把 issue 归一化用于去重：去空白、去标点差异、转小写。"""
    s = str(text or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[。，、；：！？,.;:!?]", "", s)
    return s.lower()


def _ordered_union(issue_lists: list[list[str]], limit: int = 8) -> list[str]:
    """
    有序并集：先按「被几个模型提到」降序，再按出现顺序。
    两个模型都指出的问题排在前面，因为它们更可能是真问题。
    """
    count: dict[str, int] = {}
    first_text: dict[str, str] = {}
    order: dict[str, int] = {}
    for issues in issue_lists:
        for issue in issues or []:
            key = _norm_issue(issue)
            if not key:
                continue
            count[key] = count.get(key, 0) + 1
            first_text.setdefault(key, str(issue).strip()[:500])
            order.setdefault(key, len(order))
    keys = sorted(count, key=lambda k: (-count[k], order[k]))
    return [first_text[k] for k in keys[:limit]]


def merge_reviews(
    reviews: list[dict],
    weights: dict | None = None,
    diff_threshold: int = 10,
) -> dict:
    """
    合并多个模型的审校结果。

    reviews: [{"provider": "qwen", "fidelity": 88, ..., "overall": 85.2, "issues": [...],
               "needs_retranslation": False, "unavailable": False}, ...]
    返回的结构与单模型 review 完全一致（多出 providers / disagreements / agreement_rate 等字段），
    这样前端和下游节点不需要改任何解析逻辑。
    """
    weights = weights or {"fidelity": 0.4, "terminology": 0.25, "fluency": 0.2, "completeness": 0.15}
    usable = [r for r in reviews if r and not r.get("unavailable") and isinstance(r.get("fidelity"), (int, float))]
    if not usable:
        raise ValueError("没有可用的审校结果（全部 unavailable）")

    # ---- 单模型：原样返回，只补上 provider 标记，行为与旧版完全一致 ----
    if len(usable) == 1:
        only = dict(usable[0])
        only.setdefault("providers", [str(only.get("provider") or "single")])
        only["disagreements"] = []
        only["agreement_rate"] = 1.0
        only["vote_mode"] = "single"
        return only

    providers = [str(r.get("provider") or f"model{i}") for i, r in enumerate(usable)]

    # ---- 维度取严 ----
    merged_dims = {dim: min(int(r[dim]) for r in usable) for dim in DIMENSIONS}

    total_weight = sum(weights.values()) or 1.0
    overall = sum(merged_dims[d] * weights.get(d, 0.0) for d in DIMENSIONS) / total_weight

    # ---- 分歧检测 ----
    disagreements = []
    for dim in DIMENSIONS:
        values = {providers[i]: int(r[dim]) for i, r in enumerate(usable)}
        gap = max(values.values()) - min(values.values())
        if gap >= diff_threshold:
            disagreements.append(
                {
                    "dimension": dim,
                    "dimension_cn": DIM_CN.get(dim, dim),
                    "values": values,
                    "gap": gap,
                }
            )

    agreements_total = len(DIMENSIONS)
    agreement_rate = round((agreements_total - len(disagreements)) / agreements_total, 2)

    return {
        **merged_dims,
        "overall": round(float(overall), 2),
        "issues": _ordered_union([r.get("issues") or [] for r in usable]),
        "needs_retranslation": any(bool(r.get("needs_retranslation")) for r in usable),
        "unavailable": False,
        "providers": providers,
        "model_scores": {providers[i]: round(float(r.get("overall") or 0), 2) for i, r in enumerate(usable)},
        "dimension_scores": {
            dim: {providers[i]: int(r[dim]) for i, r in enumerate(usable)} for dim in DIMENSIONS
        },
        "disagreements": disagreements,
        "agreement_rate": agreement_rate,
        "vote_mode": "vote",
    }


def summarize_vote(review: dict) -> str:
    """给时间线/日志用的一句话摘要，例如：双模型审校 82.5 分（qwen 88 / glm 79），3/4 维度一致。"""
    mode = review.get("vote_mode")
    if mode != "vote":
        providers = review.get("providers") or ["single"]
        return f"{providers[0]} 审校 {review.get('overall')} 分"
    scores = review.get("model_scores") or {}
    detail = " / ".join(f"{k} {v}" for k, v in scores.items())
    agree = round(float(review.get("agreement_rate") or 0) * 4)
    return f"双模型投票 {review.get('overall')} 分（{detail}），{agree}/4 维度一致"