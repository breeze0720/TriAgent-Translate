<script setup>
/**
 * 右栏「人工审核」区：待人工时提供通过 / 编辑后通过 / 打回重译，
 * 失败时提供重试，运行中显示实时进度说明。
 */
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'
import { isFailedStatus, stageLabel } from '../utils/format'

const props = defineProps({
    status: { type: String, default: '' },
    stage: { type: String, default: '' },
    progress: { type: Number, default: 0 },
    retryCount: { type: Number, default: 0 },
    maxRetries: { type: Number, default: 3 },
    error: { type: String, default: '' },
    dirty: { type: Boolean, default: false },
    submitting: { type: Boolean, default: false },
    comment: { type: String, default: '' },
    message: { type: String, default: '' }
})

const emit = defineEmits(['approve', 'approve-edited', 'reject', 'retry', 'update:comment'])

const normalized = computed(() => String(props.status || '').toLowerCase())
const awaiting = computed(
    () => normalized.value === 'waiting_human' || normalized.value === 'awaiting_human'
)
const failed = computed(() => isFailedStatus(props.status))
const running = computed(() =>
    ['pending', 'queued', 'running', 'translating', 'reviewing', 'polishing'].includes(normalized.value)
)
const completed = computed(() => normalized.value === 'completed' || normalized.value === 'done')
const retriesLeft = computed(() => Math.max(0, props.maxRetries - props.retryCount))
const exhausted = computed(() => retriesLeft.value <= 0)

const onCommentInput = (event) => emit('update:comment', event.target.value)
</script>

<template>
    <section class="card review">
        <header class="card-head">
            <h2 class="card-title">人工审核</h2>
            <StatusBadge v-if="status" :status="status" class="badge-right" />
        </header>

        <div class="card-body">
            <!-- 待人工：三种操作 -->
            <template v-if="awaiting">
                <p class="review-tip">
                    译文已通过自动审校，等待你的确认。可在中栏直接修改译文，修改会随「通过」一并提交。
                </p>

                <label class="field-label" for="review-comment">审核备注（可选）</label>
                <textarea
                    id="review-comment"
                    class="textarea comment"
                    rows="3"
                    :value="comment"
                    placeholder="填写修改说明或打回理由，便于追溯"
                    @input="onCommentInput"
                ></textarea>

                <p v-if="dirty" class="review-hint warn">检测到译文修改，点击「通过」会一并提交。</p>

                <div class="review-actions">
                    <button
                        class="btn btn-sm btn-primary"
                        type="button"
                        :disabled="submitting"
                        @click="emit('approve')"
                    >
                        {{ submitting ? '提交中…' : '通过' }}
                    </button>
                    <button
                        class="btn btn-sm"
                        type="button"
                        :disabled="submitting"
                        @click="emit('approve-edited')"
                    >
                        编辑后通过
                    </button>
                    <button
                        class="btn btn-sm btn-danger reject"
                        type="button"
                        :disabled="submitting"
                        @click="emit('reject')"
                    >
                        打回重译
                    </button>
                </div>
            </template>

            <!-- 失败：重试 -->
            <template v-else-if="failed">
                <p class="review-tip danger">{{ error || '任务执行失败，可重试恢复。' }}</p>
                <p class="review-hint">
                    重试次数
                    <span class="mono">{{ retryCount }} / {{ maxRetries }}</span>
                </p>
                <button
                    class="btn btn-sm btn-primary retry-btn"
                    type="button"
                    :disabled="submitting || exhausted"
                    @click="emit('retry')"
                >
                    {{ submitting ? '重试中…' : '重试任务' }}
                </button>
                <p v-if="exhausted" class="review-hint warn">
                    已达最大重试次数，请调整原文或联系管理员后再创建新任务。
                </p>
            </template>

            <!-- 运行中 / 排队 -->
            <template v-else-if="running">
                <div class="running-box">
                    <div class="running-head">
                        <span class="running-stage">{{ stageLabel(stage) }}</span>
                        <span class="mono running-pct">{{ Math.round(progress) }}%</span>
                    </div>
                    <div class="running-track">
                        <span
                            class="running-fill"
                            :style="{ transform: `scaleX(${Math.max(0, Math.min(100, progress)) / 100})` }"
                        ></span>
                    </div>
                    <p class="review-hint">流水线执行中，进度通过 SSE 实时推送；断线会自动重连。</p>
                    <p v-if="message" class="running-message">{{ message }}</p>
                </div>
            </template>

            <!-- 完成 -->
            <template v-else-if="completed">
                <p class="review-tip ok">任务已完成，可在顶部导出 Markdown 或 Word 文件。</p>
                <p class="review-hint">如需调整，可在中栏查看对照与评分卡。</p>
            </template>

            <template v-else>
                <p class="review-hint">暂无任务信息，选择或新建任务后开始。</p>
            </template>
        </div>
    </section>
</template>

<style scoped>
.badge-right {
    margin-left: auto;
}

.review-tip {
    color: var(--text-2);
    font-size: 11.8px;
    line-height: 1.68;
}

.review-tip.danger {
    padding: 7px 9px;
    border: 1px solid var(--danger-border);
    border-radius: var(--radius-sm);
    background: var(--danger-soft);
    color: var(--danger);
    font-size: 11.5px;
    word-break: break-word;
}

.review-tip.ok {
    padding: 7px 9px;
    border: 1px solid var(--ok-border);
    border-radius: var(--radius-sm);
    background: var(--ok-soft);
    color: var(--ok);
    font-size: 11.5px;
}

.field-label {
    margin-top: 10px;
}

.comment {
    min-height: 58px;
    font-size: 11.8px;
}

.review-hint {
    margin-top: 7px;
    color: var(--muted);
    font-size: 11px;
    line-height: 1.6;
}

.review-hint.warn {
    color: var(--warn);
}

.review-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    margin-top: 10px;
}

.review-actions .reject {
    grid-column: 1 / -1;
}

.retry-btn {
    width: 100%;
    margin-top: 10px;
}

.running-box {
    padding: 9px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel-2);
}

.running-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 6px;
}

.running-stage {
    color: var(--text-2);
    font-size: 12px;
    font-weight: 500;
}

.running-pct {
    color: var(--accent);
    font-size: 11.5px;
}

.running-track {
    height: 3px;
    overflow: hidden;
    border-radius: 2px;
    background: var(--panel-3);
}

.running-fill {
    display: block;
    width: 100%;
    height: 100%;
    transform-origin: left center;
    background: var(--accent);
    transition: transform var(--dur-slow) ease;
}

.running-message {
    margin-top: 6px;
    color: var(--muted);
    font-size: 11px;
    line-height: 1.55;
    word-break: break-word;
}
</style>