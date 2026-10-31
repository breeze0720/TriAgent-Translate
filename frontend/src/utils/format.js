/** 展示层格式化工具：状态/阶段文案、时间、评分归一化、文本切分 */

const STATUS_META = {
    pending: { label: '排队中', tone: 'queued' },
    queued: { label: '排队中', tone: 'queued' },
    running: { label: '翻译中', tone: 'running' },
    translating: { label: '翻译中', tone: 'running' },
    reviewing: { label: '审校中', tone: 'running' },
    polishing: { label: '润色中', tone: 'running' },
    waiting_human: { label: '待人工', tone: 'human' },
    awaiting_human: { label: '待人工', tone: 'human' },
    done: { label: '已完成', tone: 'done' },
    completed: { label: '已完成', tone: 'done' },
    failed: { label: '失败', tone: 'failed' },
    error: { label: '失败', tone: 'failed' },
    cancelled: { label: '已取消', tone: 'queued' }
}

export function statusMeta(status) {
    const key = String(status || '').toLowerCase()
    return STATUS_META[key] || { label: key ? key : '未知', tone: 'queued' }
}

/** 流水线阶段顺序（与后端 stage 取值一致） */
export const STAGES = [
    { key: 'terminology', label: '术语抽取', desc: '识别领域术语并生成术语表' },
    { key: 'translate', label: '初译', desc: '分段翻译原文' },
    { key: 'review', label: '审校', desc: '忠实度 / 术语 / 流畅度 / 完整性评审' },
    { key: 'polish', label: '润色', desc: '按评审意见改写译文' },
    { key: 'human_review', label: '人工审核', desc: '等待人工确认、修改或打回' },
    { key: 'finalize', label: '定稿', desc: '生成最终译文并支持导出' }
]

// 后端 history/SSE 中的阶段别名（节点完成态、终态写法）
const STAGE_ALIAS = {
    terminology_done: 'terminology',
    translated: 'translate',
    translating: 'translate',
    reviewed: 'review',
    reviewing: 'review',
    polished: 'polish',
    polishing: 'polish',
    final: 'finalize',
    finalized: 'finalize'
}

export function normalizeStage(stage) {
    const key = String(stage || '').toLowerCase()
    return STAGE_ALIAS[key] || key
}

export function isPipelineStage(stage) {
    const key = normalizeStage(stage)
    return STAGES.some((item) => item.key === key)
}

const STAGE_LABEL = STAGES.reduce((map, stage) => {
    map[stage.key] = stage.label
    return map
}, {})

export function stageLabel(stage) {
    const key = normalizeStage(stage)
    return STAGE_LABEL[key] || (key ? String(stage) : '未开始')
}

/** 稳定态：不再继续自动推进，需人工审核 / 重试 / 已结束 */
const SETTLED = ['done', 'completed', 'failed', 'error', 'waiting_human', 'awaiting_human']

export function isSettledStatus(status) {
    return SETTLED.includes(String(status || '').toLowerCase())
}

export function isFailedStatus(status) {
    const key = String(status || '').toLowerCase()
    return key === 'failed' || key === 'error'
}

/** 是否处于「运行中/排队」这类会持续变化的状态 */
export function isActiveStatus(status) {
    return ['pending', 'queued', 'running', 'translating', 'reviewing', 'polishing'].includes(
        String(status || '').toLowerCase()
    )
}

/** 兼容 ISO 与后端 "YYYY-MM-DD HH:mm:ss" 两种时间格式 */
export function parseDate(value) {
    if (!value) return null
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
    let text = String(value).trim()
    if (/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}/.test(text)) text = text.replace(/\s+/, 'T')
    const date = new Date(text)
    return Number.isNaN(date.getTime()) ? null : date
}

/** 时间：MM-DD HH:mm */
export function formatTime(value) {
    const date = parseDate(value)
    if (!date) return ''
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 耗时：12s / 3m20s / 1h05m */
export function formatDuration(ms) {
    const total = Math.round(Number(ms) / 1000)
    if (!Number.isFinite(total) || total < 0) return ''
    if (total < 60) return `${total}s`
    if (total < 3600) {
        const m = Math.floor(total / 60)
        const s = total % 60
        return `${m}m${s ? String(s).padStart(2, '0') + 's' : ''}`
    }
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    return `${h}h${String(m).padStart(2, '0')}m`
}

/** 评分归一化到 0-100：兼容 0-1 小数与 0-5 分制 */
export function normalizeScore(value) {
    const num = Number(value)
    if (value === null || value === undefined || value === '' || !Number.isFinite(num)) return null
    if (num >= 0 && num <= 1) return Math.round(num * 100)
    if (num > 1 && num <= 5 && !Number.isInteger(num)) return Math.round(num * 20)
    return Math.round(num)
}

/** 截断标题：取前 n 个字符，压缩空白 */
export function truncate(text, n = 20) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim()
    if (!clean) return ''
    return clean.length > n ? `${clean.slice(0, n)}…` : clean
}

/** 段落切分（连续空行视为分段） */
export function splitParagraphs(text) {
    return String(text || '')
        .split(/\n{2,}/)
        .map((part) => part.trim())
        .filter(Boolean)
}

/** 兼容 segments/translations 元素为字符串或对象 */
export function segmentText(item) {
    if (item === null || item === undefined) return ''
    if (typeof item === 'string') return item
    return item.text ?? item.source ?? item.content ?? item.target ?? item.translation ?? ''
}

/** 按目标段数对齐数组 */
export function padArray(list, size, fill = '') {
    const out = Array.isArray(list) ? list.slice(0, size) : []
    while (out.length < size) out.push(fill)
    return out
}