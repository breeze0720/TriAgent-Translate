import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址：默认 8001（验收用真实后端）；本地自测可用 VITE_API_TARGET 覆盖
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8001'

// 开发环境把 /api 透传到本地后端；SSE 走同一代理，无需额外配置
export default defineConfig({
    plugins: [vue()],
    server: {
        port: 5273,
        strictPort: true,
        proxy: {
            '/api': {
                target: API_TARGET,
                changeOrigin: true,
                // SSE 需要长连接，关闭代理侧的超时/缓冲干扰
                ws: false
            }
        }
    },
    preview: {
        port: 5274,
        proxy: {
            '/api': { target: API_TARGET, changeOrigin: true }
        }
    },
    build: {
        outDir: 'dist',
        sourcemap: false
    }
})