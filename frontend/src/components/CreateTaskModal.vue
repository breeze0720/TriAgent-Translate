<script setup>
/**
 * 新建任务弹层：原文（粘贴文本 / 上传文档）+ 语言/风格/领域 + 是否人工审核
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

const props = defineProps({
    submitting: { type: Boolean, default: false },
    error: { type: String, default: '' },
    // 由 /api/health 带回来的上传限制；拿不到时用下面的兜底值
    limits: { type: Object, default: () => ({}) }
})

const emit = defineEmits(['close', 'submit'])

const textareaRef = ref(null)
const fileInputRef = ref(null)
const localError = ref('')
// 'text' = 粘贴原文；'file' = 上传文档。两种输入最终都变成同一个 source_text
const mode = ref('text')
const file = ref(null)
const dragging = ref(false)

// 兜底值要与后端 config.MAX_UPLOAD_MB / ALLOWED_UPLOAD_EXTENSIONS 默认值一致。
// 前端校验只为「少跑一趟网络」提前提示，真正拦截在后端——前端代码是可以被绕过的。
const FALLBACK_LIMITS = { formats: ['.pdf', '.docx', '.md', '.txt'], max_mb: 20 }
const formats = computed(() => {
    const list = props.limits?.formats
    return Array.isArray(list) && list.length ? list : FALLBACK_LIMITS.formats
})
const maxMb = computed(() => {
    const value = Number(props.limits?.max_mb)
    return value > 0 ? value : FALLBACK_LIMITS.max_mb
})
const accept = computed(() => formats.value.join(','))
const formatLabel = computed(() =>
    formats.value.map((item) => String(item).replace('.', '').toUpperCase()).join(' / ')
)

// 后端直接把语言名写进提示词并落库展示，因此这里用可读名称而不是语言代码
const LANGUAGES = [
    { value: '中文', label: '中文' },
    { value: 'English', label: '英语' },
    { value: '日本語', label: '日语' },
    { value: '한국어', label: '韩语' },
    { value: 'Français', label: '法语' },
    { value: 'Deutsch', label: '德语' },
    { value: 'Español', label: '西班牙语' },
    { value: 'Русский', label: '俄语' }
]

const TARGET_LANGUAGES = LANGUAGES

const STYLES = [
    { value: 'general', label: '通用' },
    { value: 'formal', label: '正式' },
    { value: 'casual', label: '口语' },
    { value: 'technical', label: '技术' },
    { value: 'literary', label: '文学' }
]

const form = reactive({
    source_text: '',
    source_lang: '中文',
    target_lang: 'English',
    style: 'general',
    domain: '',
    enable_human_review: false
})

const charCount = computed(() => form.source_text.trim().length)
const message = computed(() => localError.value || props.error)

function fileExt(name) {
    const value = String(name || '')
    const idx = value.lastIndexOf('.')
    return idx >= 0 ? value.slice(idx).toLowerCase() : ''
}

function formatSize(bytes) {
    if (!bytes) return '0 KB'
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
    return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

const fileMeta = computed(() =>
    file.value ? { name: file.value.name, size: formatSize(file.value.size) } : null
)

// 本地只做「明显不合法」的拦截：扩展名、0 字节、超体积。
// 能不能抽出文字（扫描件 PDF）只有后端解析完才知道，所以那类提示来自接口返回。
function pickFile(next) {
    localError.value = ''
    if (!next) {
        file.value = null
        return
    }
    const ext = fileExt(next.name)
    if (!formats.value.includes(ext)) {
        localError.value = `不支持的文件格式「${ext || '无扩展名'}」，仅支持 ${formatLabel.value}`
        file.value = null
        return
    }
    if (!next.size) {
        localError.value = '这个文件是空的（0 字节），请换一份'
        file.value = null
        return
    }
    if (next.size > maxMb.value * 1024 * 1024) {
        localError.value = `文件过大：${formatSize(next.size)}，超过上限 ${maxMb.value} MB`
        file.value = null
        return
    }
    file.value = next
}

function onFileChange(event) {
    pickFile(event.target.files?.[0] || null)
    // 清空 input，否则「再选同一个文件」不会触发 change
    event.target.value = ''
}

function browse() {
    fileInputRef.value?.click()
}

function clearFile() {
    file.value = null
    localError.value = ''
}

function onDrop(event) {
    dragging.value = false
    pickFile(event.dataTransfer?.files?.[0] || null)
}

function switchMode(next) {
    if (next === mode.value) return
    mode.value = next
    localError.value = ''
    if (next === 'text') nextTick(() => textareaRef.value?.focus())
}

function submit() {
    if (props.submitting) return
    const common = {
        source_lang: form.source_lang,
        target_lang: form.target_lang,
        style: form.style,
        domain: form.domain.trim(),
        enable_human_review: form.enable_human_review
    }
    if (mode.value === 'file') {
        if (!file.value) {
            localError.value = '请先选择要翻译的文档'
            browse()
            return
        }
        localError.value = ''
        emit('submit', { ...common, mode: 'file', file: file.value })
        return
    }
    if (!form.source_text.trim()) {
        localError.value = '请填写需要翻译的原文'
        textareaRef.value?.focus()
        return
    }
    localError.value = ''
    emit('submit', { ...common, mode: 'text', source_text: form.source_text })
}

function requestClose() {
    if (props.submitting) return
    emit('close')
}

function onKeydown(event) {
    if (event.key === 'Escape') requestClose()
}

onMounted(async () => {
    window.addEventListener('keydown', onKeydown)
    await nextTick()
    textareaRef.value?.focus()
})

onBeforeUnmount(() => {
    window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
    <div class="overlay" @click.self="requestClose">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="create-task-title">
            <header class="modal-head">
                <div>
                    <h2 id="create-task-title" class="modal-title">新建翻译任务</h2>
                    <p class="modal-sub">提交后任务将立即进入流水线，可在右侧跟踪实时进度</p>
                </div>
                <button class="btn btn-sm btn-ghost" type="button" aria-label="关闭" @click="requestClose">×</button>
            </header>

            <div class="modal-body">
                <div class="mode-tabs" role="tablist" aria-label="原文输入方式">
                    <button
                        class="mode-tab"
                        :class="{ active: mode === 'text' }"
                        type="button"
                        role="tab"
                        :aria-selected="mode === 'text'"
                        @click="switchMode('text')"
                    >
                        粘贴文本
                    </button>
                    <button
                        class="mode-tab"
                        :class="{ active: mode === 'file' }"
                        type="button"
                        role="tab"
                        :aria-selected="mode === 'file'"
                        @click="switchMode('file')"
                    >
                        上传文档
                    </button>
                </div>

                <div v-if="mode === 'text'" class="field">
                    <label class="field-label" for="source-text">
                        原文 <span class="req">必填</span>
                        <span v-if="charCount" class="count mono">{{ charCount }} 字</span>
                    </label>
                    <textarea
                        id="source-text"
                        ref="textareaRef"
                        v-model="form.source_text"
                        class="textarea"
                        rows="8"
                        placeholder="粘贴需要翻译的原文，支持多段落；段落会作为对照的切分依据。"
                    ></textarea>
                </div>

                <div v-else class="field">
                    <label class="field-label">
                        原文文档 <span class="req">必填</span>
                        <span v-if="fileMeta" class="count mono">{{ fileMeta.size }}</span>
                    </label>
                    <div
                        class="dropzone"
                        :class="{ dragging, filled: !!fileMeta }"
                        role="button"
                        tabindex="0"
                        @click="browse"
                        @keydown.enter.prevent="browse"
                        @keydown.space.prevent="browse"
                        @dragover.prevent="dragging = true"
                        @dragleave.prevent="dragging = false"
                        @drop.prevent="onDrop"
                    >
                        <template v-if="fileMeta">
                            <span class="dz-name mono">{{ fileMeta.name }}</span>
                            <span class="dz-meta">点击可重新选择</span>
                        </template>
                        <template v-else>
                            <span class="dz-title">点击选择文件，或把文件拖进来</span>
                            <span class="dz-meta">支持 {{ formatLabel }}，单文件 ≤ {{ maxMb }} MB</span>
                        </template>
                    </div>
                    <input
                        ref="fileInputRef"
                        class="visually-hidden"
                        type="file"
                        :accept="accept"
                        @change="onFileChange"
                    />
                    <p class="dz-hint">
                        文档里要有可提取的文字；扫描版 / 纯图片 PDF 抽不出内容，会提示换文件。
                        <button v-if="fileMeta" class="dz-clear" type="button" @click.stop="clearFile">
                            清除
                        </button>
                    </p>
                </div>

                <div class="field-grid">
                    <div class="field">
                        <label class="field-label" for="source-lang">源语言</label>
                        <select id="source-lang" v-model="form.source_lang" class="select">
                            <option v-for="item in LANGUAGES" :key="item.value" :value="item.value">
                                {{ item.label }}
                            </option>
                        </select>
                    </div>
                    <div class="field">
                        <label class="field-label" for="target-lang">目标语言</label>
                        <select id="target-lang" v-model="form.target_lang" class="select">
                            <option v-for="item in TARGET_LANGUAGES" :key="item.value" :value="item.value">
                                {{ item.label }}
                            </option>
                        </select>
                    </div>
                    <div class="field">
                        <label class="field-label" for="style">翻译风格</label>
                        <select id="style" v-model="form.style" class="select">
                            <option v-for="item in STYLES" :key="item.value" :value="item.value">
                                {{ item.label }}
                            </option>
                        </select>
                    </div>
                    <div class="field">
                        <label class="field-label" for="domain">领域</label>
                        <input
                            id="domain"
                            v-model="form.domain"
                            class="input"
                            type="text"
                            placeholder="如：法律、医学、计算机（可留空）"
                        />
                    </div>
                </div>

                <button
                    class="switch-row"
                    type="button"
                    role="switch"
                    :aria-checked="form.enable_human_review"
                    @click="form.enable_human_review = !form.enable_human_review"
                >
                    <span class="switch" :class="{ on: form.enable_human_review }" aria-hidden="true">
                        <span class="switch-knob"></span>
                    </span>
                    <span class="switch-text">
                        <span class="switch-title">启用人工审核</span>
                        <span class="switch-desc">译校完成后暂停，由你确认或修改译文；关闭则自动定稿</span>
                    </span>
                </button>

                <p v-if="message" class="form-error">{{ message }}</p>
            </div>

            <footer class="modal-foot">
                <button class="btn" type="button" :disabled="submitting" @click="requestClose">取消</button>
                <button class="btn btn-primary" type="button" :disabled="submitting" @click="submit">
                    {{ submitting ? '创建中…' : '创建任务' }}
                </button>
            </footer>
        </div>
    </div>
</template>

<style scoped>
.modal-head {
    display: flex;
    flex: 0 0 auto;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--border);
}

.modal-title {
    color: var(--text);
    font-size: 14px;
    font-weight: 600;
}

.modal-sub {
    margin-top: 3px;
    color: var(--muted);
    font-size: 11.5px;
    line-height: 1.6;
}

.modal-body {
    flex: 1 1 auto;
    padding: 14px 16px;
    overflow-y: auto;
}

.field + .field,
.field-grid {
    margin-top: 12px;
}

.field-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
}

.field-label {
    display: flex;
    align-items: center;
    gap: 6px;
}

.req {
    color: var(--danger);
    font-size: 10.5px;
}

.count {
    margin-left: auto;
    color: var(--faint);
    font-size: 11px;
}

/* ---- 输入方式切换：粘贴文本 / 上传文档 ---- */
.mode-tabs {
    display: inline-flex;
    gap: 2px;
    margin-bottom: 12px;
    padding: 2px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel-2);
}

