<template>
    <!-- ============================================
      ResultCard.vue - Step 3: Final Results
      Displays all stage outputs with expand/collapse.
      Includes email sending and "new translation" button.
      ============================================ -->
    <div class="result-root fade-in">
        <!-- Failed state -->
        <div v-if="task.status === 'failed'" class="result-banner failed">
            <span class="banner-icon">⚠️</span>
            <div>
                <div class="banner-title">翻译未达标</div>
                <div class="banner-sub">审校评分低于 80 分，3 次重试后仍未通过</div>
            </div>
        </div>

        <!-- Success state -->
        <div v-else class="result-banner success">
            <span class="banner-icon">✅</span>
            <div>
                <div class="banner-title">翻译完成</div>
                <div class="banner-sub">
                    审校评分 {{ task.review_score }}/100 · 共 {{ task.retry_count }} 次重试
                </div>
            </div>
        </div>

        <!-- Source Text -->
        <div class="section">
            <div class="section-label">📄 原文（{{ task.source_lang }}）</div>
            <div class="text-block source">{{ task.source_text }}</div>
        </div>

        <!-- Translation (collapsible) -->
        <div v-if="task.translation" class="section">
            <div class="section-header" @click="showTranslation = !showTranslation">
                <span class="section-label">📝 翻译稿</span>
                <span class="chevron">{{ showTranslation ? '▾' : '▸' }}</span>
            </div>
            <div v-if="showTranslation" class="text-block translation fade-in">
                {{ task.translation }}
            </div>
        </div>

        <!-- Review Feedback (collapsible) -->
        <div v-if="task.review_feedback" class="section">
            <div class="section-header" @click="showReview = !showReview">
                <span class="section-label">🔍 审校意见（{{ task.review_score }}/100）</span>
                <span class="chevron">{{ showReview ? '▾' : '▸' }}</span>
            </div>
            <div v-if="showReview" class="text-block review fade-in">
                {{ task.review_feedback }}
            </div>
        </div>

        <!-- Final Result -->
        <div v-if="finalResult" class="section">
            <div class="section-label">✨ 最终结果（润色后 · {{ task.target_lang }}）</div>
            <div class="text-block final">{{ finalResult }}</div>
        </div>

        <!-- Email -->
        <div v-if="task.status === 'done'" class="email-row fade-in">
            <input
                v-model="emailAddress"
                class="email-input"
                type="email"
                placeholder="输入收件人邮箱地址..."
            />
            <button
                class="btn btn-outline"
                :disabled="!emailAddress.trim()"
                @click="sendEmail"
            >📧 发送邮件</button>
        </div>

        <!-- New Translation -->
        <button class="btn btn-primary new-btn" @click="$emit('newTranslation')">
            🔄 开始新的翻译
        </button>
    </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
    task: Object,
})

const emit = defineEmits(['sendEmail', 'newTranslation'])

const emailAddress = ref('')
const showTranslation = ref(true)
const showReview = ref(true)

const finalResult = computed(() => {
    return props.task?.final_result || props.task?.polished || props.task?.translation || ''
})

function sendEmail() {
    if (!emailAddress.value.trim()) return
    emit('sendEmail', emailAddress.value.trim())
    emailAddress.value = ''
}
</script>

<style scoped>
.result-root { }

/* Banner */
.result-banner {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 18px 20px;
    border-radius: 14px;
    margin-bottom: 24px;
}

.result-banner.success {
    background: #f6ffed;
    border: 1px solid #b7eb8f;
}

.result-banner.failed {
    background: #fff1f0;
    border: 1px solid #ffa39e;
}

.banner-icon { font-size: 28px; }
.banner-title { font-size: 16px; font-weight: 600; color: #333; }
.result-banner.success .banner-title { color: #389e0d; }
.result-banner.failed .banner-title { color: #cf1322; }
.banner-sub { font-size: 13px; color: #888; margin-top: 3px; }

/* Sections */
.section { margin-bottom: 18px; }

.section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    user-select: none;
}

.section-header:hover { opacity: 0.8; }

.section-label {
    font-size: 14px;
    font-weight: 600;
    color: #555;
    margin-bottom: 8px;
}

.chevron { font-size: 16px; color: #999; }

/* Text blocks */
.text-block {
    font-size: 14px;
    line-height: 1.8;
    padding: 14px 18px;
    border-radius: 12px;
    white-space: pre-wrap;
    word-break: break-word;
}

.text-block.source {
    background: #fafafa;
    border-left: 3px solid #d9d9d9;
    color: #555;
}

.text-block.translation {
    background: #fafafa;
    border-left: 3px solid #1890ff;
    color: #333;
}

.text-block.review {
    background: #fffbe6;
    border-left: 3px solid #faad14;
    color: #8c6d00;
}

.text-block.final {
    background: linear-gradient(135deg, #f9f0ff 0%, #f0f5ff 100%);
    border: 1.5px solid #d4c5f0;
    font-size: 16px;
    color: #1a1a1a;
    line-height: 1.9;
}

/* Email */
.email-row {
    display: flex;
    gap: 10px;
    margin-top: 24px;
    padding-top: 18px;
    border-top: 1px solid #f0f0f0;
}

.email-input {
    flex: 1;
    padding: 11px 16px;
    border: 1.5px solid #e0e0e0;
    border-radius: 12px;
    font-size: 14px;
    outline: none;
    font-family: inherit;
    transition: border-color 0.2s;
}

.email-input:focus {
    border-color: #667eea;
}

/* New translation button */
.new-btn {
    width: 100%;
    margin-top: 16px;
    padding: 13px;
    font-size: 16px;
}
</style>
