"""
agent.py - 三个翻译 Agent 定义 + 邮件发送工具
使用 LangChain ChatOpenAI 对接 DeepSeek 和 Qwen 两个大模型。
每个 Agent 有明确的角色提示词，通过 prompt engineering 控制输出质量。
"""
import smtplib
import json
import re
from email.mime.text import MIMEText

from langchain_openai import ChatOpenAI

import config

# ============================================
# LLM 实例（模块级单例，复用连接）
# ============================================

# DeepSeek 模型 — 负责翻译和润色（中文翻译能力强，成本低）
deepseek_llm = ChatOpenAI(
    model=config.DEEPSEEK_MODEL_NAME,
    api_key=config.DEEPSEEK_API_KEY,
    base_url=config.DEEPSEEK_BASE_URL,
    temperature=0.3,   # 低温度保证翻译一致性
    max_tokens=4096,
)

# Qwen 模型 — 负责审校（推理和评判能力强，善于发现细微错误）
qwen_llm = ChatOpenAI(
    model=config.QWEN_MODEL_NAME,
    api_key=config.QWEN_API_KEY,
    base_url=config.QWEN_BASE_URL,
    temperature=0.2,   # 审校需要更严谨，温度更低
    max_tokens=4096,
)


# ============================================
# 翻译 Agent（DeepSeek）
# ============================================

TRANSLATOR_PROMPT = """你是一位专业的翻译专家，精通{source_lang}和{target_lang}之间的互译。

## 你的任务
将以下{source_lang}文本翻译成{target_lang}。

## 翻译要求
1. 准确传达原文意思，不增不减
2. 符合{target_lang}的表达习惯，读起来自然流畅
3. 专业术语要准确翻译
4. 保持原文的语气和风格
5. 只输出翻译结果，不要添加任何解释或说明

## 待翻译文本
{source_text}

## 翻译结果
"""

TRANSLATOR_RETRY_PROMPT = """你是一位专业的翻译专家，精通{source_lang}和{target_lang}之间的互译。

## 你的任务
根据审校专家的反馈，重新翻译以下文本。

## 原文
{source_text}

## 上一版翻译
{previous_translation}

## 审校意见
{review_feedback}

## 修改要求
请针对审校意见中指出的问题进行针对性修正，确保翻译质量。只输出修正后的翻译结果。

## 修正后的翻译
"""


def translate(source_text: str, source_lang: str, target_lang: str,
              review_feedback: str = "", previous_translation: str = "") -> str:
    """
    调用翻译 Agent，将原文翻译为目标语言。

    参数:
        source_text: 原文
        source_lang: 源语言（如 zh、en、ja）
        target_lang: 目标语言
        review_feedback: 审校意见（首次翻译时为空）
        previous_translation: 上一版翻译（首次翻译时为空）

    返回:
        翻译结果文本
    """
    # 根据是否有审校反馈，选择不同的提示词
    if review_feedback and previous_translation:
        prompt = TRANSLATOR_RETRY_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            source_text=source_text,
            previous_translation=previous_translation,
            review_feedback=review_feedback,
        )
        print(f"[翻译Agent] 根据审校意见重新翻译...")
    else:
        prompt = TRANSLATOR_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            source_text=source_text,
        )
        print(f"[翻译Agent] 开始翻译: {source_lang} → {target_lang}")

    response = deepseek_llm.invoke(prompt)
    result = response.content.strip()
    print(f"[翻译Agent] 翻译完成，长度: {len(result)} 字符")
    return result


# ============================================
# 审校 Agent（Qwen）
# ============================================

REVIEWER_PROMPT = """你是一位严格的翻译审校专家，精通{source_lang}和{target_lang}。

## 你的任务
审校以下翻译的质量，给出评分和具体修改建议。

## 原文（{source_lang}）
{source_text}

## 译文（{target_lang}）
{translation}

## 评分标准（0-100分）
- 90-100：翻译精准，表达地道，无需修改
- 80-89：基本准确，有小的改进空间
- 60-79：存在语法或用词问题，需要修改
- 0-59：严重错误，需要重新翻译

## 输出格式（严格遵守）
请按以下JSON格式输出，不要添加其他内容：
```json
{{
    "score": <0-100的整数>,
    "feedback": "<具体的修改建议，指出问题所在并给出改进方向>"
}}
```"""


