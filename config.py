"""
config.py - 配置加载模块
从 .env 文件读取所有配置项，包括双模型 API、邮件、数据库。
"""
import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# ============================================
# Qwen 模型配置（审校 Agent）
# ============================================
QWEN_MODEL_NAME = os.getenv("QWEN_MODEL_NAME", "qwen3.7-max")
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL")

# ============================================
# DeepSeek 模型配置（翻译 Agent + 润色 Agent）
# ============================================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
DEEPSEEK_MODEL_NAME = os.getenv("DEEPSEEK_MODEL_NAME", "deepseek-chat")

# ============================================
# 邮件配置（QQ邮箱 SMTP）
# ============================================
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.qq.com")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# ============================================
# MySQL 数据库配置
# ============================================
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agent_translate")
