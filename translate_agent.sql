-- ============================================
-- 多智能体协作翻译与审校系统 - 建表语句
-- 使用方法：复制到 Navicat 查询窗口执行，或在命令行中执行
-- ============================================

-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS agent_translate
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE agent_translate;

-- 翻译任务主表
CREATE TABLE IF NOT EXISTS translation_tasks (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '任务ID',
    source_text TEXT NOT NULL COMMENT '原文',
    source_lang VARCHAR(20) NOT NULL DEFAULT 'zh' COMMENT '源语言',
    target_lang VARCHAR(20) NOT NULL DEFAULT 'en' COMMENT '目标语言',
    translation TEXT COMMENT '翻译Agent输出',
    review_feedback TEXT COMMENT '审校Agent意见',
    review_score DECIMAL(5,2) COMMENT '审校评分 0-100',
    polished TEXT COMMENT '润色Agent输出',
    final_result TEXT COMMENT '最终结果（润色后的文本）',
    status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT '任务状态: pending/translating/reviewing/polishing/done/failed',
    retry_count INT DEFAULT 0 COMMENT '审校不通过后的重试次数',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='翻译任务表';

-- 审校记录表（保留每次审校的历史版本，方便复盘）
CREATE TABLE IF NOT EXISTS review_records (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID',
    task_id INT NOT NULL COMMENT '关联翻译任务ID',
    version INT NOT NULL DEFAULT 1 COMMENT '审校版本号（第几次审校）',
    translation_text TEXT COMMENT '当前版本译文',
    review_content TEXT COMMENT '审校意见',
    score DECIMAL(5,2) COMMENT '本次评分 0-100',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    FOREIGN KEY (task_id) REFERENCES translation_tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审校记录表';
