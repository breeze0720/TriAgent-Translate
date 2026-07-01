"""
document_loader.py - Document text extraction
Supports PDF, Word (.docx), Markdown (.md), and TXT files.
Extracts full text for translation (no chunking needed).
"""
import os


def extract_text(file_path: str) -> str:
    """
    Extract full text from a document file.

    Args:
        file_path: Path to the document file

    Returns:
        Extracted text content

    Raises:
        ValueError: Unsupported file format
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return _load_pdf(file_path)
    elif ext == ".docx":
        return _load_docx(file_path)
    elif ext in [".md", ".txt"]:
        return _load_text(file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext}. Supports PDF, DOCX, MD, TXT")


def _load_pdf(file_path: str) -> str:
    """Extract text from PDF using pypdf"""
    from pypdf import PdfReader

    reader = PdfReader(file_path)
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            pages.append(text.strip())

    print(f"[DocLoader] PDF: {len(reader.pages)} pages, extracted {len(pages)} non-empty pages")
    return "\n\n".join(pages)


def _load_docx(file_path: str) -> str:
    """Extract text from Word document using docx2txt"""
    import docx2txt

    text = docx2txt.process(file_path)
    result = text if text else ""
    print(f"[DocLoader] DOCX: extracted {len(result)} chars")
    return result


def _load_text(file_path: str) -> str:
    """Read plain text files (Markdown / TXT)"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    print(f"[DocLoader] Text file: {len(text)} chars")
    return text
