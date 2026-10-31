<script setup>
/**
 * 评分卡：4 维条形 + overall 大数字 + issues 列表
 */
import { computed } from 'vue'
import { isFailedStatus, normalizeScore } from '../utils/format'

const props = defineProps({
    review: { type: Object, default: null },
    status: { type: String, default: '' }
})

const DIMENSIONS = [
    { key: 'fidelity', label: '忠实度' },
    { key: 'terminology', label: '术语准确' },
    { key: 'fluency', label: '流畅度' },
    { key: 'completeness', label: '完整性' }
]

const normalize = (value) => String(value || '').toLowerCase()

const overall = computed(() => normalizeScore(props.review?.overall ?? props.review?.score))

const dimensions = computed(() =>
    DIMENSIONS.map((dim) => ({
        ...dim,
        value: normalizeScore(props.review?.[dim.key] ?? props.review?.scores?.[dim.key])
    }))
)

const hasReview = computed(
    () => overall.value !== null || dimensions.value.some((dim) => dim.value !== null)
)

const issues = computed(() => {
    const raw = props.review?.issues
    if (!Array.isArray(raw)) return []
    return raw.map((item, index) => {
        if (typeof item === 'string') return { key: index, tag: '', text: item, suggestion: '' }
        const text =
            item?.description || item?.message || item?.text || item?.issue || item?.detail || ''
        const suggestion = item?.suggestion || item?.fix || item?.advice || ''
        return {
            key: index,
            tag: item?.dimension || item?.severity || item?.type || '',
            text: text || JSON.stringify(item),
            suggestion
        }
    })
})

const toneOf = (value) => {
    if (value === null) return 'muted'
    if (value >= 80) return 'ok'
    if (value >= 60) return 'warn'
    return 'danger'
}

const emptyText = computed(() => {
    if (isFailedStatus(props.status)) return '本次任务未生成评分卡，可重试后再查看。'
    const status = normalize(props.status)
    if (status === 'waiting_human' || status === 'awaiting_human') return '评分卡将在自动审校完成后展示。'
    return '任务完成后将在此展示四维评分与问题清单。'
})
</script>

<template>
    <section class="card score-card">
        <header class="card-head">
            <h2 class="card-title">评分卡</h2>
            <span v-if="hasReview" class="card-hint">{{ issues.length }} 条问题</span>
            <span v-else class="card-hint">暂无评分</span>
        </header>

        <div class="card-body">
            <template v-if="hasReview">
                <div class="score-top">
                    <div class="score-overall">
                        <span class="overall-value mono" :class="`val-${toneOf(overall)}`">
                            {{ overall === null ? '--' : overall }}
                        </span>
                        <span class="overall-unit">/ 100</span>
                    </div>
                    <span class="overall-label">综合评分</span>
                </div>

                <div class="dims">
                    <div v-for="dim in dimensions" :key="dim.key" class="dim">
                        <div class="dim-head">
                            <span class="dim-label">{{ dim.label }}</span>
                            <span class="dim-value mono" :class="`val-${toneOf(dim.value)}`">
                                {{ dim.value === null ? '--' : dim.value }}
                            </span>
                        </div>
                        <div class="dim-track">
                            <span
                                class="dim-fill"
                                :class="`val-${toneOf(dim.value)}`"
                                :style="{ transform: `scaleX(${(dim.value ?? 0) / 100})` }"
                            ></span>
                        </div>
                    </div>
                </div>

                <div v-if="issues.length" class="issues">
                    <p class="issues-title">审校问题</p>
                    <ul class="issue-list">
                        <li v-for="issue in issues" :key="issue.key" class="issue">
                            <span v-if="issue.tag" class="issue-tag">{{ issue.tag }}</span>
                            <div class="issue-main">
                                <p class="issue-text">{{ issue.text }}</p>
                                <p v-if="issue.suggestion" class="issue-suggestion">建议：{{ issue.suggestion }}</p>
                            </div>
                        </li>
                    </ul>
                </div>
                <p v-else class="issues-empty">未发现审校问题。</p>
            </template>

            <div v-else class="state state-compact score-empty">
                <p class="state-title">评分尚未生成</p>
                <p class="state-desc">{{ emptyText }}</p>
            </div>
        </div>
    </section>
</template>

<style scoped>
.score-top {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding-bottom: 10px;
}

.score-overall {
    display: flex;
    align-items: baseline;
    gap: 5px;
}

.overall-value {
    color: var(--text);
    font-size: 30px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: -0.03em;
}

.overall-unit {
    color: var(--faint);
    font-size: 11px;
}

.overall-label {
    color: var(--muted);
    font-size: 11.5px;
}

.val-ok {
    color: var(--ok);
}

.val-warn {
    color: var(--warn);
}

.val-danger {
    color: var(--danger);
}

.val-muted {
    color: var(--faint);
}

.dims {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 24px;
}

.dim-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 5px;
}

.dim-label {
    color: var(--muted);
    font-size: 11.5px;
}

.dim-value {
    font-size: 11.5px;
}

.dim-track {
    height: 4px;
    overflow: hidden;
    border-radius: 2px;
    background: var(--panel-3);
}

.dim-fill {
    display: block;
    width: 100%;
    height: 100%;
    transform-origin: left center;
    background: var(--accent);
    transition: transform var(--dur-slow) ease;
}

.dim-fill.val-ok {
    background: var(--ok);
}

.dim-fill.val-warn {
    background: var(--warn);
}

.dim-fill.val-danger {
    background: var(--danger);
}

.dim-fill.val-muted {
    background: var(--border-strong);
}

.issues {
    margin-top: 14px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
}

.issues-title {
    margin-bottom: 7px;
    color: var(--muted);
    font-size: 11px;
    letter-spacing: 0.02em;
}

.issue-list {
    display: flex;
    flex-direction: column;
    gap: 7px;
}

.issue {
    display: flex;
    align-items: flex-start;
    gap: 7px;
}

.issue-tag {
    flex: 0 0 auto;
    padding: 1px 6px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--panel-2);
    color: var(--muted);
    font-size: 10.5px;
    line-height: 1.6;
}

.issue-main {
    min-width: 0;
}

.issue-text {
    color: var(--text-2);
    font-size: 12px;
    line-height: 1.62;
}

.issue-suggestion {
    margin-top: 2px;
    color: var(--muted);
    font-size: 11.5px;
    line-height: 1.55;
}

.issues-empty {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    color: var(--faint);
    font-size: 11.5px;
}

.score-empty {
    padding: 22px 12px;
}

@media (max-width: 620px) {
    .dims {
        grid-template-columns: 1fr;
    }
}
</style>