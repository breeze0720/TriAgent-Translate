# 08 · 四节点四模型（附：可选的双模型审校投票）

> 对应代码：`config.py`（`NODE_MODELS` / `NODE_MODEL_FALLBACKS` / `resolve_node_model`）、
> `agent.py`（`get_llm` / `llm_for` / `NODE_TEMPERATURE`）、`main.py`（启动日志与 `/status` 的 `node_models`）、
> `tests/test_model_routing.py`（8 个用例）、`core/review_vote.py`（可选投票合并，14 个用例）

## 1. 实现概览

一句话：**四个专家节点不是共用一个模型，而是各自配一个模型，缺 key 时按各自的候选链降级，并把"实际生效的是哪个"打印出来。**

### 1.1 配置长什么样

```ini
# .env
MODEL_TERMINOLOGY=glm:glm-4-flash       # 术语抽取：结构化抽取，量大、判错代价低
MODEL_TRANSLATE=deepseek:deepseek-chat  # 翻译：语言质量是核心指标
MODEL_REVIEW=qwen:qwen3.7-max           # 审校：评判只调一次，但要判断力
MODEL_POLISH=glm:glm-4.6                # 润色：长文改写，要语感
```

格式是 `provider:model`。三个 provider 都是 OpenAI 兼容端点，所以**一个 `ChatOpenAI` 类通吃**：

```python
# config.py
PROVIDER_DEFAULT_MODEL = {"deepseek": DEEPSEEK_MODEL_NAME, "qwen": QWEN_MODEL_NAME, "glm": GLM_MODEL_NAME}

def provider_credentials(provider: str) -> dict:
    if provider == "deepseek":
        return {"api_key": DEEPSEEK_API_KEY, "base_url": DEEPSEEK_BASE_URL,
                "max_tokens": DEEPSEEK_MAX_TOKENS, "timeout": DEEPSEEK_TIMEOUT}
    if provider == "qwen": ...
    if provider == "glm": ...
    raise KeyError(f"未知的模型 provider: {provider}")
```

### 1.2 客户端：缓存 key 必须带 model

```python
# agent.py
_LLM_CACHE: dict[tuple, ChatOpenAI] = {}
_LLM_LOCK = threading.Lock()

def get_llm(provider: str, model: str, temperature: float = 0.2) -> ChatOpenAI:
    key = (provider, model, temperature)
    with _LLM_LOCK:
        llm = _LLM_CACHE.get(key)
        if llm is None:
            cred = config.provider_credentials(provider)
            if not (cred["api_key"] and cred["base_url"]):
                raise RuntimeError(f"{provider} 未配置 API Key：请在 .env 里补 {provider.upper()}_API_KEY")
            llm = ChatOpenAI(model=model, api_key=cred["api_key"], base_url=cred["base_url"],
                             temperature=temperature, max_tokens=cred["max_tokens"], timeout=cred["timeout"])
            _LLM_CACHE[key] = llm
        return llm

def llm_for(node: str) -> ChatOpenAI:
    info = config.resolve_node_model(node)          # 含"缺 key 降级"逻辑
    return get_llm(info["provider"], info["model"], temperature=NODE_TEMPERATURE.get(node, 0.2))
```

两个细节：
- **缓存 key 里带 `model`**：术语用 `glm-4-flash`、润色用 `glm-4.6`，两者 provider 相同但模型不同，不能复用同一个客户端对象。
- **温度按节点区分**：抽取/评判 `0.2`（要稳），生成/润色 `0.3`（要一点灵活性），统一在 `NODE_TEMPERATURE` 里。

节点里的调用点就一行，替换掉原来硬编码的 `get_deepseek_llm()`：

```python
structured = llm_for("terminology").with_structured_output(Glossary, method="function_calling")  # 术语
content = llm_for("translate").invoke(prompt)                                                    # 翻译
review  = _review_once(info["provider"], info["model"], prompt)                                  # 审校
content = llm_for("polish").invoke(prompt)                                                       # 润色
```

### 1.3 降级：缺 key 时"换一个模型"，但必须让人看见

