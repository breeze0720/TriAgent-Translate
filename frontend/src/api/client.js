/**
 * 后端 API 客户端
 * 开发环境经 vite proxy 转发到 127.0.0.1:8001，生产环境同源部署。
 */

const API_BASE = import.meta.env.VITE_API_BASE || ''

export class ApiError extends Error {
    constructor(message, status = 0) {
        super(message)
        this.name = 'ApiError'
        this.status = status
    }
}

async function readBody(res) {
    const text = await res.text()
    if (!text) return null
    try {
        return JSON.parse(text)
    } catch {
        return text
    }
}

// 后端错误结构可能是 {detail}/{message}/{error}，detail 也可能是 FastAPI 校验数组
function describeError(data, fallback) {
    if (!data) return fallback
    if (typeof data === 'string') return data.slice(0, 300) || fallback
    const detail = data.detail ?? data.message ?? data.error
    if (!detail) return fallback
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
        const parts = detail.map((item) => {
            if (typeof item === 'string') return item
            const where = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : ''
            return [where, item?.msg || ''].filter(Boolean).join('：') || JSON.stringify(item)
        })
        return parts.join('；') || fallback
    }
    return JSON.stringify(detail)
}

async function request(path, { method = 'GET', json, form, multipart, signal } = {}) {
    const init = { method, signal, headers: { Accept: 'application/json' } }
    if (json !== undefined) {
        init.headers['Content-Type'] = 'application/json'
        init.body = JSON.stringify(json)
    }
    if (form !== undefined) {
        init.headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
        init.body = new URLSearchParams(form).toString()
    }
    if (multipart !== undefined) {
        // 不要手写 Content-Type：必须让浏览器自己带上 boundary，否则后端解析不出文件
        init.body = multipart
    }

    let res
    try {
        res = await fetch(API_BASE + path, init)
    } catch (err) {
        if (err?.name === 'AbortError') throw err
        throw new ApiError('无法连接后端服务，请确认服务已在 127.0.0.1:8001 启动', 0)
    }

    const data = await readBody(res)
    if (!res.ok) {
        throw new ApiError(describeError(data, `请求失败（HTTP ${res.status}）`), res.status)
    }
    return data
}

export const api = {
    /** 任务列表 */
    listTasks(limit = 50) {
        return request(`/api/tasks?limit=${encodeURIComponent(limit)}`)
    },

    /** 任务详情 */
    getTask(id, signal) {
        return request(`/api/tasks/${encodeURIComponent(id)}`, { signal })
    },

    /** 新建任务（表单提交） */
    createTask(payload) {
        return request('/api/tasks', {
            method: 'POST',
            form: {
                source_text: payload.source_text,
                source_lang: payload.source_lang,
                target_lang: payload.target_lang,
                style: payload.style,
                domain: payload.domain ?? '',
                // 后端 FastAPI 对 bool 的解析兼容 true/false
                enable_human_review: payload.enable_human_review ? 'true' : 'false'
            }
        })
    },

    /** 新建任务（上传文档，multipart/form-data） */
    createTaskFromFile(file, payload) {
        const body = new FormData()
        body.append('file', file)
        body.append('source_lang', payload.source_lang)
        body.append('target_lang', payload.target_lang)
        body.append('style', payload.style ?? '')
        body.append('domain', payload.domain ?? '')
        body.append('enable_human_review', payload.enable_human_review ? 'true' : 'false')
        return request('/api/tasks/upload', { method: 'POST', multipart: body })
    },

    /** 人工审核结果提交 */
    resumeTask(id, { approved, edited_translation, comment }) {
        const json = { approved }
        if (edited_translation) json.edited_translation = edited_translation
        if (comment) json.comment = comment
        return request(`/api/tasks/${encodeURIComponent(id)}/resume`, { method: 'POST', json })
    },

    /** 失败重试 */
    retryTask(id) {
        return request(`/api/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' })
    },

    /** 保存术语表 */
    saveGlossary(id, glossary) {
        return request(`/api/tasks/${encodeURIComponent(id)}/glossary`, {
            method: 'POST',
            json: { glossary }
        })
    },

    /** 删除任务 */
    deleteTask(id) {
        return request(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },

    /** 导出下载地址（由浏览器直接下载） */
    exportUrl(id, fmt = 'md') {
        return `${API_BASE}/api/tasks/${encodeURIComponent(id)}/export?fmt=${encodeURIComponent(fmt)}`
    },

    /** 健康检查 */
    health() {
        return request('/api/health')
    }
}