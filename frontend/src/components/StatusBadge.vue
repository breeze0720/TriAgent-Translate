<script setup>
/**
 * 状态徽标：排队 / 翻译中 / 待人工 / 完成 / 失败
 */
import { computed } from 'vue'
import { statusMeta } from '../utils/format'

const props = defineProps({
    status: { type: String, default: '' }
})

const meta = computed(() => statusMeta(props.status))
</script>

<template>
    <span class="badge" :class="`tone-${meta.tone}`">
        <span class="badge-dot" aria-hidden="true"></span>{{ meta.label }}
    </span>
</template>