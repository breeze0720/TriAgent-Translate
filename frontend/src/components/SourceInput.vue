<template>
    <!-- ============================================
      SourceInput.vue - Step 1: Input
      Two modes: text input + file upload (with tabs)
      ============================================ -->
    <div class="source-root fade-in">
        <div class="card-title">📝 输入原文</div>

        <!-- Mode Tabs -->
        <div class="mode-tabs">
            <button
                class="mode-tab"
                :class="{ active: inputMode === 'text' }"
                @click="inputMode = 'text'"
            >⌨️ 文本输入</button>
            <button
                class="mode-tab"
                :class="{ active: inputMode === 'file' }"
                @click="inputMode = 'file'"
            >📎 文件上传</button>
        </div>

        <!-- Language Selection -->
        <div class="lang-row">
            <div class="lang-item">
                <label>源语言</label>
                <select v-model="sourceLang" class="select">
                    <option value="zh">中文</option>
                    <option value="en">English</option>
                    <option value="ja">日本語</option>
                    <option value="ko">한국어</option>
                    <option value="fr">Français</option>
                    <option value="de">Deutsch</option>
                </select>
            </div>

            <div class="swap-icon" @click="swapLang">⇄</div>

            <div class="lang-item">
                <label>目标语言</label>
                <select v-model="targetLang" class="select">
                    <option value="en">English</option>
                    <option value="zh">中文</option>
                    <option value="ja">日本語</option>
                    <option value="ko">한국어</option>
                    <option value="fr">Français</option>
                    <option value="de">Deutsch</option>
                </select>
            </div>
        </div>

        <!-- Mode: Text Input -->
        <div v-if="inputMode === 'text'" class="fade-in">
            <textarea
                v-model="sourceText"
                class="textarea"
                placeholder="请输入需要翻译的文本..."
                rows="7"
            ></textarea>
            <div class="char-count">{{ sourceText.length }} / 5000</div>
        </div>

        <!-- Mode: File Upload -->
        <div v-else class="fade-in">
            <div
                class="upload-zone"
                :class="{ dragging: isDragging, 'has-file': uploadedFile }"
                @click="$refs.fileInput.click()"
                @dragover.prevent="isDragging = true"
                @dragleave="isDragging = false"
                @drop.prevent="handleDrop"
            >
                <div v-if="uploadedFile" class="upload-file-info">
                    <span class="file-icon">📄</span>
                    <span class="file-name">{{ uploadedFile.name }}</span>
                    <span class="file-size">{{ formatSize(uploadedFile.size) }}</span>
                    <button class="file-remove" @click.stop="removeFile">✕</button>
                </div>
                <div v-else class="upload-placeholder">
                    <div class="upload-icon">📁</div>
                    <div class="upload-text">点击上传或拖拽文件到此处</div>
                    <div class="upload-hint">支持 PDF / Word / Markdown / TXT</div>
                </div>
            </div>
            <input
                ref="fileInput"
                type="file"
                accept=".pdf,.docx,.md,.txt"
                style="display:none"
                @change="handleFileSelect"
            />
        </div>

        <!-- Submit -->
        <button
            class="btn btn-primary submit-btn"
            :disabled="!canSubmit"
            @click="submitTranslation"
        >
             开始翻译
        </button>

        <!-- Hint -->
        <div class="hint">
        </div>
    </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const emit = defineEmits(['translate', 'translateFile'])

// Modes
const inputMode = ref('text')  // 'text' | 'file'

// Language
const sourceLang = ref('zh')
const targetLang = ref('en')

function swapLang() {
    const tmp = sourceLang.value
    sourceLang.value = targetLang.value
    targetLang.value = tmp
}

// Text input
const sourceText = ref('')

// File upload
const uploadedFile = ref(null)
const isDragging = ref(false)

function handleDrop(e) {
    isDragging.value = false
    const files = e.dataTransfer.files
    if (files.length > 0) processFile(files[0])
}

