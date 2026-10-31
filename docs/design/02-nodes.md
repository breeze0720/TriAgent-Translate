# 02 · 六个专家节点：职责、prompt 与结构化输出

执行阶段的"智能"全在六个专家节点里：术语（terminology）、翻译（translate）、审校（review）、
润色（polish）、人工审核（human_review）、收尾（finalize）。本篇按"这行在做什么"拆开每个节点的
prompt（提示词）要点与输出契约；supervisor 已在第 00 篇讲过，这里只作为决策者出现。

对应代码：`workflow.py` 322-587 行、`agent.py`、`models.py`。

## 1. 实现概览

### 1.1 六个节点一页纸

| 节点 | 谁干活 | 输入（state 里读） | 输出（返回的键） | 失败策略 |
|------|--------|--------------------|------------------|----------|
| terminology | DeepSeek，1 次调用 | source_text、lang、style、domain | glossary、stage=terminology_done | 异常 → 空术语表继续，写 history |
| translate | DeepSeek，按段调用 | segments、glossary、review.issues、上一次译文 | translations、translation、retry_count+1 | 异常向上抛，由 runner 标失败 |
| review | Qwen，结构化输出 | source_text、translation、glossary | review（4 维 + issues）、review_unavailable | 连续 2 次解析失败 → ReviewUnavailableError |
| polish | DeepSeek，1 次调用 | translation、review.issues | polished、translation | 无 issues 直接跳过 |
| human_review | 人 | 当前译文、review、glossary | human_decision、translation | 用 `interrupt()` 暂停 |
| finalize | 无 LLM | 全部结果 | status=done/failed、error | 只判定，不写库 |

### 1.2 术语节点：可降级，但不静默

```python
version = _version_bump(state)
glossary: list[dict] = []
try:
    glossary = agent.extract_terms(source_text=..., source_lang=..., target_lang=...,
                                   style=..., domain=...)
    note = f"术语抽取完成：{len(glossary)} 个术语"
    if not glossary:
        note = "术语抽取完成：未发现需固定的术语，空表继续"
except Exception as exc:
    note = f"术语抽取失败（{type(exc).__name__}），降级为空术语表继续"
    print(f"[工作流] {note}: {exc}")

return {
    "glossary": normalize_glossary(glossary),   # 去空、按 term 去重、截断到 20 个
    "stage": "terminology_done",
    "version": version,
    "history": [_history("terminology", note)],
}
```

要点：术语缺失只影响一致性、不影响可交付，所以**降级继续**；但失败原因一定写进 history，
前端能看到"这次没有术语表"，而不是假装成功。`normalize_glossary()` 是术语抽取节点、
人工编辑术语表接口、DB 三处共用的唯一格式入口（`workflow.py` 145-170 行）。

### 1.3 翻译节点：重译要带"上一版 + 具体意见"

```python
retry_count = int(state.get("retry_count") or 0) + 1   # 每次进入就 +1，无条件（bug ① 修复点）
segments = state.get("segments") or split_segments(state.get("source_text", ""))
review = state.get("review") or {}
issues = (review.get("issues") or []) if retry_count > 1 else []   # 首译不带意见
previous = {int(item.get("index", -1)): str(item.get("text") or "")
            for item in (state.get("translations") or []) if isinstance(item, dict)}

for segment in segments:
    translated.append({"index": segment.get("index", 0), "source": segment.get("text", ""),
                       "text": agent.translate_segment(..., glossary=glossary,
                                                       review_issues=issues,
                                                       previous_translation=previous.get(segment.get("index", 0), "")),
                       "attempt": retry_count})
```

为什么按段调用而不是整篇：`SEGMENT_MAX_CHARS=300` 一段，模型不容易漏译、术语漂移也更容易定位；
重译时同一段能拿到上一版做对照。`attempt` 字段让前端能标"这段是第 2 版"。

`agent.translate_segment()` 内部有两个 prompt：

