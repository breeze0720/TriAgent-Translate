<script setup>
/**
 * 三栏工作台主视图
 * 左：任务列表 ｜ 中：对照 + 时间线 + 评分卡 ｜ 右：术语表 + 人工审核
 * 数据流：REST 拉取详情，SSE 增量推送阶段/进度/评分，终态后补拉一次完整详情。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppHeader from './components/AppHeader.vue'
import TaskSidebar from './components/TaskSidebar.vue'
import WorkspaceEmpty from './components/WorkspaceEmpty.vue'
import BilingualPanel from './components/BilingualPanel.vue'
import StageTimeline from './components/StageTimeline.vue'
import ScoreCard from './components/ScoreCard.vue'
import GlossaryPanel from './components/GlossaryPanel.vue'
import ReviewPanel from './components/ReviewPanel.vue'
import CreateTaskModal from './components/CreateTaskModal.vue'
import NoticeStack from './components/NoticeStack.vue'
import StatusBadge from './components/StatusBadge.vue'

import { api } from './api/client'
import { useTheme } from './composables/useTheme'
import { useNotifications } from './composables/useNotifications'
import { useTaskStream } from './composables/useTaskStream'
import {
    isActiveStatus,
    isFailedStatus,
    isPipelineStage,
    isSettledStatus,
    padArray,
    segmentText,
    splitParagraphs,
    stageLabel,
    truncate
} from './utils/format'

const { theme, toggleTheme } = useTheme()
const { notices, push, dismiss } = useNotifications()

// ---- 列表 ----
const tasks = ref([])
const listLoading = ref(true)
const listError = ref('')
const backendOnline = ref(true)

// ---- 当前任务 ----
const currentId = ref('')
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const lastMessage = ref('')

// ---- 对照编辑 ----
const layout = ref(readLayout())
const editedTranslations = ref([])
const baselineTranslations = ref('[]')

// ---- 审核 / 术语 ----
const reviewComment = ref('')
const submitting = ref(false)
const savingGlossary = ref(false)
const glossaryError = ref('')

// ---- 新建任务 ----
const createOpen = ref(false)
const creating = ref(false)
const createError = ref('')
// 上传限制由后端 /api/health 下发，前端不写死格式和体积
const uploadInfo = ref({})

// ---- 删除确认 ----
const pendingDeleteId = ref('')
const confirmDelete = ref(false)

let detailSeq = 0
let pendingDeleteTimer = null
let confirmDeleteTimer = null
let listTimer = null
let healthTimer = null

function readLayout() {
    try {
        return localStorage.getItem('ap2-compare-layout') === 'stack' ? 'stack' : 'side'
    } catch {
        return 'side'
    }
}

// ---- SSE ----
// 注意：useTaskStream 在 setup 期间调用，handleStreamEvent 为函数声明，已提升
const {
    state: streamState,
    open: startStream,
    close: stopStream,
    markSettled
} = useTaskStream(handleStreamEvent)

// ---- 计算属性 ----
const normalizedStatus = computed(() => String(detail.value?.status || '').toLowerCase())
const progress = computed(() => {
    const value = Number(detail.value?.progress)
    if (!Number.isFinite(value)) return 0
    return Math.max(0, Math.min(100, value))
})
const settled = computed(() => isSettledStatus(detail.value?.status))
const isRunning = computed(() =>
    ['pending', 'queued', 'running', 'translating', 'reviewing', 'polishing'].includes(normalizedStatus.value)
)
const editable = computed(
    () => normalizedStatus.value === 'waiting_human' || normalizedStatus.value === 'awaiting_human'
)
const retryCount = computed(() => Number(detail.value?.retry_count || 0))
const maxRetries = computed(() => Number(detail.value?.max_retries ?? 3))
const canRetry = computed(
    () => isFailedStatus(detail.value?.status) && retryCount.value < maxRetries.value
)

const title = computed(() => {
    const data = detail.value
    if (!data) return ''
    return data.title || truncate(data.source_text || '', 40) || '未命名任务'
})

const translationsDirty = computed(
    () => JSON.stringify(editedTranslations.value) !== baselineTranslations.value
)

const hasActiveTask = computed(() => tasks.value.some((task) => isActiveStatus(task.status)))

// 原文分段：优先用后端 segments，缺失时按段落/译文段数切分
const sourceSegments = computed(() => {
    const data = detail.value
    if (!data) return []
    const fromServer = Array.isArray(data.segments)
        ? data.segments.map(segmentText).filter((text) => text !== '')
        : []
    if (fromServer.length) return fromServer

    const paragraphs = splitParagraphs(data.source_text || '')
    const targetCount = editedTranslations.value.length
    if (!targetCount) return paragraphs.length ? paragraphs : [String(data.source_text || '')].filter(Boolean)
    return padArray(paragraphs, targetCount, '')
})

const streamLabel = computed(() => {
    if (settled.value) {
        return normalizedStatus.value === 'awaiting_human' || normalizedStatus.value === 'waiting_human'
            ? '实时推送已暂停'
            : '实时推送已结束'
    }
    const labels = {
        connecting: '正在连接实时推送…',
        open: '实时推送已连接',
        reconnecting: '连接中断，正在重连…',
        closed: '实时推送已关闭',
        idle: '未连接实时推送'
    }
    return labels[streamState.value] || '未连接实时推送'
})

const deleteLabel = computed(() => (confirmDelete.value ? '确认删除' : '删除'))

// ---- 列表 ----
async function refreshList({ silent = false } = {}) {
    if (!silent) listLoading.value = true
    try {
        const data = await api.listTasks(50)
        const list = Array.isArray(data?.tasks) ? data.tasks : []
        // 后端 id 为数字，统一转字符串，便于与详情/SSE 对齐
        tasks.value = list.map((task) => ({ ...task, id: String(task.id) }))
        listError.value = ''
        backendOnline.value = true
        if (currentId.value && !list.some((task) => task.id === currentId.value)) closeCurrent()
        if (!currentId.value && list.length) openTask(list[0].id)
    } catch (err) {
        listError.value = err?.message || '加载任务列表失败'
        if (err?.status === 0) backendOnline.value = false
    } finally {
        listLoading.value = false
    }
}

function patchListItem(id, payload) {
    const index = tasks.value.findIndex((task) => task.id === id)
    if (index < 0) return
    const next = { ...tasks.value[index] }
    if (payload.status) next.status = payload.status
    if (payload.stage && isPipelineStage(payload.stage)) next.stage = payload.stage
    if (payload.progress !== undefined && payload.progress !== null) next.progress = Number(payload.progress)
    if (payload.review?.overall !== undefined && payload.review?.overall !== null) {
        next.score = payload.review.overall
    }
    tasks.value.splice(index, 1, next)
}

// ---- 详情 ----
function translationListOf(data) {
    const list =
        Array.isArray(data?.translations) && data.translations.length
            ? data.translations
            : data?.translation
              ? [data.translation]
              : []
    return list.map(segmentText)
}

function applyDetail(data, { keepEdits = false } = {}) {
    // 详情用 task_id，列表用 id，统一成字符串 id
    const normalized = { ...data, id: String(data?.task_id ?? data?.id ?? currentId.value) }
    detail.value = normalized
    if (keepEdits) return
    const list = translationListOf(normalized)
    const size = Math.max(list.length, Array.isArray(normalized.segments) ? normalized.segments.length : 0)
    editedTranslations.value = padArray(list, size, '')
    baselineTranslations.value = JSON.stringify(editedTranslations.value)
}

async function openTask(id) {
    if (!id) return
    const taskId = String(id)
    if (taskId === currentId.value && detail.value) return
    const seq = ++detailSeq
    currentId.value = taskId
    detail.value = null
    detailError.value = ''
    detailLoading.value = true
    lastMessage.value = ''
    reviewComment.value = ''
    editedTranslations.value = []
    baselineTranslations.value = '[]'
    stopStream()

    try {
        const data = await api.getTask(taskId)
        if (seq !== detailSeq) return
        applyDetail(data)
        backendOnline.value = true
        // 终态任务不再保持 SSE 长连接；运行中任务立即订阅
        if (!isSettledStatus(data.status)) startStream(taskId)
    } catch (err) {
        if (seq !== detailSeq) return
        detailError.value = err?.message || '加载任务详情失败'
        if (err?.status === 0) backendOnline.value = false
    } finally {
        if (seq === detailSeq) detailLoading.value = false
    }
}

async function reloadDetail(id, { quiet = false, keepEdits = false } = {}) {
    if (!id) return
    const taskId = String(id)
    try {
        const data = await api.getTask(taskId)
        if (currentId.value !== taskId) return
        applyDetail(data, { keepEdits })
        backendOnline.value = true
        const streaming = streamState.value === 'open' || streamState.value === 'connecting'
        if (!isSettledStatus(data.status) && !streaming) startStream(taskId)
    } catch (err) {
        if (!quiet) push(`刷新任务详情失败：${err?.message || '未知错误'}`, 'error')
    }
}

function closeCurrent() {
    stopStream()
    currentId.value = ''
    detail.value = null
    detailError.value = ''
    lastMessage.value = ''
    reviewComment.value = ''
    editedTranslations.value = []
    baselineTranslations.value = '[]'
    confirmDelete.value = false
}

// ---- SSE 事件 ----
function handleStreamEvent(payload) {
    const id = currentId.value
    if (!id || !payload) return
    // SSE 载荷用 task_id；仅在事件属于当前任务时更新界面
    const eventId = String(payload.task_id ?? payload.id ?? id)
    if (eventId !== id) return

    const data = detail.value
    if (data) {
        // glossary/init 等非流水线阶段不覆盖当前阶段
        if (payload.stage && isPipelineStage(payload.stage)) data.stage = payload.stage
        if (payload.status) data.status = payload.status
        if (payload.progress !== undefined && payload.progress !== null) {
            data.progress = Number(payload.progress)
        }
        if (payload.review) data.review = payload.review
        if (payload.error) data.error = payload.error
    }
    // 「当前状态快照」是重连时的握手消息，不作为进度文案展示
    if (payload.message && payload.message !== '当前状态快照') lastMessage.value = payload.message
    patchListItem(id, payload)

    if (!isSettledStatus(payload.status)) return

    // 终态：关闭推送，补拉完整详情（评分/术语/历史），刷新列表
    markSettled()
    reloadDetail(id, { quiet: true })
    refreshList({ silent: true })

    const status = String(payload.status || '').toLowerCase()
    if (status === 'failed' || status === 'error') {
        push(`任务执行失败：${payload.error || payload.message || '未知原因'}`, 'error', 9000)
    } else if (status === 'waiting_human' || status === 'awaiting_human') {
        push('译文已就绪，等待人工审核', 'warn', 7000)
    } else {
        push('任务已完成，可导出结果', 'success', 6000)
    }
}

// ---- 操作：审核 / 重试 / 术语 / 导出 / 删除 / 新建 ----
async function submitReview(kind) {
    const id = currentId.value
    if (!id || submitting.value) return
    const approved = kind !== 'reject'
    const payload = { approved, comment: reviewComment.value.trim() || undefined }
    if (approved && (translationsDirty.value || kind === 'approve-edited')) {
        const merged = editedTranslations.value.join('\n\n').trim()
        if (merged) payload.edited_translation = merged
    }

    submitting.value = true
    try {
        await api.resumeTask(id, payload)
        reviewComment.value = ''
        baselineTranslations.value = JSON.stringify(editedTranslations.value)
        push(approved ? '已提交通过，任务继续执行' : '已打回重译，任务重新排队', 'success')
        await reloadDetail(id)
        await refreshList({ silent: true })
        if (!isSettledStatus(detail.value?.status)) startStream(id)
    } catch (err) {
        push(`提交审核结果失败：${err?.message || '未知错误'}`, 'error')
    } finally {
        submitting.value = false
    }
}

async function retryTask() {
    const id = currentId.value
    if (!id || submitting.value) return
    submitting.value = true
    try {
        await api.retryTask(id)
        push('已触发重试，任务重新进入流水线', 'success')
        await reloadDetail(id)
        await refreshList({ silent: true })
        startStream(id)
    } catch (err) {
        push(`重试失败：${err?.message || '未知错误'}`, 'error')
    } finally {
        submitting.value = false
    }
}

async function saveGlossary(rows) {
    const id = currentId.value
    if (!id) return
    savingGlossary.value = true
    glossaryError.value = ''
    try {
        await api.saveGlossary(id, rows)
        if (detail.value) detail.value.glossary = rows.map((item) => ({ ...item }))
        push('术语表已保存', 'success')
    } catch (err) {
        glossaryError.value = err?.message || '保存失败'
        push(`保存术语表失败：${glossaryError.value}`, 'error')
    } finally {
        savingGlossary.value = false
    }
}

function exportTask(fmt) {
    const id = currentId.value
    if (!id) return
    const link = document.createElement('a')
    link.href = api.exportUrl(id, fmt)
    link.download = `${id}.${fmt}`
    link.style.display = 'none'
    document.body.appendChild(link)
    link.click()
    link.remove()
    push(`已请求导出 ${fmt.toUpperCase()} 文件`, 'info', 3000)
}

function requestDeleteTask(id) {
    if (!id) return
    if (pendingDeleteId.value === id) {
        doDeleteTask(id)
        return
    }
    pendingDeleteId.value = id
    push('再次点击「确认」删除该任务', 'warn', 2600)
    window.clearTimeout(pendingDeleteTimer)
    pendingDeleteTimer = window.setTimeout(() => {
        pendingDeleteId.value = ''
    }, 4000)
}

function requestDeleteCurrent() {
    const id = currentId.value
    if (!id) return
    if (confirmDelete.value) {
        confirmDelete.value = false
        doDeleteTask(id)
        return
    }
    confirmDelete.value = true
    window.clearTimeout(confirmDeleteTimer)
    confirmDeleteTimer = window.setTimeout(() => {
        confirmDelete.value = false
    }, 4000)
}

async function doDeleteTask(id) {
    pendingDeleteId.value = ''
    window.clearTimeout(pendingDeleteTimer)
    try {
        await api.deleteTask(id)
        push('任务已删除', 'success')
        if (currentId.value === id) closeCurrent()
        await refreshList({ silent: true })
    } catch (err) {
        push(`删除失败：${err?.message || '未知错误'}`, 'error')
    }
}

function openCreate() {
    createError.value = ''
    createOpen.value = true
}

function closeCreate() {
    if (creating.value) return
    createOpen.value = false
    createError.value = ''
}

async function createTask(payload) {
    if (creating.value) return
    creating.value = true
    createError.value = ''
    try {
        const result =
            payload?.mode === 'file'
                ? await api.createTaskFromFile(payload.file, payload)
                : await api.createTask(payload)
        const newId = result?.task_id || result?.id
        createOpen.value = false
        push(
            result?.source_file
                ? `已解析「${result.source_file}」并创建任务，开始执行流水线`
                : '任务已创建，开始执行流水线',
            'success'
        )
        await refreshList({ silent: true })
        if (newId) openTask(newId)
    } catch (err) {
        createError.value = err?.message || '创建任务失败'
        if (err?.status === 0) backendOnline.value = false
    } finally {
        creating.value = false
    }
}

function onTranslationsUpdate(list) {
    editedTranslations.value = Array.isArray(list) ? list : []
}

function setLayout(value) {
    layout.value = value === 'stack' ? 'stack' : 'side'
}

// ---- 健康检查 / 轮询 ----
async function checkHealth() {
    try {
        const info = await api.health()
        backendOnline.value = true
        if (info?.upload) uploadInfo.value = info.upload
    } catch {
        backendOnline.value = false
    }
}

// 断线恢复：重连成功后补拉一次详情，避免漏掉中断期间的事件
let streamInterrupted = false
watch(streamState, (value) => {
    if (value === 'reconnecting') streamInterrupted = true
    if (value === 'open' && streamInterrupted) {
        streamInterrupted = false
        reloadDetail(currentId.value, { quiet: true, keepEdits: translationsDirty.value })
    }
})

watch(layout, (value) => {
    try {
        localStorage.setItem('ap2-compare-layout', value)
    } catch {
        /* 忽略持久化失败 */
    }
})

