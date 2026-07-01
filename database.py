"""
database.py - MySQL 数据库连接与操作
使用 pymysql 连接本地 MySQL，提供翻译任务的 CRUD 操作。
"""
import pymysql
import config


def get_connection():
    """
    获取数据库连接。
    每次调用创建新连接，调用方负责关闭。
    """
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,  # 返回字典格式，方便转 JSON
    )


# ============================================
# 翻译任务 CRUD
# ============================================

def create_task(source_text: str, source_lang: str, target_lang: str) -> int:
    """
    创建新的翻译任务（状态为 pending）。
    返回新任务的 ID。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                INSERT INTO translation_tasks (source_text, source_lang, target_lang, status)
                VALUES (%s, %s, %s, 'pending')
            """
            cursor.execute(sql, (source_text, source_lang, target_lang))
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def update_task_status(task_id: int, status: str):
    """
    更新任务状态。
    status 取值：pending / translating / reviewing / polishing / done / failed
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE translation_tasks SET status = %s WHERE id = %s"
            cursor.execute(sql, (status, task_id))
            conn.commit()
    finally:
        conn.close()


def save_translation(task_id: int, translation: str):
    """保存翻译Agent的输出"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE translation_tasks SET translation = %s WHERE id = %s"
            cursor.execute(sql, (translation, task_id))
            conn.commit()
    finally:
        conn.close()


def save_review(task_id: int, feedback: str, score: float):
    """保存审校Agent的输出"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                UPDATE translation_tasks
                SET review_feedback = %s, review_score = %s
                WHERE id = %s
            """
            cursor.execute(sql, (feedback, score, task_id))
            conn.commit()
    finally:
        conn.close()


def save_polished(task_id: int, polished: str):
    """保存润色Agent的输出，并标记任务完成"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                UPDATE translation_tasks
                SET polished = %s, final_result = %s, status = 'done'
                WHERE id = %s
            """
            cursor.execute(sql, (polished, polished, task_id))
            conn.commit()
    finally:
        conn.close()


def mark_failed(task_id: int, error: str):
    """标记任务失败"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE translation_tasks SET status = 'failed' WHERE id = %s"
            cursor.execute(sql, (task_id,))
            conn.commit()
    finally:
        conn.close()


def increment_retry(task_id: int) -> int:
    """增加重试次数，返回新的重试次数"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE translation_tasks SET retry_count = retry_count + 1 WHERE id = %s"
            cursor.execute(sql, (task_id,))
            conn.commit()
            # 读取最新值
            cursor.execute("SELECT retry_count FROM translation_tasks WHERE id = %s", (task_id,))
            row = cursor.fetchone()
            return row["retry_count"] if row else 0
    finally:
        conn.close()


def get_task(task_id: int) -> dict | None:
    """获取单个任务的完整信息"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM translation_tasks WHERE id = %s", (task_id,))
            return cursor.fetchone()
    finally:
        conn.close()


def get_all_tasks(page: int = 1, page_size: int = 20) -> list[dict]:
    """分页获取所有翻译任务（按时间倒序）"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            offset = (page - 1) * page_size
            sql = """
                SELECT id, source_text, source_lang, target_lang,
                       review_score, status, retry_count, created_at, updated_at
                FROM translation_tasks
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """
            cursor.execute(sql, (page_size, offset))
            return cursor.fetchall()
    finally:
        conn.close()


def get_task_count() -> int:
    """获取任务总数"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS cnt FROM translation_tasks")
            row = cursor.fetchone()
            return row["cnt"] if row else 0
    finally:
        conn.close()


def delete_task(task_id: int) -> bool:
    """删除指定任务，返回是否删除成功"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM translation_tasks WHERE id = %s", (task_id,))
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()


# ============================================
# 审校记录操作
# ============================================

def save_review_record(task_id: int, version: int, translation_text: str, review_content: str, score: float):
    """保存一条审校记录（用于追溯每次审校的历史）"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                INSERT INTO review_records (task_id, version, translation_text, review_content, score)
                VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (task_id, version, translation_text, review_content, score))
            conn.commit()
    finally:
        conn.close()


# ============================================
# 数据库连接测试
# ============================================

def test_connection() -> bool:
    """测试数据库连接是否正常"""
    try:
        conn = get_connection()
        conn.ping()
        conn.close()
        return True
    except Exception as e:
        print(f"[数据库] 连接测试失败: {e}")
        return False