- 首译 `TRANSLATE_PROMPT`：注入术语表 + 风格/领域，要求"只输出译文本身"；
- 重译 `TRANSLATE_RETRY_PROMPT`：额外给 `previous_translation` 和逐条 `review_issues`，
  明确"只改有问题的部分，其他保持原样"。

### 1.4 审校节点：4 维 rubric（评分量表）+ 结构化输出

`models.py` 的输出契约（Pydantic，Python 数据校验库，schema 即字段与类型约束）：

```python
class ReviewResult(BaseModel):
    fidelity: int = Field(..., ge=0, le=100)        # 忠实度：不增不减、不曲解
    terminology: int = Field(..., ge=0, le=100)     # 术语一致性
    fluency: int = Field(..., ge=0, le=100)         # 流畅度
    completeness: int = Field(..., ge=0, le=100)    # 完整性：不漏句/漏信息
    issues: list[str] = Field(default_factory=list) # 位置 + 问题 + 怎么改，最多 8 条
    needs_retranslation: bool = False               # 只有"整段逻辑错"才 True
```

调用方式是 `get_qwen_llm().with_structured_output(ReviewResult, method="function_calling")`：
让模型按函数参数 schema 返回，解析失败会在库层暴露，而不是被正则兜底成一个假分数。

**总分不由模型给**，由代码按配置权重合成（`agent._build_review`）：

```python
weights = config.REVIEW_WEIGHTS      # fidelity 0.4 / terminology 0.25 / fluency 0.2 / completeness 0.15
total_weight = sum(weights.values()) or 1.0
overall = (result.fidelity * weights["fidelity"]
           + result.terminology * weights["terminology"]
           + result.fluency * weights["fluency"]
           + result.completeness * weights["completeness"]) / total_weight
```

四个维度这样设计的理由：翻译的失败模式基本就这四类（错译、术语不统一、译文生硬、漏内容）；
分开打分 + 权重写死，能回答"为什么是 84.65 分"。任务 #3 的真实数据是
`fidelity=88, terminology=80, fluency=76, completeness=95`，
算出来就是 `88×0.4 + 80×0.25 + 76×0.2 + 95×0.15 = 84.65`——每个数字都能拆开解释，
而不是"模型觉得还行"。术语是最容易硬性核对的，所以 prompt 里加了一条硬约束：
**术语表里的词译错，terminology 不得高于 60 分**。

`review_node` 的失败处理：

```python
try:
    review = agent.review_translation(...)          # 内部最多尝试 2 次结构化解析
    unavailable = False
except Exception as exc:
    review = {"fidelity": 0, "terminology": 0, "fluency": 0, "completeness": 0,
              "overall": 0.0, "issues": [f"审校不可用：{type(exc).__name__}: {exc}"],
              "needs_retranslation": False, "unavailable": True}
    unavailable = True                               # 绝不伪造 50 分
```

### 1.5 润色节点：只改 issues，不重译

```python
issues = (state.get("review") or {}).get("issues") or []
current = state.get("translation", "")
if not issues:
    return {"polished": current, "stage": "polished", ...}    # 没问题就跳过，省一次调用
polished = agent.polish_translation(source_text=..., translation=current,
                                    issues=issues, glossary=..., ...)
return {"polished": polished, "translation": polished, ...}    # 最终交付用润色版
```

`POLISHER_PROMPT` 的三条硬要求：只改问题相关句子、不增删信息、术语保持术语表译法；
输出只给完整译文，不解释改了哪里（方便直接落库/展示）。

### 1.6 人工审核与收尾

`human_review_node` 把展示载荷交给 `interrupt()` 暂停（第 04 篇详述）；
`finalize_node` **不调用任何模型**，只按下表判定终态（优先级从上到下）：

| 条件 | status | error |
|------|--------|-------|
| 译文为空 | failed | 最终译文为空 |
| 人工通过 | done | （空） |
| overall ≥ PASS_SCORE 且审校可用 | done | （空） |
| 人工明确驳回 | failed | 人工审核驳回，且已无剩余重试次数 |
| 审校持续不可用 | failed | 审校模型持续不可用，任务未通过质量校验 |
| 其余（低分耗尽预算） | failed | 审校总分 X 低于阈值 80，已翻译 N 次仍未达标 |