.mode-tab {
    padding: 5px 12px;
    border-radius: 5px;
    color: var(--muted);
    font-size: 12px;
    transition: background var(--dur) ease, color var(--dur) ease;
}

.mode-tab:hover {
    color: var(--text);
}

.mode-tab.active {
    background: var(--panel);
    color: var(--text);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}

/* ---- 拖拽上传区 ---- */
.dropzone {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-height: 132px;
    padding: 16px;
    border: 1px dashed var(--border-strong);
    border-radius: var(--radius-sm);
    background: var(--panel-2);
    text-align: center;
    transition: border-color var(--dur) ease, background var(--dur) ease;
    cursor: pointer;
}

.dropzone:hover {
    border-color: var(--accent);
}

.dropzone.dragging {
    border-color: var(--accent);
    background: var(--panel-3);
}

.dropzone.filled {
    border-style: solid;
    border-color: var(--accent);
}

.dz-title {
    color: var(--text);
    font-size: 12.5px;
    font-weight: 500;
}

.dz-name {
    max-width: 100%;
    color: var(--text);
    font-size: 12.5px;
    word-break: break-all;
}

.dz-meta {
    color: var(--muted);
    font-size: 11.5px;
}

.dz-hint {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 6px;
    color: var(--faint);
    font-size: 11px;
    line-height: 1.6;
}

