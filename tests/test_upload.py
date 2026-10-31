"""
test_upload.py - 文档上传链路的单元测试

覆盖四道防线里能被纯函数验证的部分，外加 document_loader 的抽取：
扩展名白名单、体积上限、抽取结果（空文本/超长）、文件名消毒、zip 炸弹、真文件抽取。

为什么不做「起服务 + 真上传」的测试：
这些判断与框架无关，纯函数能直接构造极端输入（0 字节、1000:1 压缩比），
比启动 FastAPI + 连 MySQL 快两个数量级，也更容易定位失败原因。
端到端留给手工 curl 验证（见 README 的演示步骤）。
"""
import io
import zipfile

import pytest

import config
import document_loader
import upload_utils


# ---------------- 第一道：扩展名白名单 ----------------

def test_extension_allows_translatable_formats():
    for name in ["a.pdf", "报告.DOCX", "readme.md", "note.txt"]:
        assert upload_utils.check_extension(name) is None, name


def test_extension_rejects_unsupported_formats():
    """xlsx/pptx 是刻意不收的：表格行列抽取后会丢结构，翻出来是乱序段落。"""
    assert "不支持的文件格式" in upload_utils.check_extension("权限矩阵.xlsx")
    assert "不支持的文件格式" in upload_utils.check_extension("新人培训.pptx")
    assert "无扩展名" in upload_utils.check_extension("没有后缀")


def test_extension_uses_last_suffix_only():
    """a.pdf.exe 的真实扩展名是 .exe，不能因为名字里有 pdf 就放行。"""
    assert "不支持的文件格式" in upload_utils.check_extension("a.pdf.exe")


# ---------------- 第二道：体积上限 ----------------

def test_size_within_limit_passes():
    assert upload_utils.check_size(1024, max_mb=20) is None


def test_size_over_limit_rejected():
    reason = upload_utils.check_size((20 * 1024 * 1024) + 1, max_mb=20)
    assert reason is not None and "超过上限" in reason


def test_size_unknown_does_not_reject():
    """拿不到长度（分块传输）时放行，由落盘后的实际字节数兜底。"""
    assert upload_utils.check_size(None, max_mb=1) is None


# ---------------- 第四道：抽取结果校验 ----------------

def test_extracted_empty_text_mentions_scanned_pdf():
    reason = upload_utils.check_extracted_text("   \n\n  ")
    assert reason is not None
    assert "没有可提取的文字" in reason and "扫描" in reason


def test_extracted_text_too_long_reports_counts():
    reason = upload_utils.check_extracted_text("字" * 21, max_chars=20)
    assert reason is not None and "21" in reason and "20" in reason


def test_extracted_text_limit_matches_config():
    """文档上传和粘贴文本共用同一条长度上限，不能一个能绕过一个被拦。"""
    assert upload_utils.check_extracted_text("字" * config.MAX_SOURCE_CHARS) is None


# ---------------- 文件名消毒 / 落盘名 ----------------

def test_sanitize_filename_blocks_path_traversal():
    assert upload_utils.sanitize_filename("../../.env") == ".env"
    assert upload_utils.sanitize_filename("C:\\Windows\\a.pdf") == "a.pdf"
    assert upload_utils.sanitize_filename("") == "unnamed"


def test_sanitize_filename_strips_windows_reserved_chars():
    assert upload_utils.sanitize_filename('a<b>c:d.pdf') == "a_b_c_d.pdf"


def test_stored_filename_prefixes_content_hash():
    name = upload_utils.stored_filename("0123456789abcdef", "../../报告.docx")
    assert name == "01234567_报告.docx"


# ---------------- 第三道：zip 炸弹 ----------------

def _zip_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", payload)
    return buffer.getvalue()


def test_zip_bomb_detected_by_ratio():
    """2MB 全零内容压缩后极小，压缩比远超 100:1，属于典型构造文件。"""
    data = _zip_bytes(b"\0" * (2 * 1024 * 1024))
    reason = upload_utils.zip_bomb_reason(data, ".docx")
    assert reason is not None and "疑似 zip 炸弹" in reason


def test_normal_docx_passes_zip_check(tmp_path):
    from docx import Document

    path = tmp_path / "normal.docx"
    Document().save(path)
    assert upload_utils.zip_bomb_reason(path, ".docx") is None


def test_zip_check_skips_non_zip_extensions():
    """pdf/md/txt 不是 zip，不该被这条规则误伤。"""
    assert upload_utils.zip_bomb_reason(_zip_bytes(b"x" * 64), ".pdf") is None


def test_broken_zip_passes_and_lets_parser_report():
    """扩展名伪装成 docx 但不是合法 zip：放行，让解析器报「文件已损坏」更有用。"""
    assert upload_utils.zip_bomb_reason(b"not a zip at all", ".docx") is None


# ---------------- document_loader 真抽取 ----------------

def test_extract_txt_and_md(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("你好，世界。", encoding="utf-8")
    assert document_loader.extract_text(str(txt)) == "你好，世界。"

    md = tmp_path / "b.md"
    md.write_text("# 标题\n\n正文段落。", encoding="utf-8")
    assert "正文段落" in document_loader.extract_text(str(md))


def test_extract_docx(tmp_path):
    from docx import Document

    path = tmp_path / "c.docx"
    doc = Document()
    doc.add_paragraph("这是需要翻译的第一段。")
    doc.save(path)
    assert "这是需要翻译的第一段" in document_loader.extract_text(str(path))


def test_extract_unsupported_extension_raises(tmp_path):
    path = tmp_path / "d.xlsx"
    path.write_bytes(b"whatever")
    with pytest.raises(ValueError):
        document_loader.extract_text(str(path))