```python
# config.py
NODE_MODEL_FALLBACKS = {
    "terminology": ["qwen:qwen-turbo", "deepseek:deepseek-chat"],
    "translate":   ["deepseek:deepseek-chat", "qwen:qwen-plus"],
    "review":      ["qwen:qwen3.7-max", "deepseek:deepseek-chat"],
    "polish":      ["qwen:qwen-plus", "deepseek:deepseek-chat"],
}

def resolve_node_model(node: str) -> dict:
    requested = NODE_MODELS[node]
    provider, model = split_model_spec(requested)
    if not provider_available(provider):
        for alt_spec in NODE_MODEL_FALLBACKS.get(node, []):
            alt_provider, alt_model = split_model_spec(alt_spec)
            if alt_provider != provider and provider_available(alt_provider):
                return {"node": node, "provider": alt_provider, "model": alt_model,
                        "requested": requested, "fallback": True}      # ← 关键：标记降级
        ...
    return {"node": node, "provider": provider, "model": model or PROVIDER_DEFAULT_MODEL[provider],
            "requested": requested, "fallback": False}
```

**为什么是"每个节点自己的候选链"而不是全局一个顺序**：

如果缺 GLM 时所有节点都往 DeepSeek 掉，四个节点会退化成"一个模型"——配置上写着四模型，实际只有一个，这是最容易出问题、也最容易被审查发现的地方。所以术语掉到 `qwen-turbo`、润色掉到 `qwen-plus`，**只要有两家 key，仍然能保持四个不同模型**。

**为什么降级必须 `fallback=True` 并且打印**：

不打印的话，"我配了四个模型"就变成自我安慰；打印出来才能自查，也顺便证明降级逻辑真的生效了：

```text
[Startup] 四节点模型：术语=qwen-turbo* · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=qwen-plus*
[Startup] ⚠ 以下节点因缺少对应 provider 的 Key 已降级：terminology→qwen-turbo, polish→qwen-plus（.env 里补 key 即自动生效）
[init] 初始化：原文切分为 1 段，进入术语抽取；模型分工：术语=qwen-turbo* · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=qwen-plus*
```

### 1.4 时间线第一行就是"证据"

`supervisor` 节点初始化时把模型分工写进事件消息，前端时间线第一行就能看到：

```
[init] 初始化：原文切分为 1 段，进入术语抽取；模型分工：术语=qwen-turbo* · 翻译=deepseek-chat · 审校=qwen3.7-max · 润色=qwen-plus*
```

`/status` 也返回结构化字段，方便脚本核对：

```json
{"translator_model": "deepseek-chat", "reviewer_model": "qwen3.7-max",
 "node_models": {"terminology": "qwen-turbo", "translate": "deepseek-chat",
                 "review": "qwen3.7-max", "polish": "qwen-plus"}}
```

## 2. 设计取舍与备选方案

### 错写法 1：四个节点全用同一个模型

```python
llm = ChatOpenAI(model="deepseek-chat")   # 然后四个节点都用它
```

能跑，但讲不出"模型选型"这件事：术语抽取不需要花大价钱用强模型，审校却恰恰最需要判断力。同一模型意味着要么**贵**（用强模型做抽取）要么**弱**（用便宜模型做评判）。按能力分配才是工程上正确的做法——这也正是题目里"四个专家"的合理落地。

### 错写法 2：新建四个全局客户端变量

```python
_terminology_llm = ChatOpenAI(...)   # 模块级写死
_translate_llm = ...
```

问题：配置改一次就要改四处代码；而且"哪个 key 没配"在 import 阶段就炸，启动即崩，排障体验极差。改成 `(provider, model, temperature)` 三元组缓存 + 运行时解析，配置全部集中在 `.env`。

### 错写法 3：缺 key 时静默 fallback

```python
try:
    llm = client_for("glm")
except Exception:
    llm = deepseek_client   # 静默降级，日志里什么都没有
```

问题：你以为自己在演示"四模型"，实际全走了一个模型，而且**没有任何证据能反驳**。正确做法是降级 + 标记 + 打印 + 落进时间线，让"实际用了什么"始终可见。

