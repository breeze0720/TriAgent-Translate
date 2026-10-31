<script setup>
/**
 * 中栏空状态：未选中任务时的引导
 */
defineEmits(['create'])
</script>

<template>
    <div class="empty-ws">
        <div class="empty-mark" aria-hidden="true">译</div>
        <h2 class="empty-title">开始一次新的翻译</h2>
        <p class="empty-desc">
            粘贴原文、选择语言与风格，多个智能体会依次完成术语抽取、初译、审校与润色；
            开启人工审核后，你可以在关键节点修改译文并决定是否通过。
        </p>
        <button class="btn btn-primary" type="button" @click="$emit('create')">＋ 新建任务</button>
        <ul class="empty-points">
            <li><span class="point-key mono">01</span>术语表可随时编辑并回写后端</li>
            <li><span class="point-key mono">02</span>进度通过 SSE 实时推送，断线自动重连</li>
            <li><span class="point-key mono">03</span>支持 Markdown / Word 导出与失败重试</li>
        </ul>
    </div>
</template>

<style scoped>
.empty-ws {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 40px 24px;
    text-align: center;
}

.empty-mark {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    margin-bottom: 2px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel-2);
    color: var(--muted);
    font-size: 18px;
}

.empty-title {
    color: var(--text);
    font-size: 15px;
    font-weight: 600;
}

.empty-desc {
    max-width: 420px;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.75;
}

.empty-points {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 10px;
    color: var(--faint);
    font-size: 11.5px;
    text-align: left;
}

.empty-points li {
    display: flex;
    align-items: baseline;
    gap: 8px;
}

.point-key {
    color: var(--accent);
    font-size: 10.5px;
}
</style>