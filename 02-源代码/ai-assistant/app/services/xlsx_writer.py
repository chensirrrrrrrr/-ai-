"""用标准库写 xlsx（报告导出用，零依赖）。

为什么自己写：`openpyxl` / `pandas` 都是重依赖，而报告导出只需要「多 sheet + 表头 + 文本/数字」
这点能力。xlsx 本质是个 zip 装了几份 XML，标准库的 `zipfile` + `xml.sax.saxutils` 完全够。

取舍说明：
- 字符串统一用 **inlineStr**（`<c t="inlineStr"><is><t>…</t></is></c>`），
  省掉 sharedStrings 表和它的下标管理 —— 报告是一次性导出，不需要共享字符串省空间。
- 带一个极简 `styles.xml`：只为表头加粗 + 冻结首行，让导出的表看着是「做过设计的」。
- 不写公式、不写合并单元格、不写列宽自适应（按最长内容粗算一个宽度，够用）。
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from typing import Any, Iterable, List, Sequence, Tuple
from xml.sax.saxutils import escape

Sheet = Tuple[str, List[List[Any]]]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheet_overrides}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="等线"/></font>
<font><b/><sz val="11"/><name val="等线"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEFF3F8"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
</cellXfs>
</styleSheet>"""

# 表头 / 冻结首行
_STYLE_HEADER = 1
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_COL_WIDTH = 60
MIN_COL_WIDTH = 9


def _cell_ref(col: int, row: int) -> str:
    """列号 0-based →  Excel 列名（A、B…Z、AA…）+ 行号（1-based）。"""
    letters = ""
    index = col
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            break
    return f"{letters}{row}"


def _cell(value: Any, col: int, row: int, *, header: bool) -> str:
    ref = _cell_ref(col, row)
    style = f' s="{_STYLE_HEADER}"' if header else ""

    if value is None or value == "":
        return f'<c r="{ref}"{style}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    if isinstance(value, (datetime, date)):
        text = value.strftime("%Y-%m-%d %H:%M" if isinstance(value, datetime) else "%Y-%m-%d")
    else:
        text = str(value)
    return (f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
            f'{escape(text)}</t></is></c>')


def _display_length(value: Any) -> int:
    text = str(value or "")
    # 中日韩字符按 2 个宽度算，列宽才不会把中文挤成 ###
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _sheet_xml(rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<worksheet xmlns="{_NS_MAIN}"><sheetData/></worksheet>')

    widths = [MIN_COL_WIDTH] * max(len(r) for r in rows)
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], min(MAX_COL_WIDTH, _display_length(value)))

    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths))
    body = "".join(
        f'<row r="{r}">' + "".join(
            _cell(value, c, r, header=(r == 1))
            for c, value in enumerate(row)
        ) + "</row>"
        for r, row in enumerate(rows, start=1)
    )
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS_MAIN}">'
            f'<sheetViews><sheetView workbookViewId="0">'
            f'<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            f'</sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{body}</sheetData></worksheet>')


def write_workbook(sheets: Iterable[Sheet]) -> bytes:
    """把 [(sheet 名, 二维表)] 写成 xlsx 字节。

    第一个 sheet 会被 Excel 默认打开，所以调用方应该把「概览」放最前面。
    """
    sheets = [(name[:31] or f"Sheet{i}", list(rows))
              for i, (name, rows) in enumerate(sheets, start=1)]
    if not sheets:
        sheets = [("Sheet1", [])]

    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1))

    sheet_tags = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _) in enumerate(sheets, start=1))
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
                f'<sheets>{sheet_tags}</sheets></workbook>')

    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{_NS_REL}/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1))
    rels += (f'<Relationship Id="rId{len(sheets) + 1}" Type="{_NS_REL}/styles" '
             f'Target="styles.xml"/>')
    workbook_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     f'<Relationships xmlns="{_NS_PKG_REL}">{rels}</Relationships>')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES.format(sheet_overrides=overrides))
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", _STYLES)
        for i, (_name, rows) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return buffer.getvalue()