### 错写法 4：把 `provider` 拼进 model 名

```python
ChatOpenAI(model=f"{provider}::{model}", ...)   # 错：真实的 model 名不带前缀
```

问题：不同厂商的模型名规则不同，硬拼会 404。`provider` 只用来选 **base_url + api_key**，`model` 原样传给厂商——这是 OpenAI 兼容协议的好处。

> 实测（真实 API，只有 DeepSeek + Qwen 两家 key）：任务 #11 走 `qwen-turbo → deepseek-chat → qwen3.7-max → qwen-plus` 四个不同模型，6 个术语，94.0 分；日志里两项带 `*`，明确标注了"因缺 GLM key 而降级"。

## 3. 常见问题（FAQ）

**Q1：为什么四个节点要用四个不同的模型？一个模型不行吗？**
一句话：不同环节要的能力不同——抽取要便宜、翻译要语言质量、评判要判断力、润色要语感；同一模型要么贵要么弱。
展开：术语抽取是结构化抽取，量大但判错代价低，用便宜快模型（glm-4-flash / qwen-turbo）就够；翻译是核心产出，用生成主力；审校只调一次但直接决定质量分，值得用思考型模型（qwen3.7-max）；润色是长文改写，语感优先。这套映射既写在配置里（`MODEL_*`），也有"实际生效值"可核对（日志、时间线、`/status`）。

**Q2：某个厂商的 key 没配，或者服务挂了怎么办？**
一句话：每个节点有自己的降级候选链，缺 key 时换到下一个可用模型，并标记 `fallback=True` 打印出来。
展开：全局只按一个顺序降级会让四个节点退化成同一个模型（配置四模型、实际一模型，最容易被追问），所以候选链按节点设计：术语降 `qwen-turbo`、润色降 `qwen-plus`——只有两家 key 也能保住"四个不同模型"。降级是显式可观测的：启动日志带 `*`、时间线第一行写明、`/status` 的 `node_models` 可查。

**Q3：多模型是不是意味着成本和延迟翻倍？**
一句话：成本按"每节点一个模型"算，延迟不叠加——四个节点本来就是串行执行，各自调一次，和"一个模型调四次"的调用次数完全相同。
展开：区别只在单价，而单价是按环节选的（抽取用最便宜的）。真正会翻倍的是"同一环节并行问两个模型"（见下面的可选投票）。

**Q4（进阶）：那为什么审校还留了双模型投票？**
一句话：生成侧同模型保证风格一致；只有"评判"环节，异质模型的**分歧**才有额外价值，所以投票是可选项，默认关。
展开：设 `REVIEW_PROVIDERS=qwen,glm` 后，审校会并行问两家，每维取严、issues 并集、分差 ≥10 记 `disagreement`，一致率落库可当质量指标；一家挂了不影响出分（`failed_providers` 记录原因）。合并逻辑是纯函数，14 个用例覆盖。

## 附：可选的双模型审校投票（默认关闭）

```ini
# .env：两个 provider 都要有 key，否则自动退化为单模型（不会报错）
REVIEW_PROVIDERS=qwen,glm
REVIEW_VOTE_DIFF=10
```

- **合并规则**（`core/review_vote.py`）：每维取较低分（取严）→ overall 用合并后的维度分按权重重算 → issues 并集按"被几家提到"排序（最多 8 条）→ 分差 ≥ 阈值记 `disagreement`，`agreement_rate = 1 - 分歧数/4`。
- **并行 + 失败隔离**：`ThreadPoolExecutor` 同时问两家，延迟 ≈ 较慢的那个；一家 429/超时不影响出分，原因写进 `failed_providers`。
- **实测**：任务 #10 两个模型都成功 → `vote_mode=vote`，4/4 维度一致，时间线 `审校完成（双模型投票）：98.6 分（qwen 98.6 / glm 98.6），4/4 维度一致，分歧 0 项，问题 2 条`；任务 #9 第二个审校 429 → 自动退化单模型，照常 94.2 分完成。