function handleFileSelect(e) {
    const files = e.target.files
    if (files.length > 0) processFile(files[0])
    e.target.value = ''
}

function processFile(file) {
    // Validate type
    const allowed = ['.pdf', '.docx', '.md', '.txt']
    const ext = '.' + file.name.split('.').pop().toLowerCase()
    if (!allowed.includes(ext)) {
        alert(`Unsupported format "${ext}". Supports: ${allowed.join(', ')}`)
        return
    }
    // Validate size (10MB max)
    if (file.size > 10 * 1024 * 1024) {
        alert('File too large. Maximum size is 10MB.')
        return
    }
    uploadedFile.value = file
}

function removeFile() {
    uploadedFile.value = null
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B'
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}

// Can submit?
const canSubmit = computed(() => {
    if (inputMode.value === 'text') return sourceText.value.trim().length > 0
    return uploadedFile.value !== null
})

// Submit
function submitTranslation() {
    if (inputMode.value === 'text') {
        emit('translate', {
            sourceText: sourceText.value.trim(),
            sourceLang: sourceLang.value,
            targetLang: targetLang.value,
        })
    } else {
        emit('translateFile', {
            file: uploadedFile.value,
            sourceLang: sourceLang.value,
            targetLang: targetLang.value,
        })
    }
}
</script>

<style scoped>
.source-root {
    /* nothing extra needed */
}

.mode-tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 18px;
}

.mode-tab {
    flex: 1;
    padding: 9px 0;
    border: 1.5px solid #e0e0e0;
    border-radius: 10px;
    background: #fafafa;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
    color: #888;
}

.mode-tab.active {
    border-color: #667eea;
    background: #f0f0ff;
    color: #667eea;
    font-weight: 600;
}

.mode-tab:hover:not(.active) {
    border-color: #d0d0d0;
}

.lang-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
}

.lang-item { flex: 1; }

.lang-item label {
    display: block;
    font-size: 12px;
    color: #999;
    margin-bottom: 4px;
}

.lang-item .select { width: 100%; }

.swap-icon {
    font-size: 18px;
    color: #667eea;
    cursor: pointer;
    user-select: none;
    padding: 0 4px;
    margin-top: 18px;
    transition: transform 0.2s;
}

.swap-icon:hover { transform: scale(1.2); }

.char-count {
    text-align: right;
    font-size: 12px;
    color: #bbb;
    margin-top: 6px;
}

/* Upload Zone */
.upload-zone {
    border: 2px dashed #d9d9d9;
    border-radius: 14px;
    padding: 36px 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s;
    background: #fafafa;
}

.upload-zone:hover,
.upload-zone.dragging {
    border-color: #667eea;
    background: #f0f0ff;
}

.upload-zone.has-file {
    border-style: solid;
    border-color: #52c41a;
    background: #f6ffed;
    padding: 20px;
}

.upload-placeholder {
    color: #999;
}

.upload-icon { font-size: 36px; margin-bottom: 8px; }
.upload-text { font-size: 14px; }
.upload-hint { font-size: 12px; color: #bbb; margin-top: 6px; }

.upload-file-info {
    display: flex;
    align-items: center;
    gap: 10px;
}

.file-icon { font-size: 24px; }
.file-name {
    flex: 1;
    text-align: left;
    font-weight: 500;
    font-size: 14px;
    color: #333;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.file-size { font-size: 12px; color: #999; }
.file-remove {
    background: none;
    border: none;
    color: #ff4d4f;
    cursor: pointer;
    font-size: 16px;
    padding: 4px 8px;
    border-radius: 6px;
}

.file-remove:hover { background: #fff1f0; }

/* Submit */
.submit-btn {
    width: 100%;
    margin-top: 18px;
    padding: 13px;
    font-size: 16px;
}

.hint {
    margin-top: 14px;
    padding: 10px;
    background: #f9f9ff;
    border-radius: 10px;
    font-size: 13px;
    color: #888;
    text-align: center;
}
</style>