watch(currentId, () => {
    confirmDelete.value = false
    glossaryError.value = ''
})

onMounted(() => {
    refreshList()
    checkHealth()
    listTimer = window.setInterval(() => {
        if (document.visibilityState !== 'visible') return
        if (hasActiveTask.value) refreshList({ silent: true })
    }, 6000)
    healthTimer = window.setInterval(checkHealth, 20000)
})

onBeforeUnmount(() => {
    window.clearInterval(listTimer)
    window.clearInterval(healthTimer)
    window.clearTimeout(pendingDeleteTimer)
    window.clearTimeout(confirmDeleteTimer)
    stopStream()
})
</script>

<template>
    <div class="app">
        <AppHeader :theme="theme" :backend-online="backendOnline" @toggle-theme="toggleTheme" />

        <div class="app-body">
            <TaskSidebar
                :tasks="tasks"
                :current-id="currentId"
                :loading="listLoading"
                :error="listError"
                :pending-delete-id="pendingDeleteId"
                @create="openCreate"
                @select="openTask"
                @delete="requestDeleteTask"
                @reload="refreshList"
            />

            <main class="workspace">
                <template v-if="!currentId">
                    <WorkspaceEmpty @create="openCreate" />
                </template>

                <template v-else>
                    <div class="ws-head">
                        <div class="ws-title-wrap">
                            <h1 class="ws-title">{{ title || '任务详情' }}</h1>
                            <div class="ws-meta">
                                <StatusBadge :status="detail?.status || ''" />
                                <span class="meta-item mono">{{ currentId }}</span>
                                <span class="meta-item">阶段：{{ stageLabel(detail?.stage) }}</span>
                                <span class="meta-item mono">{{ Math.round(progress) }}%</span>
                                <span class="meta-item stream" :class="`s-${streamState}`">
                                    {{ streamLabel }}
                                </span>
                                <span v-if="retryCount" class="meta-item mono">
                                    重试 {{ retryCount }} / {{ maxRetries }}
                                </span>
                            </div>
                        </div>

                        <div class="ws-actions">
                            <button class="btn btn-sm" type="button" :disabled="!detail" @click="exportTask('md')">
                                导出 MD
                            </button>
                            <button class="btn btn-sm" type="button" :disabled="!detail" @click="exportTask('docx')">
                                导出 DOCX
                            </button>
                            <button
                                class="btn btn-sm"
                                type="button"
                                :disabled="!canRetry || submitting"
                                :title="canRetry ? '重新执行失败的任务' : '仅失败任务可重试'"
                                @click="retryTask"
                            >
                                {{ submitting ? '处理中…' : '重试' }}
                            </button>
                            <button
                                class="btn btn-sm"
                                :class="confirmDelete ? 'btn-danger-solid' : 'btn-danger'"
                                type="button"
                                @click="requestDeleteCurrent"
                            >
                                {{ deleteLabel }}
                            </button>
                        </div>
                    </div>

                    <div class="ws-progress">
                        <span class="ws-progress-fill" :style="{ transform: `scaleX(${progress / 100})` }"></span>
                    </div>

                    <div class="ws-scroll">
                        <div v-if="detailError" class="state">
                            <p class="state-title">任务详情加载失败</p>
                            <p class="state-desc">{{ detailError }}</p>
                            <button class="btn btn-sm" type="button" @click="openTask(currentId)">
                                重新加载
                            </button>
                        </div>

                        <div v-else-if="detailLoading && !detail" class="ws-skeleton">
                            <div class="skeleton sk-panel"></div>
                            <div class="skeleton sk-short"></div>
                            <div class="skeleton sk-mid"></div>
                            <div class="skeleton sk-panel"></div>
                        </div>

                        <template v-else-if="detail">
                            <div v-if="lastMessage && !settled" class="ws-banner">
                                <span class="dot" aria-hidden="true"></span>{{ lastMessage }}
                            </div>

                            <BilingualPanel
                                :segments="sourceSegments"
                                :translations="editedTranslations"
                                :layout="layout"
                                :editable="editable"
                                :running="isRunning"
                                @update:translations="onTranslationsUpdate"
                                @update:layout="setLayout"
                            />

                            <StageTimeline
                                :history="detail.history || []"
                                :current-stage="detail.stage || ''"
                                :status="detail.status || ''"
                                :progress="progress"
                            />

                            <ScoreCard :review="detail.review || null" :status="detail.status || ''" />

                            <div v-if="detail.error" class="ws-error-banner">
                                失败原因：{{ detail.error }}
                            </div>
                        </template>
                    </div>
                </template>
            </main>

            <aside class="inspector">
                <GlossaryPanel
                    :glossary="detail?.glossary || []"
                    :saving="savingGlossary"
                    :error="glossaryError"
                    :disabled="!detail"
                    @save="saveGlossary"
                />

                <ReviewPanel
                    :status="detail?.status || ''"
                    :stage="detail?.stage || ''"
                    :progress="progress"
                    :retry-count="retryCount"
                    :max-retries="maxRetries"
                    :error="detail?.error || ''"
                    :dirty="translationsDirty"
                    :submitting="submitting"
                    :comment="reviewComment"
                    :message="lastMessage"
                    @update:comment="reviewComment = $event"
                    @approve="submitReview('approve')"
                    @approve-edited="submitReview('approve-edited')"
                    @reject="submitReview('reject')"
                    @retry="retryTask"
                />
            </aside>
        </div>

        <CreateTaskModal
            v-if="createOpen"
            :submitting="creating"
            :error="createError"
            :limits="uploadInfo"
            @close="closeCreate"
            @submit="createTask"
        />

        <NoticeStack :items="notices" @dismiss="dismiss" />
    </div>
</template>