## 2. 设计取舍与备选方案

### 2.1 审校：从"正则抠 JSON + 默认 50 分"改成结构化输出

v1 的写法（真实旧代码，`_archive/old-annotate_backup/02-多智能体翻译-LangGraph__agent.py` 177-203 行）：

```python
# ❌ 三层正则兜底，全部失败就 return 50.0
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
    if json_match:
        try: data = json.loads(json_match.group(1))
        except json.JSONDecodeError: return 50.0, raw[:500]     # ← 假分数
    else:
        ...
        return 50.0, raw[:500]
```

问题不在于"50 分不好"，而在于**它把"模型没按格式返回"伪装成"翻译质量中等"**：
做评测时这些假分数会混进真实分数里，重试决策也会基于假数据。
现在的策略是：结构化解析失败 → 重试 1 次 → 仍失败就抛 `ReviewUnavailableError`，
节点标记 `review_unavailable=True`，supervisor 只允许"再试一次"，然后转人工或失败。

### 2.2 润色：整篇重写 vs 定向修改

```python
# ❌ 每次都整篇重译/重写：贵，而且会把已经通过的句子改坏
polished = llm("请润色这篇译文：" + translation)
```

本项目只在"审校通过但还有具体 issues"时才润色，而且把 issues 逐条喂给模型。
好处是省 token、改动可对照；代价是模型可能"只改列表里的问题"而忽略列表外的语病——
所以我们不指望润色提升质量分，它只是收尾。真正的质量闸门在审校和重试。

### 2.3 prompt 的四个工程细节

1. **术语表只渲染一处**：`format_glossary()` 同时被翻译、审校、润色调用，三处看到同一份约束；
2. **能省就省**：`_style_block()` 在 style/domain 为空时返回空串，不占 prompt 预算；
3. **空结果显式定义**：术语 prompt 明确"没有值得固定的术语时返回空数组"，否则模型会硬凑几个出来；
4. **输出纯度要求**："只输出译文/只输出完整译文，不要解释"——下游要直接落库和展示，混进"好的，以下是译文："会让分段渲染和导出全错。

### 2.4 诚实的取舍

- 4 维权重（0.4 / 0.25 / 0.2 / 0.15）是手工拍的，不是学出来的；换领域应该重新标定；
- 审校只用一个模型（Qwen），存在模型自身偏好；理论上可以多模型投票，但成本和延迟翻倍；
- 结构化输出会增加 token 和延迟（任务 #3 单次审校约 26 秒，任务 #4 约 72 秒），换来的是"分数可解释、失败可识别"；
- `needs_retranslation` 字段目前只做记录，真正的重译决策看 `overall < 80` 与重试预算，
  没有把它接进分支（接进去会更精细，但也更容易出现"小修却整篇重译"）。

## 3. 常见问题（FAQ）

**Q1：你怎么保证审校分数不是模型随便给的？**
A：让模型只输出 4 个维度的分数和可核对的问题清单，总分由代码按固定权重合成；
解析失败不兜底假分数，而是标记不可用并降级处理，所以分数可解释、数据不被污染。

**Q2：润色和重译有什么区别？**
A：重译会带着审校意见重新翻译、会消耗重试预算；润色只针对已通过译文的具体 issues 做定向修改，
不重新翻译、不消耗预算，也尽量避免改动已经正确的句子。

**Q3：模型输出不符合 schema 怎么办？**
A：先自动重试一次并追加格式提醒，仍失败就抛 `ReviewUnavailableError`，
节点写 `review_unavailable=True`，supervisor 最多再试一轮，然后转人工或按失败收尾。

---

**一句话总结**：把"智能"关进 schema 和预算里——每个节点只输出约定好的字段，失败就走明确的降级路径。