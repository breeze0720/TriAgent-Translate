<script setup>
/**
 * 右栏术语表：term / translation / note 可编辑，支持新增行与保存回写
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
    glossary: { type: Array, default: () => [] },
    saving: { type: Boolean, default: false },
    error: { type: String, default: '' },
    disabled: { type: Boolean, default: false }
})

const emit = defineEmits(['save'])

const rows = ref([])
const baseline = ref('[]')
const localError = ref('')

const cleanRows = (list) =>
    (list || [])
        .map((item) => ({
            term: String(item?.term ?? '').trim(),
            translation: String(item?.translation ?? '').trim(),
            note: String(item?.note ?? '').trim()
        }))
        .filter((item) => item.term || item.translation || item.note)

function resetFromProps() {
    rows.value = (props.glossary || []).map((item) => ({
        term: item?.term ?? '',
        translation: item?.translation ?? '',
        note: item?.note ?? ''
    }))
    baseline.value = JSON.stringify(cleanRows(rows.value))
    localError.value = ''
}

watch(() => props.glossary, resetFromProps, { immediate: true, deep: true })

const dirty = computed(() => JSON.stringify(cleanRows(rows.value)) !== baseline.value)
const shownError = computed(() => localError.value || props.error)

function addRow() {
    if (props.disabled) return
    rows.value.push({ term: '', translation: '', note: '' })
}

function removeRow(index) {
    rows.value.splice(index, 1)
}

function save() {
    if (props.disabled || props.saving || !dirty.value) return
    const payload = cleanRows(rows.value)
    // 后端 GlossaryTerm 要求 term 与 translation 均非空
    if (payload.some((item) => !item.term || !item.translation)) {
        localError.value = '术语与译文均为必填，请补全或删除该行'
        return
    }
    localError.value = ''
    emit('save', payload)
}
</script>

<template>
    <section class="card glossary">
        <header class="card-head">
            <h2 class="card-title">术语表</h2>
            <span class="card-hint mono">{{ rows.length }}</span>
            <span v-if="dirty" class="dirty-dot" title="有未保存的修改"></span>
            <button class="btn btn-sm btn-ghost add-btn" type="button" :disabled="disabled" @click="addRow">
                ＋ 新增
            </button>
        </header>

        <div class="card-body">
            <div v-if="disabled" class="state state-compact">
                <p class="state-desc">选中任务后可查看与维护术语表。</p>
            </div>

            <div v-else-if="!rows.length" class="state state-compact">
                <p class="state-title">暂无术语</p>
                <p class="state-desc">术语抽取完成后会自动写入，也可点击「新增」手动补充。</p>
            </div>

            <div v-else class="glossary-list">
                <div v-for="(row, index) in rows" :key="index" class="glossary-row">
                    <div class="glossary-grid">
                        <input
                            v-model="row.term"
                            class="input input-sm"
                            type="text"
                            placeholder="术语"
                            aria-label="术语原文"
                            maxlength="200"
                        />
                        <input
                            v-model="row.translation"
                            class="input input-sm"
                            type="text"
                            placeholder="译文"
                            aria-label="术语译文"
                            maxlength="200"
                        />
                        <input
                            v-model="row.note"
                            class="input input-sm note-input"
                            type="text"
                            placeholder="备注（可选）"
                            aria-label="术语备注"
                            maxlength="500"
                        />
                    </div>
                    <button
                        class="row-del"
                        type="button"
                        aria-label="删除该术语"
                        @click="removeRow(index)"
                    >
                        ×
                    </button>
                </div>
            </div>

            <p v-if="shownError" class="glossary-error">{{ shownError }}</p>

            <div class="glossary-foot">
                <span class="foot-hint">
                    {{ dirty ? '有未保存的修改' : rows.length ? '已同步' : '' }}
                </span>
                <button class="btn btn-sm" type="button" :disabled="!dirty || saving" @click="resetFromProps">
                    撤销
                </button>
                <button
                    class="btn btn-sm btn-primary"
                    type="button"
                    :disabled="disabled || !dirty || saving"
                    @click="save"
                >
                    {{ saving ? '保存中…' : '保存' }}
                </button>
            </div>
        </div>
    </section>
</template>

<style scoped>
.add-btn {
    margin-left: auto;
}

.dirty-dot {
    width: 5px;
    height: 5px;
    margin-left: -3px;
    border-radius: 50%;
    background: var(--warn);
}

.glossary-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.glossary-row {
    position: relative;
    padding-right: 16px;
}

.glossary-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 5px;
}

.input-sm {
    padding: 4px 6px;
    font-size: 11.5px;
}

.note-input {
    grid-column: 1 / -1;
}

.row-del {
    position: absolute;
    top: 3px;
    right: -2px;
    width: 16px;
    height: 16px;
    border-radius: var(--radius-sm);
    color: var(--faint);
    font-size: 12px;
    line-height: 1;
    opacity: 0;
    transition: opacity var(--dur) ease, background var(--dur) ease, color var(--dur) ease;
}

.glossary-row:hover .row-del {
    opacity: 1;
}

.row-del:hover {
    background: var(--danger-soft);
    color: var(--danger);
}

.glossary-error {
    margin-top: 8px;
    color: var(--danger);
    font-size: 11.5px;
    line-height: 1.55;
}

.glossary-foot {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 10px;
    padding-top: 9px;
    border-top: 1px solid var(--border);
}

.foot-hint {
    flex: 1 1 auto;
    color: var(--faint);
    font-size: 11px;
}
</style>