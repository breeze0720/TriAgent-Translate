-- ============================================
-- 多智能体协作翻译与审校系统 v2 - 建表语句
-- 与 init_db.py 保持同步；可直接在 Navicat / 命令行执行。
-- 与 v1 的 translation_tasks / review_records 相比：
--   1) tasks 表同时保存运行态（status/stage/progress）和完整结果快照；
--   2) task_events 用 (task_id, stage, version) 唯一键保证图重放时事件不重复；
--   3) glossary 单独成表，支持前端人工编辑术语表后「整表替换」。
-- 旧版表如仍在库里不会影响新程序，可手工删除。
-- ============================================

CREATE DATABASE IF NOT EXISTS agent_translate
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE agent_translate;

-- 翻译任务主表
CREATE TABLE IF NOT EXISTS tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '任务ID',
    source_text MEDIUMTEXT NOT NULL COMMENT '原文',
    source_lang VARCHAR(20) NOT NULL DEFAULT '中文' COMMENT '源语言',
    target_lang VARCHAR(20) NOT NULL DEFAULT 'English' COMMENT '目标语言',
    style VARCHAR(200) NOT NULL DEFAULT '' COMMENT '风格要求',
    domain VARCHAR(200) NOT NULL DEFAULT '' COMMENT '领域',
    enable_human_review TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否启用人工审核',
    status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/waiting_human/done/failed',
    stage VARCHAR(30) NOT NULL DEFAULT 'init' COMMENT '当前阶段：init/terminology/translate/review/polish/human_review/final',
    progress TINYINT NOT NULL DEFAULT 0 COMMENT '进度百分比 0-100',
    retry_count INT NOT NULL DEFAULT 0 COMMENT '已进入翻译节点的次数',
    version INT NOT NULL DEFAULT 0 COMMENT '状态版本号，每次节点写入 +1',
    score DECIMAL(5,2) DEFAULT NULL COMMENT '审校总分 0-100',
    translation MEDIUMTEXT COMMENT '当前（最终）译文',
    polished MEDIUMTEXT COMMENT '润色结果',
    final_result MEDIUMTEXT COMMENT '最终交付译文',
    review_json LONGTEXT COMMENT '审校结构化结果 JSON',
    glossary_json LONGTEXT COMMENT '术语表 JSON',
    segments_json LONGTEXT COMMENT '分段原文 JSON',
    translations_json LONGTEXT COMMENT '分段译文 JSON',
    history_json LONGTEXT COMMENT '阶段历史 JSON（累积型 channel 的快照）',
    error_message VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '失败原因',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    KEY idx_tasks_status (status),
    KEY idx_tasks_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='翻译任务主表';

-- 任务阶段事件表（幂等：同一任务的同一阶段同一版本只保留一条）
CREATE TABLE IF NOT EXISTS task_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '事件ID',
    task_id BIGINT NOT NULL COMMENT '关联任务',
    stage VARCHAR(30) NOT NULL DEFAULT '' COMMENT '阶段',
    status VARCHAR(30) NOT NULL DEFAULT '' COMMENT '状态',
    message VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '事件描述',
    version INT NOT NULL DEFAULT 0 COMMENT '写入时的 state.version',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY uk_task_stage_version (task_id, stage, version),
    KEY idx_task_events_task (task_id),
    CONSTRAINT fk_task_events_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='任务阶段事件';

-- 术语表（前端可人工编辑，整表替换）
CREATE TABLE IF NOT EXISTS glossary (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '术语ID',
    task_id BIGINT NOT NULL COMMENT '关联任务',
    term VARCHAR(200) NOT NULL COMMENT '源语言术语',
    translation VARCHAR(200) NOT NULL COMMENT '固定译法',
    note VARCHAR(500) NOT NULL DEFAULT '' COMMENT '备注',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY uk_glossary_task_term (task_id, term),
    KEY idx_glossary_task (task_id),
    CONSTRAINT fk_glossary_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人工可编辑的术语表';