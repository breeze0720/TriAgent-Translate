"""
init_db.py - 初始化 / 迁移 MySQL 表结构

用法：
    python init_db.py            # 建库建表（幂等），并补齐缺失列
    python init_db.py --reset    # 先删掉三张业务表再重建（清空任务数据）

设计说明：
- 建表语句和 translate_agent.sql 保持同步，便于直接在数据库客户端里核对；
- 除了 CREATE TABLE IF NOT EXISTS，还做一次「缺列补齐」：
  旧版本已经建过 tasks 表时，新增字段（如 stage/version/history_json）不会自动出现，
  迁移逻辑用 information_schema 检查后 ALTER TABLE，避免手工改库。
"""
import argparse
import sys

import pymysql

import config

# 表名会拼进 f-string，先做白名单校验，避免环境变量被写成奇怪的东西。
_DB_NAME = "".join(ch for ch in config.MYSQL_DATABASE if ch.isalnum() or ch == "_") or "agent_translate"


TASKS_DDL = """
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='翻译任务主表'
"""

TASK_EVENTS_DDL = """
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='任务阶段事件（幂等：task_id+stage+version 唯一）'
"""

GLOSSARY_DDL = """
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人工可编辑的术语表'
"""

# 迁移用：任务表新增列。key=列名，value=(列定义, 是否允许 NULL/默认值写法)
_TASKS_COLUMNS = {
    "style": "VARCHAR(200) NOT NULL DEFAULT ''",
    "domain": "VARCHAR(200) NOT NULL DEFAULT ''",
    "enable_human_review": "TINYINT(1) NOT NULL DEFAULT 0",
    "status": "VARCHAR(30) NOT NULL DEFAULT 'pending'",
    "stage": "VARCHAR(30) NOT NULL DEFAULT 'init'",
    "progress": "TINYINT NOT NULL DEFAULT 0",
    "retry_count": "INT NOT NULL DEFAULT 0",
    "version": "INT NOT NULL DEFAULT 0",
    "score": "DECIMAL(5,2) DEFAULT NULL",
    "translation": "MEDIUMTEXT",
    "polished": "MEDIUMTEXT",
    "final_result": "MEDIUMTEXT",
    "review_json": "LONGTEXT",
    "glossary_json": "LONGTEXT",
    "segments_json": "LONGTEXT",
    "translations_json": "LONGTEXT",
    "history_json": "LONGTEXT",
    "error_message": "VARCHAR(1000) NOT NULL DEFAULT ''",
}


def _ensure_columns(cursor, table: str, columns: dict[str, str]) -> list[str]:
    """检查 information_schema，把缺失的列 ALTER 进来。返回实际新增的列名。"""
    cursor.execute(
        """
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (_DB_NAME, table),
    )
    existing = {row["COLUMN_NAME"] for row in cursor.fetchall()}
    added: list[str] = []
    for name, definition in columns.items():
        if name in existing:
            continue
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {definition}")
        added.append(name)
    return added


def init_database(reset: bool = False) -> None:
    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{_DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE `{_DB_NAME}`")

            if reset:
                # 先删子表再删主表，避免外键约束报错。
                cursor.execute("DROP TABLE IF EXISTS task_events")
                cursor.execute("DROP TABLE IF EXISTS glossary")
                cursor.execute("DROP TABLE IF EXISTS tasks")
                print("[init_db] --reset：已删除 tasks / task_events / glossary")

            cursor.execute(TASKS_DDL)
            cursor.execute(TASK_EVENTS_DDL)
            cursor.execute(GLOSSARY_DDL)

            added = _ensure_columns(cursor, "tasks", _TASKS_COLUMNS)
            if added:
                print(f"[init_db] 迁移：tasks 表补齐列 {added}")

            cursor.execute("SHOW TABLES")
            tables = [list(row.values())[0] for row in cursor.fetchall()]
            print(f"[init_db] 数据库 `{_DB_NAME}` 就绪，表：{tables}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化多智能体翻译系统的 MySQL 表结构")
    parser.add_argument("--reset", action="store_true", help="先删除三张业务表再重建（清空任务数据）")
    args = parser.parse_args()

    try:
        init_database(reset=args.reset)
    except Exception as exc:
        print(f"[init_db] 初始化失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())