def review(source_text: str, translation: str, source_lang: str, target_lang: str) -> tuple[float, str]:
    """
    调用审校 Agent，对翻译结果进行质量评估。

    参数:
        source_text: 原文
        translation: 译文
        source_lang: 源语言
        target_lang: 目标语言

    返回:
        (score, feedback) — 评分（0-100）和审校意见
    """
    print(f"[审校Agent] 开始审校翻译质量...")

    prompt = REVIEWER_PROMPT.format(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        translation=translation,
    )

    response = qwen_llm.invoke(prompt)
    raw = response.content.strip()
    print(f"[审校Agent] 原始响应: {raw[:200]}...")

    # 解析 JSON（模型可能包裹在 markdown 代码块中）
    try:
        # 尝试直接解析
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试从 markdown 代码块中提取 JSON
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                print(f"[审校Agent] JSON解析失败，使用默认低分")
                return 50.0, raw[:500]
        else:
            # 尝试找花括号包裹的内容
            brace_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if brace_match:
                try:
                    data = json.loads(brace_match.group())
                except json.JSONDecodeError:
                    print(f"[审校Agent] JSON解析失败，使用默认低分")
                    return 50.0, raw[:500]
            else:
                print(f"[审校Agent] 无法从响应中提取JSON，使用默认低分")
                return 50.0, raw[:500]

    score = float(data.get("score", 50))
    feedback = data.get("feedback", "")
    print(f"[审校Agent] 审校完成 - 评分: {score}/100")
    return score, feedback


# ============================================
# 润色 Agent（DeepSeek）
# ============================================

POLISHER_PROMPT = """你是一位专业的文字润色师，精通{target_lang}的写作。

## 你的任务
对以下翻译进行最终润色，提升文字的流畅度和文学性。

## 原文（参考）
{source_text}

## 已审校通过的译文
{translation}

## 审校意见（已确认通过）
{review_feedback}

## 润色要求
1. 在保持翻译准确的前提下，提升文字的美感
2. 优化句式结构，让表达更自然
3. 使用更地道、更优雅的词汇
4. 保持原文的语气和风格不变
5. 只输出润色后的最终文本，不要添加任何解释或说明

## 润色结果
"""


def polish(source_text: str, translation: str, review_feedback: str,
           source_lang: str, target_lang: str) -> str:
    """
    调用润色 Agent，对审校通过的翻译进行最终润色。

    参数:
        source_text: 原文
        translation: 已审校通过的译文
        review_feedback: 审校意见
        source_lang: 源语言
        target_lang: 目标语言

    返回:
        润色后的文本
    """
    print(f"[润色Agent] 开始润色...")

    prompt = POLISHER_PROMPT.format(
        target_lang=target_lang,
        source_text=source_text,
        translation=translation,
        review_feedback=review_feedback,
    )

    response = deepseek_llm.invoke(prompt)
    result = response.content.strip()
    print(f"[润色Agent] 润色完成，长度: {len(result)} 字符")
    return result


# ============================================
# 邮件发送工具（复用项目一的逻辑）
# ============================================

def send_email(to: str, subject: str, content: str) -> tuple[bool, str]:
    """
    通过 QQ 邮箱 SMTP 发送邮件。

    参数:
        to: 收件人邮箱地址
        subject: 邮件主题
        content: 邮件正文（纯文本）

    返回:
        (success, message) — 是否成功 + 提示信息
    """
    print(f"[邮件工具] 发送邮件 -> 收件人: {to}, 主题: {subject}")

    # 检查邮箱配置
    if not config.EMAIL_FROM or not config.EMAIL_PASSWORD:
        return False, "邮件发送失败：未配置发件人邮箱，请在 .env 文件中填写 EMAIL_FROM 和 EMAIL_PASSWORD"

    try:
        # 构建邮件对象（纯文本格式）
        msg = MIMEText(content, "plain", "utf-8")
        msg["To"] = to
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_FROM

        # 连接 QQ 邮箱 SMTP 服务器（SSL 加密，端口 465）
        smtp = smtplib.SMTP_SSL(config.EMAIL_HOST, port=465)
        smtp.login(config.EMAIL_FROM, config.EMAIL_PASSWORD)
        smtp.sendmail(config.EMAIL_FROM, to, msg.as_string())
        smtp.quit()

        print(f"[邮件工具] 邮件发送成功 -> {to}")
        return True, f"邮件已成功发送到 {to}"

    except smtplib.SMTPAuthenticationError:
        return False, "邮件发送失败：QQ邮箱认证失败，请检查 EMAIL_PASSWORD（需使用授权码，非QQ密码）"
    except Exception as e:
        print(f"[邮件工具] 邮件发送失败: {e}")
        return False, f"邮件发送失败: {str(e)}"