.dz-clear {
    flex: 0 0 auto;
    color: var(--accent);
    font-size: 11px;
    text-decoration: underline;
}

.visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
}

.switch-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    width: 100%;
    margin-top: 14px;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel-2);
    text-align: left;
    transition: border-color var(--dur) ease, background var(--dur) ease;
}

.switch-row:hover {
    border-color: var(--border-strong);
}

.switch {
    position: relative;
    flex: 0 0 auto;
    width: 30px;
    height: 18px;
    margin-top: 2px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    background: var(--panel-3);
    transition: background var(--dur) ease, border-color var(--dur) ease;
}

.switch.on {
    border-color: var(--accent);
    background: var(--accent);
}

.switch-knob {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--panel);
    transition: transform var(--dur) ease;
}

.switch.on .switch-knob {
    transform: translateX(12px);
}

.switch-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.switch-title {
    color: var(--text);
    font-size: 12.5px;
    font-weight: 500;
}

.switch-desc {
    color: var(--muted);
    font-size: 11.5px;
    line-height: 1.55;
}

.form-error {
    margin-top: 10px;
    padding: 7px 9px;
    border: 1px solid var(--danger-border);
    border-radius: var(--radius-sm);
    background: var(--danger-soft);
    color: var(--danger);
    font-size: 12px;
}

.modal-foot {
    display: flex;
    flex: 0 0 auto;
    justify-content: flex-end;
    gap: 8px;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
}

@media (max-width: 560px) {
    .field-grid {
        grid-template-columns: 1fr;
    }
}
</style>