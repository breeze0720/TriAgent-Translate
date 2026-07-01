"""Initialize MySQL database and tables for translation system."""
import pymysql
import config

conn = pymysql.connect(
    host=config.MYSQL_HOST,
    port=config.MYSQL_PORT,
    user=config.MYSQL_USER,
    password=config.MYSQL_PASSWORD,
    charset='utf8mb4'
)
cursor = conn.cursor()
cursor.execute('CREATE DATABASE IF NOT EXISTS agent_translate DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
cursor.execute('USE agent_translate')
cursor.execute('''CREATE TABLE IF NOT EXISTS translation_tasks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_text TEXT NOT NULL,
    source_lang VARCHAR(20) DEFAULT 'zh',
    target_lang VARCHAR(20) DEFAULT 'en',
    translation TEXT,
    review_feedback TEXT,
    review_score DECIMAL(5,2),
    polished TEXT,
    final_result TEXT,
    status VARCHAR(30) DEFAULT 'pending',
    retry_count INT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
cursor.execute('''CREATE TABLE IF NOT EXISTS review_records (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT NOT NULL,
    version INT DEFAULT 1,
    translation_text TEXT,
    review_content TEXT,
    score DECIMAL(5,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES translation_tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
conn.commit()
conn.close()
print('Database setup complete!')
