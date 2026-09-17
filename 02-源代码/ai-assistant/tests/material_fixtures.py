# -*- coding: utf-8 -*-
"""测试用的最小材料夹具：用纯标准库造出结构合法的 xlsx / pdf。

为什么要自己造而不是放二进制样本：
1. 二进制样本进仓库要引 Git LFS，评审也不好看；
2. 自己造能精确控制文本，断言才好写；
3. 顺带证明「没装 openpyxl 也能解 xlsx」这条兜底路径确实能用。
"""
from __future__ import annotations

import io
import zipfile
from typing import Iterable, List, Sequence

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CONTENT_TYPES = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)


def make_xlsx(rows: Iterable[Sequence[str]]) -> bytes:
    """把二维表写成标准 xlsx（共享字符串 + 单工作表，够用且结构合法）。"""
    shared: List[str] = []
    index: dict = {}
    row_xml: List[str] = []

    for row_no, row in enumerate(rows, start=1):
        cells: List[str] = []
        for col_no, value in enumerate(row):
            if value is None or value == "":
                continue
            if value not in index:
                index[value] = len(shared)
                shared.append(str(value))
            column = chr(ord("A") + col_no)
            cells.append(f'<c r="{column}{row_no}" t="s"><v>{index[value]}</v></c>')
        if cells:
            row_xml.append(f'<row r="{row_no}">{"".join(cells)}</row>')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0"?><sst xmlns="{_NS}" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">'
            + "".join(f"<si><t>{text}</t></si>" for text in shared)
            + "</sst>")
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0"?><worksheet xmlns="{_NS}"><sheetData>'
            + "".join(row_xml)
            + "</sheetData></worksheet>")
    return buffer.getvalue()


def make_pdf(lines: Sequence[str]) -> bytes:
    """单页 PDF，未压缩内容流；xref 与 /Length 都按真实字节数写。

    这样 pypdf / pdfplumber / 标准库兜底三条路都能读出来 ——
    如果偷懒写错 /Length，pdfplumber 会越界读到下一个对象，正文变乱码，
    排查起来会误判成「我们的解析器有 bug」。
    """
    content = b"BT /F1 12 Tf 72 720 Td\n"
    for line in lines:
        content += b"(" + line.encode("latin-1") + b") Tj T*\n"
    content += b"ET\n"

    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: List[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj".encode() + body + b"endobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer<</Root 1 0 R/Size {len(objects) + 1}>>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)
