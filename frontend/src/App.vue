<template>
    <!-- ============================================
      App.vue - Root Component (3-Step Wizard)
      Step 1: Input (text + file upload)
      Step 2: Progress (auto-advance on done)
      Step 3: Results (view + email + new translation)
      ============================================ -->
    <div class="header">
        <h1>🤖 多智能体协作翻译助手</h1>
        <p>翻译 Agent (DeepSeek) → 审校 Agent (Qwen) → 润色 Agent (DeepSeek)</p>
    </div>

    <!-- Step Indicator -->
    <div class="step-indicator">
        <div class="step-item">
            <div class="step-circle" :class="stepClass(1)">{{ stepIcon(1) }}</div>
            <div class="step-label" :class="stepLabelClass(1)">输入原文</div>
        </div>
        <div class="step-connector">
            <div class="step-line" :class="{ done: currentStep >= 2 }"></div>
        </div>
        <div class="step-item">
            <div class="step-circle" :class="stepClass(2)">{{ stepIcon(2) }}</div>
            <div class="step-label" :class="stepLabelClass(2)">处理中</div>
        </div>
        <div class="step-connector">
            <div class="step-line" :class="{ done: currentStep >= 3 }"></div>
        </div>
        <div class="step-item">
            <div class="step-circle" :class="stepClass(3)">{{ stepIcon(3) }}</div>
            <div class="step-label" :class="stepLabelClass(3)">查看结果</div>
        </div>
    </div>

    <!-- Step Content -->
    <div class="card">
        <!-- Step 1: Input -->
        <SourceInput
            v-if="currentStep === 1"
            :disabled="false"
            @translate="handleTranslate"
            @translate-file="handleTranslateFile"
        />

        <!-- Step 2: Progress -->
        <ProgressPanel
            v-else-if="currentStep === 2"
            :status="taskStatus"
            :review-score="reviewScore"
            :retry-count="retryCount"
        />

        <!-- Step 3: Results -->
        <ResultCard
            v-else-if="currentStep === 3"
            :task="currentTask"
            @send-email="handleSendEmail"
            @new-translation="handleReset"
        />
    </div>
</template>

<script setup>
import { ref, onUnmounted } from 'vue'
import SourceInput from './components/SourceInput.vue'
import ProgressPanel from './components/ProgressPanel.vue'
import ResultCard from './components/ResultCard.vue'

// ==================== State ====================
const currentStep = ref(1)
const taskStatus = ref('idle')
const reviewScore = ref(0)
const retryCount = ref(0)
const currentTask = ref(null)
let pollingTimer = null

// ==================== Step indicator helpers ====================
function stepClass(step) {
    if (currentStep.value > step) return 'done'
    if (currentStep.value === step) return 'current'
    return ''
}

function stepLabelClass(step) {
    if (currentStep.value > step) return 'done'
    if (currentStep.value === step) return 'current'
    return ''
}

function stepIcon(step) {
    if (currentStep.value > step) return '✓'
    return step
}

// ==================== Text Translation ====================
async function handleTranslate({ sourceText, sourceLang, targetLang }) {
    currentStep.value = 2
    taskStatus.value = 'pending'
    reviewScore.value = 0
    retryCount.value = 0
    currentTask.value = null

    try {
        const res = await fetch('/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_text: sourceText,
                source_lang: sourceLang,
                target_lang: targetLang,
            }),
        })
        const data = await res.json()

        if (!data.success) {
            alert('Failed to create task: ' + data.message)
            currentStep.value = 1
            return
        }

        startPolling(data.task_id)
    } catch (e) {
        alert('Network error. Is the backend running?')
        currentStep.value = 1
    }
}

// ==================== File Translation ====================
async function handleTranslateFile({ file, sourceLang, targetLang }) {
    currentStep.value = 2
    taskStatus.value = 'pending'
    reviewScore.value = 0
    retryCount.value = 0
    currentTask.value = null

    try {
        const formData = new FormData()
        formData.append('file', file)
        formData.append('source_lang', sourceLang)
        formData.append('target_lang', targetLang)

        const res = await fetch('/translate-file', {
            method: 'POST',
            body: formData,
        })
        const data = await res.json()

        if (!data.success) {
            alert('Upload failed: ' + data.message)
            currentStep.value = 1
            return
        }

        startPolling(data.task_id)
    } catch (e) {
        alert('Network error. Is the backend running?')
        currentStep.value = 1
    }
}

// ==================== Polling ====================
function startPolling(taskId) {
    if (pollingTimer) clearInterval(pollingTimer)

    pollingTimer = setInterval(async () => {
        try {
            const res = await fetch(`/task/${taskId}`)
            const data = await res.json()

            if (!data.success || !data.task) return

            const task = data.task
            currentTask.value = task
            taskStatus.value = task.status
            reviewScore.value = task.review_score || 0
            retryCount.value = task.retry_count || 0

            // Auto-advance to Step 3 when done or failed
            if (task.status === 'done' || task.status === 'failed') {
                clearInterval(pollingTimer)
                pollingTimer = null
                // Small delay so user sees the final state before transition
                setTimeout(() => { currentStep.value = 3 }, 800)
            }
        } catch (e) {
            console.error('Polling error:', e)
        }
    }, 1500)
}

// ==================== Email ====================
async function handleSendEmail(email) {
    if (!currentTask.value) return

    try {
        const res = await fetch(`/task/${currentTask.value.id}/send-email`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: email }),
        })
        const data = await res.json()
        alert(data.success ? '[OK] ' + data.message : '[FAIL] ' + data.message)
    } catch (e) {
        alert('Email sending failed: network error')
    }
}

// ==================== Reset ====================
function handleReset() {
    currentStep.value = 1
    currentTask.value = null
    taskStatus.value = 'idle'
    reviewScore.value = 0
    retryCount.value = 0
}

onUnmounted(() => {
    if (pollingTimer) clearInterval(pollingTimer)
})
</script>

<style scoped>
/* Step indicator uses global styles in style.css */
.card {
    min-height: 400px;
}
</style>
