<script setup>
/**
 * 左栏：新建任务 + 任务列表（状态徽标 / 细进度条 / 评分 / 悬停删除）
 */
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'
import { formatTime, normalizeScore, stageLabel, statusMeta, truncate } from '../utils/format'

const props = defineProps({
    tasks: { type: Array, default: () => [] },
    currentId: { type: String, default: '' },
    loading: { type: Boolean, default: false },
    error: { type: String, default: '' },
    pendingDeleteId: { type: String, default: '' }
})

const emit = defineEmits(['create', 'select', 'delete', 'reload'])

const isEmpty = computed(() => !props.loading && !props.error && props.tasks.length === 0)

const titleOf = (task) => task.title || task.source_title || task.id || '未命名任务'
// 列表标题按需求取前 20 字
const shortTitle = (task) => truncate(titleOf(task), 20)
const scoreOf = (task) => normalizeScore(task.score)

const progressOf = (task) => {
    const value = Number(task.progress)
    if (!Number.isFinite(value)) return 0
    return Math.max(0, Math.min(100, value))
}

const isActive = (task) =>
    ['running', 'translating', 'reviewing', 'polishing', 'pending', 'queued'].includes(
        String(task.status || '').toLowerCase()
    )

const footText = (task) => {
    const status = String(task.status || '').toLowerCase()
    if (status === 'done' || status === 'completed') {
        const score = scoreOf(task)
        return score === null ? '已完成' : `评分 ${score}`
    }
    return stageLabel(task.stage)
}

const toneOf = (task) => statusMeta(task.status).tone
</script>

<template>
    <aside class="sidebar">
        <div class="side-top">
            <button class="btn btn-primary btn-block" type="button" @click="emit('create')">
                <span aria-hidden="true">＋</span> 新建任务
            </button>
            <div class="side-meta">
                <span>任务列表</span>
                <span class="mono side-count">{{ tasks.length }}</span>
            </div>
        </div>

        <div class="side-list" aria-live="polite">
            <div v-if="loading && !tasks.length" class="side-skeleton">
                <div v-for="i in 4" :key="i" class="skeleton side-skeleton-item"></div>
            </div>

            <div v-else-if="error" class="state state-compact">
                <p class="state-title">列表加载失败</p>
                <p class="state-desc">{{ error }}</p>
                <button class="btn btn-sm" type="button" @click="emit('reload')">重新加载</button>
            </div>

            <div v-else-if="isEmpty" class="state state-compact">
                <p class="state-title">还没有任务</p>
                <p class="state-desc">点击上方「新建任务」，粘贴原文即可开始。</p>
            </div>

            <div
                v-for="task in tasks"
                :key="task.id"
                class="task-item"
                :class="{ active: task.id === currentId, failed: toneOf(task) === 'failed' }"
                role="button"
                tabindex="0"
                :aria-current="task.id === currentId ? 'true' : 'false'"
                @click="emit('select', task.id)"
                @keydown.enter.prevent="emit('select', task.id)"
                @keydown.space.prevent="emit('select', task.id)"
            >
                <div class="task-row">
                    <span class="task-title" :title="titleOf(task)">{{ shortTitle(task) }}</span>
                    <StatusBadge :status="task.status" />
                </div>

                <div class="task-bar" :class="{ idle: !isActive(task) }">
                    <span class="task-bar-fill" :style="{ width: `${progressOf(task)}%` }"></span>
                </div>

                <div class="task-foot">
                    <span class="mono task-time">{{ formatTime(task.created_at) }}</span>
                    <span class="task-stage">{{ footText(task) }}</span>
                </div>

                <button
                    class="task-del"
                    :class="{ confirm: pendingDeleteId === task.id }"
                    type="button"
                    :aria-label="pendingDeleteId === task.id ? '确认删除任务' : '删除任务'"
                    @click.stop="emit('delete', task.id)"
                >
                    {{ pendingDeleteId === task.id ? '确认' : '×' }}
                </button>
            </div>
        </div>
    </aside>
</template>

<style scoped>
.side-top {
    flex: 0 0 auto;
    padding: 12px 12px 8px;
}

.side-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 12px;
    padding: 0 2px;
    color: var(--muted);
    font-size: 11px;
    letter-spacing: 0.02em;
}

.side-count {
    color: var(--faint);
    font-size: 11px;
}

.side-list {
    flex: 1 1 auto;
    min-height: 0;
    padding: 0 8px 12px;
    overflow-y: auto;
}

.side-skeleton {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 4px;
}

.side-skeleton-item {
    height: 64px;
}

.task-item {
    position: relative;
    padding: 9px 10px 8px;
    border: 1px solid transparent;
    border-radius: var(--radius);
    cursor: pointer;
    transition: background var(--dur) ease, border-color var(--dur) ease;
}

.task-item + .task-item {
    margin-top: 2px;
}

.task-item:hover {
    border-color: var(--border);
    background: var(--panel);
}

.task-item.active {
    border-color: var(--border-strong);
    background: var(--panel);
    box-shadow: var(--shadow);
}

.task-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-right: 18px;
}

.task-title {
    overflow: hidden;
    flex: 1 1 auto;
    min-width: 0;
    color: var(--text-2);
    font-size: 12.5px;
    font-weight: 500;
    line-height: 1.45;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.task-item.active .task-title {
    color: var(--text);
}

.task-bar {
    height: 2px;
    margin: 8px 0 7px;
    overflow: hidden;
    border-radius: 2px;
    background: var(--panel-3);
}

.task-bar.idle {
    background: var(--panel-3);
}

.task-bar-fill {
    display: block;
    height: 100%;
    border-radius: 2px;
    background: var(--accent);
    transition: width var(--dur-slow) ease;
}

.task-item.failed .task-bar-fill {
    background: var(--danger);
}

.task-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    color: var(--faint);
    font-size: 10.5px;
}

.task-stage {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.task-del {
    position: absolute;
    top: 6px;
    right: 6px;
    display: grid;
    place-items: center;
    width: 17px;
    height: 17px;
    border-radius: var(--radius-sm);
    color: var(--faint);
    font-size: 13px;
    line-height: 1;
    opacity: 0;
    transition: opacity var(--dur) ease, background var(--dur) ease, color var(--dur) ease;
}

.task-item:hover .task-del,
.task-item.active .task-del,
.task-del.confirm {
    opacity: 1;
}

.task-del:hover {
    background: var(--danger-soft);
    color: var(--danger);
}

.task-del.confirm {
    width: auto;
    padding: 0 5px;
    background: var(--danger-soft);
    color: var(--danger);
    font-size: 10.5px;
}
</style>