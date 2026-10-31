<script setup>
/**
 * 阶段时间线：terminology → translate → review → polish → human_review → finalize
 * 数据来自 detail.history（stage, at, note）；当前阶段高亮并显示耗时。
 */
import { computed } from 'vue'
import {
    STAGES,
    formatDuration,
    formatTime,
    isFailedStatus,
    isPipelineStage,
    isSettledStatus,
    normalizeStage
} from '../utils/format'

const props = defineProps({
    history: { type: Array, default: () => [] },
    currentStage: { type: String, default: '' },
    status: { type: String, default: '' },
    progress: { type: Number, default: 0 },
    message: { type: String, default: '' }
})

const normalize = (value) => String(value || '').toLowerCase()

const entries = computed(() => {
    const records = props.history || []
    // 同一阶段可能因重试出现多次，取最后一次；supervisor/init/glossary 等非流水线阶段忽略
    const latest = new Map()
    for (const item of records) {
        const raw = item?.stage
        if (!isPipelineStage(raw)) continue
        latest.set(normalizeStage(raw), item)
    }

    const current = normalizeStage(props.currentStage)
    const currentIndex = STAGES.findIndex((stage) => stage.key === current)
    const completed = ['completed', 'done'].includes(normalize(props.status))

    let previousAt = null
    return STAGES.map((stage, index) => {
        const record = latest.get(stage.key)
        const at = record?.at ? new Date(record.at).getTime() : NaN
        let duration = null
        if (Number.isFinite(at)) {
            if (previousAt !== null) duration = at - previousAt
            previousAt = at
        }

        let state = 'pending'
        if (record) {
            state = completed ? 'done' : index === currentIndex ? 'active' : 'done'
        } else if (index === currentIndex) {
            state = 'active'
        } else if (currentIndex >= 0 && index < currentIndex) {
            state = 'skipped'
        }

        return {
            ...stage,
            at: Number.isFinite(at) ? at : null,
            note: record?.note || record?.message || '',
            duration,
            state
        }
    })
})

// 当前阶段色调：运行中强调色 / 待人工琥珀 / 失败红
const activeTone = computed(() => {
    if (isFailedStatus(props.status)) return 'danger'
    const status = normalize(props.status)
    if (status === 'waiting_human' || status === 'awaiting_human') return 'warn'
    return 'accent'
})

const currentLabel = computed(() => {
    const status = normalize(props.status)
    if (isSettledStatus(props.status) && status !== 'waiting_human' && status !== 'awaiting_human') {
        return status === 'completed' || status === 'done' ? '流水线已结束' : '流水线中断'
    }
    const found = entries.value.find((stage) => stage.state === 'active')
    return found ? `当前阶段：${found.label}` : '等待开始'
})
</script>

<template>
    <section class="card">
        <header class="card-head">
            <h2 class="card-title">阶段时间线</h2>
            <span class="card-hint">{{ currentLabel }}</span>
            <span class="timeline-progress mono">{{ Math.round(progress) }}%</span>
        </header>

        <div class="card-body">
            <div v-if="message" class="stage-live">
                <span class="dot" aria-hidden="true"></span>{{ message }}
            </div>

            <ol class="timeline">
                <li
                    v-for="stage in entries"
                    :key="stage.key"
                    class="stage"
                    :class="[stage.state, `tone-${activeTone}`]"
                >
                    <span class="stage-dot" aria-hidden="true"></span>
                    <div class="stage-main">
                        <div class="stage-line">
                            <span class="stage-name">{{ stage.label }}</span>
                            <span class="stage-key mono">{{ stage.key }}</span>
                            <span v-if="stage.at" class="stage-time mono">{{ formatTime(stage.at) }}</span>
                            <span v-if="stage.duration !== null" class="stage-dur mono">
                                {{ formatDuration(stage.duration) }}
                            </span>
                        </div>
                        <p v-if="stage.note" class="stage-note">{{ stage.note }}</p>
                        <p v-else-if="stage.state === 'active'" class="stage-note muted">
                            {{
                                normalize(status) === 'waiting_human' || normalize(status) === 'awaiting_human'
                                    ? '等待人工确认'
                                    : '进行中…'
                            }}
                        </p>
                        <p v-else-if="stage.state === 'pending'" class="stage-note faint">{{ stage.desc }}</p>
                    </div>
                </li>
            </ol>

            <p v-if="!history.length" class="timeline-empty">暂无阶段记录，任务开始后会在此实时展示。</p>
        </div>
    </section>
</template>

<style scoped>
.timeline-progress {
    margin-left: auto;
    color: var(--muted);
    font-size: 11.5px;
}

.stage-live {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 10px;
    padding: 6px 9px;
    border: 1px solid var(--accent-border);
    border-radius: var(--radius-sm);
    background: var(--accent-soft);
    color: var(--accent);
    font-size: 11.5px;
}

.stage-live .dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
}

.timeline {
    position: relative;
    padding-left: 20px;
}

.timeline::before {
    position: absolute;
    top: 7px;
    bottom: 9px;
    left: 4px;
    width: 1px;
    background: var(--border);
    content: '';
}

.stage {
    position: relative;
    padding: 5px 0 7px;
}

.stage + .stage {
    margin-top: 2px;
}

.stage-dot {
    position: absolute;
    top: 7px;
    left: -20px;
    width: 9px;
    height: 9px;
    border: 1px solid var(--border-strong);
    border-radius: 50%;
    background: var(--panel);
}

.stage.done .stage-dot {
    border-color: var(--accent);
    background: var(--accent);
}

.stage.active .stage-dot {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-ring);
}

.stage.active.tone-warn .stage-dot {
    border-color: var(--warn);
    box-shadow: 0 0 0 3px var(--warn-soft);
}

.stage.active.tone-danger .stage-dot {
    border-color: var(--danger);
    box-shadow: 0 0 0 3px var(--danger-soft);
}

.stage.skipped .stage-dot {
    border-style: dashed;
}

.stage-line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
}

.stage-name {
    color: var(--muted);
    font-size: 12.5px;
    font-weight: 500;
}

.stage.done .stage-name {
    color: var(--text-2);
}

.stage.active .stage-name {
    color: var(--text);
    font-weight: 600;
}

.stage-key {
    color: var(--faint);
    font-size: 10.5px;
}

.stage-time,
.stage-dur {
    color: var(--faint);
    font-size: 10.5px;
}

.stage-dur {
    padding: 0 5px;
    border-radius: 4px;
    background: var(--panel-2);
}

.stage-note {
    margin-top: 3px;
    color: var(--muted);
    font-size: 11.5px;
    line-height: 1.6;
}

.stage-note.muted {
    color: var(--faint);
}

.stage-note.faint {
    color: var(--faint);
    opacity: 0.75;
}

.stage.active .stage-note {
    color: var(--accent);
}

.stage.active.tone-warn .stage-note {
    color: var(--warn);
}

.stage.active.tone-danger .stage-note {
    color: var(--danger);
}

.timeline-empty {
    margin-top: 6px;
    color: var(--faint);
    font-size: 11.5px;
}
</style>