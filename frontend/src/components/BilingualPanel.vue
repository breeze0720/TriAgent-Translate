<script setup>
/**
 * 原文 / 译文对照：支持「并排」「上下」两种布局，人工审核阶段译文可直接编辑
 */
import { computed } from 'vue'

const props = defineProps({
    segments: { type: Array, default: () => [] },
    translations: { type: Array, default: () => [] },
    layout: { type: String, default: 'side' },
    editable: { type: Boolean, default: false },
    running: { type: Boolean, default: false }
})

const emit = defineEmits(['update:translations', 'update:layout'])

const rows = computed(() => {
    const size = Math.max(props.segments.length, props.translations.length)
    return Array.from({ length: size }, (_, index) => ({
        index,
        source: props.segments[index] ?? '',
        target: props.translations[index] ?? ''
    }))
})

const padIndex = (index) => String(index + 1).padStart(2, '0')

const setTarget = (index, value) => {
    const next = props.translations.slice()
    while (next.length <= index) next.push('')
    next[index] = value
    emit('update:translations', next)
}

const onInput = (index, event) => setTarget(index, event.target.value)

const targetPlaceholder = computed(() => (props.running ? '译文生成中…' : '在此输入译文'))
</script>

<template>
    <section class="card">
        <header class="card-head">
            <h2 class="card-title">原文 / 译文对照</h2>
            <span class="card-hint">{{ rows.length }} 段</span>
            <span v-if="editable" class="edit-hint">可直接修改译文</span>
            <span v-else class="card-hint readonly">只读</span>

            <div class="seg-toggle" role="group" aria-label="对照布局">
                <button
                    type="button"
                    :class="{ on: layout === 'side' }"
                    @click="emit('update:layout', 'side')"
                >
                    并排
                </button>
                <button
                    type="button"
                    :class="{ on: layout === 'stack' }"
                    @click="emit('update:layout', 'stack')"
                >
                    上下
                </button>
            </div>
        </header>

        <div class="card-body">
            <div v-if="!rows.length" class="state state-compact">
                <p class="state-title">暂无原文</p>
                <p class="state-desc">任务创建后会在此显示分段原文与对应译文。</p>
            </div>

            <template v-else>
                <div v-if="layout === 'side'" class="compare-head">
                    <span>原文</span>
                    <span>译文</span>
                </div>

                <div class="compare-list" :class="layout">
                    <div
                        v-for="row in rows"
                        :key="row.index"
                        class="compare-row"
                        :class="[layout, { empty: !row.source }]"
                    >
                        <div class="cell source">
                            <span class="seg-no mono">{{ padIndex(row.index) }}</span>
                            <p class="seg-text">{{ row.source || '——' }}</p>
                        </div>
                        <div class="cell target">
                            <span class="seg-no mono">{{ padIndex(row.index) }}</span>
                            <textarea
                                v-if="editable"
                                class="seg-edit"
                                :value="row.target"
                                rows="2"
                                spellcheck="false"
                                :placeholder="targetPlaceholder"
                                @input="onInput(row.index, $event)"
                            ></textarea>
                            <p v-else class="seg-text" :class="{ pending: !row.target }">
                                {{ row.target || (running ? '译文生成中…' : '——') }}
                            </p>
                        </div>
                    </div>
                </div>
            </template>
        </div>
    </section>
</template>

<style scoped>
.edit-hint {
    color: var(--warn);
    font-size: 11px;
}

.readonly {
    color: var(--faint);
}

.seg-toggle {
    display: inline-flex;
    gap: 2px;
    margin-left: auto;
    padding: 2px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel-2);
}

.seg-toggle button {
    height: 20px;
    padding: 0 8px;
    border-radius: 4px;
    color: var(--muted);
    font-size: 11px;
    transition: background var(--dur) ease, color var(--dur) ease;
}

.seg-toggle button:hover {
    color: var(--text);
}

.seg-toggle button.on {
    background: var(--panel);
    box-shadow: var(--shadow);
    color: var(--text);
}

.compare-head {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    padding: 0 0 6px 34px;
    color: var(--faint);
    font-size: 11px;
}

.compare-row {
    display: grid;
    gap: 16px;
    padding: 9px 0;
    border-top: 1px solid var(--border);
}

.compare-row.side {
    grid-template-columns: 1fr 1fr;
}

.compare-row.stack {
    gap: 7px;
    grid-template-columns: 1fr;
}

.cell {
    position: relative;
    min-width: 0;
    padding-left: 34px;
}

.seg-no {
    position: absolute;
    top: 1px;
    left: 0;
    color: var(--faint);
    font-size: 10.5px;
}

.seg-text {
    color: var(--text-2);
    font-size: 12.5px;
    line-height: 1.72;
    white-space: pre-wrap;
    word-break: break-word;
}

.cell.target .seg-text {
    color: var(--text);
}

.seg-text.pending {
    color: var(--faint);
}

.seg-edit {
    display: block;
    width: 100%;
    min-height: 2.9em;
    padding: 4px 6px;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: var(--panel-2);
    color: var(--text);
    font: inherit;
    font-size: 12.5px;
    line-height: 1.72;
    field-sizing: content;
    resize: vertical;
    transition: border-color var(--dur) ease, background var(--dur) ease;
}

.seg-edit:hover {
    border-color: var(--border-strong);
}

.seg-edit:focus {
    outline: none;
    border-color: var(--accent);
    background: var(--panel);
    box-shadow: 0 0 0 3px var(--accent-ring);
}

.compare-row.empty .cell.source .seg-text {
    color: var(--faint);
}
</style>