<template>
    <!-- ============================================
      ProgressPanel.vue - Step 2: Workflow Progress
      Shows 3-stage pipeline with live status updates
      Auto-advances to Step 3 when workflow completes
      ============================================ -->
    <div class="progress-root fade-in">
        <div class="card-title">📊 工作流进度</div>

        <!-- 3 Stages -->
        <div class="stages">
            <!-- Stage 1: Translation -->
            <div class="stage-row" :class="stageClass('translating')">
                <div class="stage-icon-box">
                    <span v-if="isStageDone('translating')" class="stage-done-icon">✓</span>
                    <span v-else-if="isStageActive('translating')" class="pulse">🔄</span>
                    <span v-else class="stage-pending-icon">1</span>
                </div>
                <div class="stage-info">
                    <div class="stage-name">翻译 Agent</div>
                    <div class="stage-desc">
                        <span class="stage-model">DeepSeek</span>
                        <span v-if="isStageActive('translating')" class="stage-status running">翻译中...</span>
                        <span v-else-if="isStageDone('translating')" class="stage-status done">已完成</span>
                    </div>
                </div>
            </div>

            <div class="stage-connector" :class="{ filled: isStageDone('translating') }">
                <div class="connector-line"></div>
            </div>

            <!-- Stage 2: Review -->
            <div class="stage-row" :class="stageClass('reviewing')">
                <div class="stage-icon-box">
                    <span v-if="isStageDone('reviewing')" class="stage-done-icon">✓</span>
                    <span v-else-if="isStageActive('reviewing')" class="pulse">🔄</span>
                    <span v-else class="stage-pending-icon">2</span>
                </div>
                <div class="stage-info">
                    <div class="stage-name">审校 Agent</div>
                    <div class="stage-desc">
                        <span class="stage-model">Qwen 3.7-max</span>
                        <span v-if="isStageActive('reviewing')" class="stage-status running">审校中...</span>
                        <span v-else-if="isStageDone('reviewing') && reviewScore > 0" class="stage-status done">
                            评分: {{ reviewScore }}/100
                        </span>
                        <span v-else-if="isStageDone('reviewing')" class="stage-status done">已完成</span>
                    </div>
                </div>
            </div>

            <div class="stage-connector" :class="{ filled: isStageDone('reviewing') }">
                <div class="connector-line"></div>
            </div>

            <!-- Stage 3: Polish -->
            <div class="stage-row" :class="stageClass('polishing')">
                <div class="stage-icon-box">
                    <span v-if="isStageDone('polishing')" class="stage-done-icon">✓</span>
                    <span v-else-if="isStageActive('polishing')" class="pulse">🔄</span>
                    <span v-else class="stage-pending-icon">3</span>
                </div>
                <div class="stage-info">
                    <div class="stage-name">润色 Agent</div>
                    <div class="stage-desc">
                        <span class="stage-model">DeepSeek</span>
                        <span v-if="isStageActive('polishing')" class="stage-status running">润色中...</span>
                        <span v-else-if="isStageDone('polishing')" class="stage-status done">已完成</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Score Bar -->
        <div v-if="reviewScore > 0" class="score-card fade-in">
            <div class="score-header">
                <span class="score-label">审校评分</span>
                <span class="score-value" :class="scoreColor">{{ reviewScore }}</span>
                <span class="score-unit">/ 100</span>
            </div>
            <div class="score-bar-bg">
                <div class="score-bar-fill" :class="scoreColor" :style="{ width: reviewScore + '%' }"></div>
            </div>
            <div v-if="retryCount > 0" class="retry-info">
                🔄 第 {{ retryCount }} 次重试（评分低于 80 分自动优化）
            </div>
        </div>

        <!-- Done state -->
        <div v-if="status === 'done'" class="done-card fade-in">
            <span class="done-icon"></span>
            <span class="done-text">翻译完成！正在跳转到结果页...</span>
        </div>

        <!-- Failed state -->
        <div v-if="status === 'failed'" class="failed-card fade-in">
            <span class="failed-icon">⚠️</span>
            <span class="failed-text">审校未达标（3次重试后仍未通过）<br>将跳转到结果页查看详情</span>
        </div>
    </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
    status: { type: String, default: 'idle' },
    reviewScore: { type: Number, default: 0 },
    retryCount: { type: Number, default: 0 },
})

const STAGE_ORDER = ['pending', 'translating', 'reviewing', 'polishing', 'done']

