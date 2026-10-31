import { ref } from 'vue'

/**
 * 轻量通知栈，替代 alert；错误类停留更久。
 */
export function useNotifications() {
    const notices = ref([])
    let seq = 0

    function dismiss(id) {
        notices.value = notices.value.filter((item) => item.id !== id)
    }

    function push(text, tone = 'info', ttl) {
        const message = String(text || '').trim()
        if (!message) return
        const id = ++seq
        notices.value.push({ id, tone, text: message })
        // 最多保留 4 条，避免堆叠遮挡工作区
        if (notices.value.length > 4) notices.value.shift()
        const lifetime = ttl ?? (tone === 'error' ? 8000 : 4200)
        window.setTimeout(() => dismiss(id), lifetime)
    }

    return { notices, push, dismiss }
}