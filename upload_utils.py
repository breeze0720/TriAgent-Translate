"""
upload_utils.py - 文档上传的纯函数校验层

为什么要单独抽一层（和项目一同样的理由）：
上传链路的坑几乎全在「输入是否合法」这一侧——扩展名伪造、体积超限、docx 其实是 zip 炸弹、
扫描版 PDF 抽不出文字。这些问题本质是纯判断，跟 FastAPI、跟数据库都没关系。
做成纯函数后，pytest 里可以直接构造极端输入（造 1000:1 的压缩比、造 0 字节文件），
不需要起服务、不需要真上传 20MB。

四道防线，顺序即成本从低到高：
1) 扩展名白名单 —— 最便宜的拒绝，连文件都不读；
2) 体积上限 —— 先看声明大小，落盘后再按实际字节数兜底（声明大小可以撒谎）；
3) zip 炸弹 —— docx 本质是 zip，只读中央目录里的 file_size，不解压，毫秒级；
4) 抽取结果校验 —— 空文本（扫描件/纯图片 PDF）、超长文本，在这里拦。
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import config

# 只有 docx 是 zip 容器（本项目不收 xlsx/pptx），所以炸弹检查只对它生效
ZIP_CONTAINER_EXTENSIONS = frozenset({".docx"})
ALLOWED = frozenset(config.ALLOWED_UPLOAD_EXTENSIONS)
_MB = 1024 * 1024
# 正常 Word 文档条目数通常 < 1000；上万条目基本是构造出来的
_MAX_ZIP_ENTRIES = 10000


def supported_hint() -> str:
    """给前端/接口用的统一提示文案，避免每处各写一份、改一处漏一处。"""
    return "、".join(config.ALLOWED_UPLOAD_EXTENSIONS)


def check_extension(filename: str) -> str | None:
    """第一道防线：扩展名白名单。返回中文错误文案，None 表示通过。"""
    ext = Path((filename or "").replace("\\", "/")).suffix.lower()
    if ext not in ALLOWED:
        return f"不支持的文件格式「{ext or '无扩展名'}」，仅支持 {supported_hint()}"
    return None


def check_size(size: int | None, max_mb: int | None = None) -> str | None:
    """
    第二道防线：体积上限（预检）。
    size 为 None（分块传输拿不到长度）时返回 None 放行，由落盘后的实际字节数兜底——
    不能因为拿不到长度就当作 0，也不能因为拿不到长度就放行。
    """
    if size is None or size < 0:
        return None
    limit_mb = config.MAX_UPLOAD_MB if max_mb is None else int(max_mb)
    if size > limit_mb * _MB:
        return f"文件过大：{size / _MB:.1f} MB，超过上限 {limit_mb} MB"
    return None


def check_extracted_text(text: str, max_chars: int | None = None) -> str | None:
    """
    第四道防线：抽取结果校验。这一步最容易被忽略，也最容易在演示现场翻车。
    - 空文本：扫描版 PDF / 纯图片 PDF 没有文字层，pypdf 抽出来就是空串。
      这不是服务端 500，而是用户该换一份带文字层的文件，所以要给可读的原因。
    - 超长：与 /api/tasks 的 20000 字符保持一致，否则「粘贴被拦、上传能绕过去」。
    """
    limit = config.MAX_SOURCE_CHARS if max_chars is None else int(max_chars)
    body = (text or "").strip()
    if not body:
        return (
            "文档中没有可提取的文字：可能是扫描版/图片版 PDF，或文档内容全在图片里。"
            "请换一份带文字层的文件，或直接把原文粘贴到文本框。"
        )
    if len(body) > limit:
        return f"文档提取出 {len(body)} 字符，超过 {limit} 字符上限，请拆分后分批提交"
    return None


def sanitize_filename(filename: str) -> str:
    """
    去掉路径与非法字符：防目录穿越（../../.env）和 Windows 保留字符。
    只取最后一段文件名并限长，避免超长文件名在磁盘上直接报错。
    """
    name = Path((filename or "").replace("\\", "/")).name.strip()
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    name = "".join(ch for ch in name if ord(ch) >= 32)
    # 只去尾部点和空格：Windows 不允许文件名以它们结尾，但开头的点（.env）是合法的
    name = name.rstrip(". ")
    return (name or "unnamed")[:120]


def content_sha256(data: bytes) -> str:
    """落盘名用内容哈希前缀，同一次上传重试时不会在 uploads/ 里堆副本。"""
    return hashlib.sha256(data).hexdigest()


def stored_filename(file_hash: str, filename: str) -> str:
    return f"{(file_hash or '')[:8]}_{sanitize_filename(filename)}"


def zip_bomb_reason(
    source: str | Path | bytes | bytearray,
    ext: str,
    max_ratio: int = 100,
    max_total_mb: int = 500,
    max_entries: int = _MAX_ZIP_ENTRIES,
) -> str | None:
    """
    第三道防线：docx 是 zip，几百 KB 可以解压出几十 GB。
    只读中央目录里声明的 file_size，不解压，因此检查一个「10GB 展开」的炸弹也只要毫秒级。
    非 zip 格式、或根本不是合法 zip（扩展名伪装）直接放行，让真正的解析器报「文件已损坏」，
    那种错误信息对用户更有用。
    """
    if (ext or "").lower() not in ZIP_CONTAINER_EXTENSIONS:
        return None
    opened: str | Path | io.BytesIO
    opened = io.BytesIO(bytes(source)) if isinstance(source, (bytes, bytearray)) else source
    try:
        with zipfile.ZipFile(opened) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                return f"压缩包条目过多（{len(infos)} > {max_entries}），疑似恶意文件，已拒绝"
            total = sum(info.file_size for info in infos)
            compressed = max(1, sum(info.compress_size for info in infos))
    except (zipfile.BadZipFile, OSError):
        return None
    limit = int(max_total_mb) * _MB
    if total > limit:
        return f"解压后总大小约 {total / _MB:.1f} MB，超过上限 {max_total_mb} MB，疑似 zip 炸弹，已拒绝"
    ratio = total / compressed
    if ratio > max_ratio:
        return f"压缩比异常（约 {ratio:.0f}:1 > {max_ratio}:1），疑似 zip 炸弹，已拒绝"
    return None