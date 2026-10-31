import { computed, ref, watch } from 'vue'

/**
 * 主题：默认跟随系统，手动切换后写入 localStorage。
 * 手动值：'light' | 'dark'；未手动选择时移除 data-theme，由 prefers-color-scheme 接管。
 */
const STORAGE_KEY = 'ap2-theme'

const readStored = () => {
    try {
        const value = localStorage.getItem(STORAGE_KEY)
        return value === 'light' || value === 'dark' ? value : 'system'
    } catch {
        return 'system'
    }
}

const preference = ref(readStored())

const media = typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null

const systemDark = ref(media ? media.matches : false)

if (media) {
    const onChange = (event) => {
        systemDark.value = event.matches
    }
    if (media.addEventListener) media.addEventListener('change', onChange)
    else if (media.addListener) media.addListener(onChange)
}

const isDark = computed(
    () => preference.value === 'dark' || (preference.value === 'system' && systemDark.value)
)

const applyTheme = () => {
    const root = document.documentElement
    if (preference.value === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', preference.value)
}

applyTheme()
watch(preference, () => {
    applyTheme()
    try {
        if (preference.value === 'system') localStorage.removeItem(STORAGE_KEY)
        else localStorage.setItem(STORAGE_KEY, preference.value)
    } catch {
        /* 隐私模式下忽略持久化失败 */
    }
})

export function useTheme() {
    // 实际生效的主题，用于图标/文案展示
    const theme = computed(() => (isDark.value ? 'dark' : 'light'))

    function toggleTheme() {
        preference.value = isDark.value ? 'light' : 'dark'
    }

    return { theme, preference, isDark, toggleTheme }
}