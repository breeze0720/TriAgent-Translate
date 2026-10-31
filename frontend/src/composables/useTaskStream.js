import { onBeforeUnmount, ref } from 'vue'

/**
 * 任务 SSE 订阅
 * - new EventSource('/api/tasks/{id}/events')
 * - 后端使用具名事件 `event: progress`，同时兼容默认 message 事件
 * - 断线后按 1s→8s 退避自动重连
 * - 心跳看门狗：30s 收不到任何数据视为半开连接，主动重连
 * - 收到终态事件后主动关闭，不再重连
 * - 切换任务时先断开旧连接
 */
const LIVENESS_MS = 30000
const WATCHDOG_TICK_MS = 5000
const NAMED_EVENTS = ['progress', 'message']

export function useTaskStream(onEvent) {
    // idle | connecting | open | reconnecting | closed
    const state = ref('idle')

    let source = null
    let timer = null
    let watchdog = null
    let attempts = 0
    let lastActivity = 0
    let currentId = ''
    let stopped = true
    let settled = false

    function clearTimer() {
        if (timer) {
            clearTimeout(timer)
            timer = null
        }
    }

    function stopWatchdog() {
        if (watchdog) {
            clearInterval(watchdog)
            watchdog = null
        }
    }

    function startWatchdog() {
        stopWatchdog()
        watchdog = setInterval(() => {
            if (stopped || settled) return
            if (state.value !== 'open') return
            if (Date.now() - lastActivity > LIVENESS_MS) forceReconnect()
        }, WATCHDOG_TICK_MS)
    }

    function teardown() {
        clearTimer()
        stopWatchdog()
        if (source) {
            source.onopen = null
            source.onmessage = null
            source.onerror = null
            source.close()
            source = null
        }
    }

    function scheduleReconnect() {
        if (stopped || settled) return
        clearTimer()
        attempts += 1
        const delay = Math.min(1000 * attempts, 8000)
        timer = setTimeout(() => {
            timer = null
            connect()
        }, delay)
    }

    // 看门狗触发：立即断开并进入重连
    function forceReconnect() {
        if (stopped || settled) return
        teardown()
        state.value = 'reconnecting'
        scheduleReconnect()
    }

    function handleMessage(message) {
        lastActivity = Date.now()
        let payload
        try {
            payload = JSON.parse(message.data)
        } catch {
            return
        }
        if (!payload || payload.type === 'ping' || payload.ping) return
        onEvent(payload, message)
    }

    function connect() {
        if (stopped || settled || !currentId) return
        state.value = 'connecting'
        lastActivity = Date.now()
        try {
            source = new EventSource(`/api/tasks/${encodeURIComponent(currentId)}/events`)
        } catch {
            state.value = 'reconnecting'
            scheduleReconnect()
            return
        }

        for (const name of NAMED_EVENTS) source.addEventListener(name, handleMessage)
        source.onmessage = handleMessage

        source.onopen = () => {
            attempts = 0
            lastActivity = Date.now()
            state.value = 'open'
            startWatchdog()
        }

        source.onerror = () => {
            teardown()
            if (stopped || settled) return
            state.value = 'reconnecting'
            scheduleReconnect()
        }
    }

    function open(id) {
        close()
        currentId = id ? String(id) : ''
        if (!currentId) return
        stopped = false
        settled = false
        attempts = 0
        connect()
    }

    function close() {
        stopped = true
        settled = false
        teardown()
        currentId = ''
        attempts = 0
        state.value = 'idle'
    }

    // 终态：关闭连接（等待用户重试/恢复后重新 open）
    function markSettled() {
        settled = true
        teardown()
        state.value = 'closed'
    }

    onBeforeUnmount(close)

    return { state, open, close, markSettled }
}