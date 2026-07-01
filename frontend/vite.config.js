import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
    plugins: [vue()],
    server: {
        port: 3000,
        // 代理配置：将 /api 开头的请求转发到后端 8000 端口
        proxy: {
            '/translate': 'http://127.0.0.1:8000',
            '/task': 'http://127.0.0.1:8000',
            '/tasks': 'http://127.0.0.1:8000',
            '/status': 'http://127.0.0.1:8000',
        }
    }
})
