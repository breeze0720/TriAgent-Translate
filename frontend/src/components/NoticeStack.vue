<script setup>
/**
 * 通知栈：右下角浮层，替代 alert
 */
const props = defineProps({
    items: { type: Array, default: () => [] }
})

const emit = defineEmits(['dismiss'])

const tagOf = (tone) => ({ success: '完成', error: '错误', warn: '注意' }[tone] || '提示')
</script>

<template>
    <div class="notices" role="status" aria-live="polite">
        <TransitionGroup name="notice">
            <div v-for="item in props.items" :key="item.id" class="notice" :class="`notice-${item.tone}`">
                <span class="notice-tag">{{ tagOf(item.tone) }}</span>
                <span class="notice-text">{{ item.text }}</span>
                <button class="notice-close" type="button" aria-label="关闭通知" @click="emit('dismiss', item.id)">
                    ×
                </button>
            </div>
        </TransitionGroup>
    </div>
</template>