<script setup>
/**
 * 顶栏：品牌、后端连接状态、主题切换
 */
const props = defineProps({
    theme: { type: String, default: 'light' },
    backendOnline: { type: Boolean, default: true }
})

defineEmits(['toggle-theme'])

const themeText = () => (props.theme === 'dark' ? '深色' : '浅色')
const themeIcon = () => (props.theme === 'dark' ? '☾' : '☀')
</script>

<template>
    <header class="app-header">
        <div class="brand">
            <span class="brand-mark" aria-hidden="true">译</span>
            <span class="brand-name">译笔工作台</span>
            <span class="brand-sep" aria-hidden="true">/</span>
            <span class="brand-sub">多智能体翻译与审校</span>
        </div>

        <div class="header-right">
            <span
                class="backend"
                :class="{ offline: !backendOnline }"
                :title="backendOnline ? '后端 API 连接正常' : '后端 API 无法连接，请检查服务是否启动'"
            >
                <span class="backend-dot" aria-hidden="true"></span>
                <span>{{ backendOnline ? '后端已连接' : '后端未连接' }}</span>
            </span>

            <button
                class="btn btn-sm"
                type="button"
                :title="`当前主题：${themeText()}，点击切换`"
                @click="$emit('toggle-theme')"
            >
                <span class="theme-icon" aria-hidden="true">{{ themeIcon() }}</span>
                <span>{{ themeText() }}</span>
            </button>
        </div>
    </header>
</template>