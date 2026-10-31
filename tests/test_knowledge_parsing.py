"""知识库 Word / PDF 解析回归测试。

重点：python-docx 的 import 名是 ``docx``，不是 ``python-docx``；
早先用 ``_require("python-docx")`` 会让已经安装依赖的环境也报「缺少依赖」。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from backend.service.knowledge import KnowledgeStore, parse_docx, parse_pdf

docx = pytest.importorskip("docx")
pypdf = pytest.importorskip("pypdf")


def make_docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "甲方"
    table.rows[0].cells[1].text = "乙方"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_text_pdf_bytes(text: str) -> bytes:
    """生成一个最小可解析的文本 PDF，避免测试依赖 reportlab。"""
    content = f"BT /F1 12 Tf 50 150 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(output)


def test_parse_docx_uses_docx_import_name() -> None:
    pages = parse_docx(make_docx_bytes("三方协议"))
    assert pages
    assert "三方协议" in pages[0][1]
    assert "甲方 | 乙方" in pages[0][1]


def test_parse_pdf_extracts_text() -> None:
    pages = parse_pdf(make_text_pdf_bytes("Hello PDF"))
    assert pages
    assert "Hello PDF" in pages[0][1]


def test_knowledge_store_accepts_docx_and_pdf(tmp_path: Path) -> None:
    with KnowledgeStore(tmp_path / "kb.db") as store:
        docx_doc = store.add_document("三方协议.docx", make_docx_bytes("三方协议"))
        pdf_doc = store.add_document("说明.pdf", make_text_pdf_bytes("PDF text"))
        assert docx_doc.status == "ready"
        assert pdf_doc.status == "ready"
        assert docx_doc.chunk_count >= 1
        assert pdf_doc.chunk_count >= 1
        assert store.stats()["docs"] == 2