function isStageDone(stepStatus) {
    const curIdx = STAGE_ORDER.indexOf(props.status)
    const stepIdx = STAGE_ORDER.indexOf(stepStatus)
    return curIdx > stepIdx || props.status === 'done'
}

function isStageActive(stepStatus) {
    return props.status === stepStatus
}

function stageClass(stepStatus) {
    if (isStageDone(stepStatus)) return 'stage-done'
    if (isStageActive(stepStatus)) return 'stage-active'
    return ''
}

const scoreColor = computed(() => {
    if (props.reviewScore >= 90) return 'score-high'
    if (props.reviewScore >= 80) return 'score-mid'
    return 'score-low'
})
</script>

<style scoped>
.progress-root { }

/* Stages */
.stages {
    padding: 8px 0 0;
}

.stage-row {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 16px 20px;
    border-radius: 14px;
    transition: all 0.3s;
}

.stage-row.stage-active {
    background: linear-gradient(135deg, #f8f7ff 0%, #f0efff 100%);
    border: 1px solid #e0dbff;
}

.stage-row.stage-done .stage-icon-box {
    background: #e6f7ed;
}

.stage-icon-box {
    width: 46px;
    height: 46px;
    border-radius: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: 700;
    background: #f3f3f3;
    color: #bbb;
    flex-shrink: 0;
    transition: all 0.3s;
}

.stage-row.stage-active .stage-icon-box {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    box-shadow: 0 0 0 6px rgba(102, 126, 234, 0.1);
}

.stage-row.stage-done .stage-icon-box {
    background: #e6f7ed;
    color: #52c41a;
}

.stage-pending-icon { color: #bbb; }
.stage-done-icon { color: #52c41a; font-size: 20px; }

.stage-info { flex: 1; }
.stage-name { font-size: 15px; font-weight: 600; color: #333; }
.stage-desc { font-size: 12px; color: #999; margin-top: 3px; display: flex; gap: 10px; align-items: center; }
.stage-model { color: #bbb; }

.stage-status {
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 500;
}

.stage-status.running { background: #fff7e6; color: #fa8c16; }
.stage-status.done { background: #e6f7ed; color: #52c41a; }

/* Connector */
.stage-connector {
    display: flex;
    justify-content: center;
    padding: 0 0 0 43px;
}

.connector-line {
    width: 2px;
    height: 20px;
    background: #e8e8e8;
    border-radius: 1px;
    transition: background 0.4s;
}

.stage-connector.filled .connector-line {
    background: #52c41a;
}

/* Score Card */
.score-card {
    margin-top: 20px;
    padding: 18px 20px;
    background: #fafafa;
    border-radius: 14px;
}

.score-header {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-bottom: 10px;
}

.score-label { font-size: 13px; color: #999; }
.score-value { font-size: 32px; font-weight: 700; line-height: 1; }
.score-value.score-high { color: #52c41a; }
.score-value.score-mid { color: #faad14; }
.score-value.score-low { color: #ff4d4f; }
.score-unit { font-size: 13px; color: #bbb; }

.score-bar-bg {
    height: 8px;
    background: #e8e8e8;
    border-radius: 4px;
    overflow: hidden;
}

.score-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.6s ease;
}

.score-bar-fill.score-high { background: linear-gradient(90deg, #52c41a, #73d13d); }
.score-bar-fill.score-mid { background: linear-gradient(90deg, #faad14, #ffc53d); }
.score-bar-fill.score-low { background: linear-gradient(90deg, #ff4d4f, #ff7875); }

.retry-info { margin-top: 8px; font-size: 12px; color: #faad14; }

/* Done / Failed */
.done-card {
    margin-top: 24px;
    padding: 20px;
    background: #f6ffed;
    border-radius: 14px;
    text-align: center;
    border: 1px solid #b7eb8f;
}

.done-icon { font-size: 28px; }
.done-text { font-size: 15px; color: #52c41a; font-weight: 500; display: block; margin-top: 8px; }

.failed-card {
    margin-top: 24px;
    padding: 20px;
    background: #fff1f0;
    border-radius: 14px;
    text-align: center;
    border: 1px solid #ffa39e;
}

.failed-icon { font-size: 28px; }
.failed-text { font-size: 14px; color: #ff4d4f; display: block; margin-top: 8px; line-height: 1.6; }

/* Animation */
.pulse { animation: pulse 1.2s infinite; }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
}
</style>
